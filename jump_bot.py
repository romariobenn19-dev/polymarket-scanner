#!/usr/bin/env python3
"""
jump_bot.py - Version G: fade sudden price rises. PAPER MODE ONLY (no orders are sent).

    python jump_bot.py run --hours 6           watch markets and paper-trade
    python jump_bot.py report                  results so far

Why: in 5,671 resolved markets (research/FINDINGS.md), after a 12-hour rise of 10c+ that left the
price at 30-60c, the price fell back by ~15c on average within 2 days. Historical prices are
12-hourly and have no order books, so this bot checks whether that move can actually be captured.

Every 5 minutes:
  1. Signals. A market qualifies when its price is between 30c and 60c, it rose 15c+ within the
     last 12 hours (checked on hourly prices), it ends 3+ days from now, and its total volume is
     $5k-$1M. One signal per market per 3 days.
  2. Entry, priced from the live order book. Buying NO = selling YES into the waiting YES buyers.
     The bot walks the YES bids for the shares needed, pays the market's taker fee, and records the
     average price. It skips (and records why) if the YES spread is wider than 5c or there are not
     enough buyers for the stake. $5 per trade, up to $50 in use, one trade per event.
  3. Exit, also from the live order book plus fee: profit of 12c+ per share, loss of 20c+ per share,
     or after 48 hours, whichever comes first. A market that resolves first settles at its result.
  4. Every signal, traded or not, has its price recorded again 48 hours later, to show whether the
     reversal also happens in the markets that could not be traded.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass

import scanner as sc

H = 3600


@dataclass
class ConfigG:
    budget: float = 50.0
    stake: float = 5.0               # $ per trade
    min_price: float = 0.30          # YES price after the rise
    max_price: float = 0.60
    min_rise: float = 0.15           # within rise_hours
    rise_hours: float = 12.0
    prefilter_day_change: float = 0.10
    min_days_left: float = 3.0
    min_volume: float = 5_000.0
    max_volume: float = 1_000_000.0
    max_spread: float = 0.05
    take_profit: float = 0.12        # per share, after fees
    stop_loss: float = 0.20
    max_hold_hours: float = 48.0
    cooldown_hours: float = 72.0
    poll_seconds: int = 300


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (ts REAL, market_id TEXT, event_id TEXT, question TEXT, token TEXT,
    yes_mid REAL, rise REAL, entered INT, reason TEXT, mid_48h REAL);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, ts REAL, market_id TEXT, event_id TEXT, question TEXT,
    token TEXT, fee_rate REAL, yes_mid REAL, rise REAL, shares REAL, entry_no REAL, entry_fee REAL,
    exit_ts REAL, exit_no REAL, exit_fee REAL, reason TEXT, pnl REAL);
CREATE TABLE IF NOT EXISTS marks (ts REAL, cash REAL, open_value REAL, open_trades INT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def taker_fee(shares: float, price: float, rate: float) -> float:
    return shares * rate * price * (1 - price)


def walk(levels: list, shares: float):
    """Average price for `shares` against book levels (best first). None if not enough size."""
    left, cost = shares, 0.0
    for price, size in levels:
        take = min(left, size)
        cost += take * price
        left -= take
        if left <= 1e-9:
            return cost / shares
    return None


def yes_levels(book: dict):
    """(bids best-first, asks best-first) as (price, size) pairs."""
    bids = sorted(((float(l["price"]), float(l["size"])) for l in book.get("bids", [])), reverse=True)
    asks = sorted((float(l["price"]), float(l["size"])) for l in book.get("asks", []))
    return bids, asks


def buy_no(book: dict, stake: float):
    """Buy NO for `stake` dollars by selling YES into the bids. Returns (shares, avg NO price) or None."""
    bids, _ = yes_levels(book)
    if not bids:
        return None
    no_price = 1 - bids[0][0]
    shares = stake / no_price
    avg_yes = walk(bids, shares)
    if avg_yes is None:
        return None
    return shares, 1 - avg_yes


def sell_no(book: dict, shares: float):
    """Average NO price when selling `shares` NO (= buying YES from the asks). None if too thin."""
    _, asks = yes_levels(book)
    avg_yes = walk(asks, shares)
    return None if avg_yes is None else 1 - avg_yes


def rise_in_window(prices: list, now_price: float, hours: float, now: float):
    """Rise from the lowest price in the last `hours` to now (0 if the low is the latest point)."""
    window = [p for t, p in prices if now - t <= hours * H]
    if not window:
        return 0.0
    return now_price - min(window)


class JumpBot:
    def __init__(self, cfg: ConfigG, db_path="paper_G.db"):
        self.cfg = cfg
        self.con = sqlite3.connect(db_path)
        self.con.executescript(SCHEMA)
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('config', ?)", (json.dumps(asdict(cfg)),))
        row = self.con.execute("SELECT v FROM meta WHERE k='cash'").fetchone()
        self.cash = float(row[0]) if row else cfg.budget
        self.con.commit()

    # -- helpers --
    def open_trades(self):
        return self.con.execute("SELECT id, market_id, event_id, token, shares, entry_no, entry_fee, ts, fee_rate "
                                "FROM trades WHERE exit_ts IS NULL").fetchall()

    def recently_signalled(self, market_id: str, now: float) -> bool:
        return self.con.execute("SELECT 1 FROM signals WHERE market_id=? AND ts > ?",
                                (market_id, now - self.cfg.cooldown_hours * H)).fetchone() is not None

    def save_cash(self):
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('cash', ?)", (str(self.cash),))

    # -- 1 + 2: signals and entries --
    def candidates(self, events: list, now: float) -> list:
        cfg, out = self.cfg, []
        for e in events:
            for m in e.get("markets") or []:
                if not sc.tradable(m) or not sc.yes_token(m):
                    continue
                bb, ba = sc._f(m.get("bestBid")), sc._f(m.get("bestAsk"))
                change = sc._f(m.get("oneDayPriceChange")) or 0.0
                vol = sc._f(m.get("volumeNum")) or 0.0
                if bb is None or ba is None or change < cfg.prefilter_day_change:
                    continue
                mid = (bb + ba) / 2
                if not (cfg.min_price <= mid <= cfg.max_price) or not (cfg.min_volume <= vol <= cfg.max_volume):
                    continue
                if days_left(m.get("endDate"), now) < cfg.min_days_left:
                    continue
                fee = float((m.get("feeSchedule") or {}).get("rate") or 0) if m.get("feesEnabled") else 0.0
                out.append({"market_id": m["id"], "event_id": str(e.get("id", "")), "question": m.get("question", ""),
                            "token": sc.yes_token(m), "fee_rate": fee})
        return out

    def consider(self, c: dict, book: dict, history: list, now: float) -> None:
        cfg = self.cfg
        bids, asks = yes_levels(book)
        if not bids or not asks:
            return
        mid = (bids[0][0] + asks[0][0]) / 2
        if not (cfg.min_price <= mid <= cfg.max_price):
            return
        rise = rise_in_window(history, mid, cfg.rise_hours, now)
        if rise < cfg.min_rise:
            return
        reason, entered = None, 0
        held_events = {r[2] for r in self.open_trades()}
        in_use = sum(r[4] * r[5] for r in self.open_trades())
        if asks[0][0] - bids[0][0] > cfg.max_spread:
            reason = f"spread {100 * (asks[0][0] - bids[0][0]):.1f}c too wide"
        elif c["event_id"] in held_events:
            reason = "already holding this event"
        elif in_use + cfg.stake > cfg.budget or self.cash < cfg.stake:
            reason = "budget in use"
        else:
            got = buy_no(book, cfg.stake)
            if got is None:
                reason = "not enough buyers for the stake"
            else:
                shares, no_price = got
                fee = taker_fee(shares, no_price, c["fee_rate"])
                self.cash -= shares * no_price + fee
                self.con.execute("""INSERT INTO trades (ts, market_id, event_id, question, token, fee_rate, yes_mid, rise,
                                    shares, entry_no, entry_fee) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                                 (now, c["market_id"], c["event_id"], c["question"], c["token"], c["fee_rate"], mid, rise,
                                  shares, no_price, fee))
                entered, reason = 1, f"bought {shares:.1f} NO at {no_price:.3f}"
                print(f"[{sc.iso(now)}] ENTER {c['question'][:60]} | YES {mid:.3f} (+{rise * 100:.0f}c) | {reason}", flush=True)
        self.con.execute("INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                         (now, c["market_id"], c["event_id"], c["question"], c["token"], mid, rise, entered, reason))
        if not entered:
            print(f"[{sc.iso(now)}] signal, not traded: {c['question'][:60]} | YES {mid:.3f} (+{rise * 100:.0f}c) | {reason}", flush=True)

    # -- 3: exits --
    def manage(self, books: dict, closed: dict, now: float) -> float:
        """Exit what needs exiting; return the current value of open trades."""
        cfg, open_value = self.cfg, 0.0
        for tid, mid_id, ev, token, shares, entry_no, entry_fee, ts, fee_rate in self.open_trades():
            if mid_id in closed:                       # resolved: NO pays 1 if YES lost
                exit_no, reason, fee = (1.0 if closed[mid_id] == 0 else 0.0), "resolved", 0.0
            else:
                b = books.get(token)
                exit_no = sell_no(b, shares) if b else None
                if exit_no is None:
                    continue
                fee = taker_fee(shares, exit_no, fee_rate)
                gain = exit_no - entry_no - (fee + entry_fee) / shares
                reason = ("take profit" if gain >= cfg.take_profit else "stop loss" if gain <= -cfg.stop_loss
                          else "48 hours" if now - ts >= cfg.max_hold_hours * H else None)
                if reason is None:
                    open_value += shares * exit_no - fee
                    continue
            pnl = shares * (exit_no - entry_no) - entry_fee - fee
            self.cash += shares * exit_no - fee
            self.con.execute("UPDATE trades SET exit_ts=?, exit_no=?, exit_fee=?, reason=?, pnl=? WHERE id=?",
                             (now, exit_no, fee, reason, pnl, tid))
            print(f"[{sc.iso(now)}] EXIT ({reason}) trade {tid}: NO {entry_no:.3f} -> {exit_no:.3f}, PnL ${pnl:+.2f}", flush=True)
        return open_value

    def follow_up(self, books: dict, now: float) -> None:
        """Record every signal's price 48 hours later."""
        for rowid, token in self.con.execute("SELECT rowid, token FROM signals WHERE mid_48h IS NULL AND ts <= ?",
                                             (now - self.cfg.max_hold_hours * H,)).fetchall():
            b = books.get(token)
            if b:
                bids, asks = yes_levels(b)
                if bids and asks:
                    self.con.execute("UPDATE signals SET mid_48h=? WHERE rowid=?", ((bids[0][0] + asks[0][0]) / 2, rowid))

    # -- loop --
    def poll(self) -> None:
        now = time.time()
        events = sc.fetch_events(min_liquidity=500)
        cands = [c for c in self.candidates(events, now) if not self.recently_signalled(c["market_id"], now)]
        pending = [r[0] for r in self.con.execute("SELECT DISTINCT token FROM signals WHERE mid_48h IS NULL AND ts <= ?",
                                                   (now - self.cfg.max_hold_hours * H,))]
        tokens = list({c["token"] for c in cands} | {r[3] for r in self.open_trades()} | set(pending))
        books = sc.fetch_books(tokens) if tokens else {}
        for c in cands:
            if c["token"] in books:
                hist = price_history(c["token"])
                self.consider(c, books[c["token"]], hist, now)
        closed = resolved_markets([r[1] for r in self.open_trades()])
        open_value = self.manage(books, closed, now)
        self.follow_up(books, now)
        self.save_cash()
        n_open = len(self.open_trades())
        self.con.execute("INSERT INTO marks VALUES (?,?,?,?)", (now, self.cash, open_value, n_open))
        self.con.commit()
        print(f"[{sc.iso(now)}] {len(cands)} candidates | cash ${self.cash:.2f} | open {n_open} worth ${open_value:.2f} "
              f"| total ${self.cash + open_value:.2f}", flush=True)

    def run(self, hours: float) -> None:
        end = time.time() + hours * H
        while time.time() < end:
            started = time.time()
            try:
                self.poll()
            except Exception as e:
                print(f"[{sc.iso(started)}] poll failed: {e}", file=sys.stderr, flush=True)
            time.sleep(max(0, self.cfg.poll_seconds - (time.time() - started)))


def days_left(end_iso, now: float) -> float:
    if not end_iso:
        return 0.0
    try:
        return (sc_ts(end_iso) - now) / 86400
    except ValueError:
        return 0.0


def sc_ts(s: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def price_history(token: str) -> list:
    try:
        d = sc.get_json(f"{sc.CLOB}/prices-history", params={"market": token, "interval": "1d", "fidelity": 60})
        return [(float(h["t"]), float(h["p"])) for h in d.get("history", [])]
    except Exception:
        return []


def resolved_markets(market_ids: list) -> dict:
    """{market_id: 1 if YES won, 0 if NO won} for markets that have resolved."""
    out = {}
    for mid in market_ids:
        try:
            m = sc.get_json(f"{sc.GAMMA}/markets/{mid}")
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            if m.get("closed") and sorted(prices) == [0.0, 1.0]:
                out[mid] = int(prices[0] == 1.0)
        except Exception:
            pass
    return out


def report(db_path="paper_G.db") -> str:
    con = sqlite3.connect(db_path)
    cfg = json.loads((con.execute("SELECT v FROM meta WHERE k='config'").fetchone() or ["{}"])[0])
    budget = cfg.get("budget", 50)
    mark = con.execute("SELECT ts, cash, open_value, open_trades FROM marks ORDER BY ts DESC LIMIT 1").fetchone()
    first = con.execute("SELECT MIN(ts) FROM marks").fetchone()[0]
    if not mark:
        return "No paper trading yet."
    total = mark[1] + mark[2]
    closed = con.execute("SELECT COUNT(*), SUM(pnl), SUM(pnl > 0) FROM trades WHERE exit_ts IS NOT NULL").fetchone()
    sig = con.execute("SELECT COUNT(*), SUM(entered) FROM signals").fetchone()
    L = ["# Version G (fade sudden rises): paper results", "",
         f"{(mark[0] - first) / 3600:.1f} hours, budget ${budget:.0f}, ${cfg.get('stake', 5):.0f} per trade", "",
         "| | |", "|---|---|",
         f"| Signals found | {sig[0] or 0} ({sig[1] or 0} traded) |",
         f"| Closed trades | {closed[0] or 0} ({closed[2] or 0} profitable) |",
         f"| Profit on closed trades | ${closed[1] or 0:+.2f} |",
         f"| Open trades | {mark[3]} (worth ${mark[2]:.2f} if sold now) |",
         f"| **Total now** | **${total:.2f} ({total - budget:+.2f})** |", ""]
    rows = con.execute("SELECT question, yes_mid, rise, entry_no, exit_no, reason, pnl FROM trades ORDER BY ts DESC LIMIT 20").fetchall()
    if rows:
        L += ["## Trades", "", "| Market | YES at signal | Rise | NO bought | NO sold | Exit | PnL |", "|---|---|---|---|---|---|---|"]
        for q, mid, rise, en, ex, reason, pnl in rows:
            L.append(f"| {q[:55]} | {mid * 100:.0f}¢ | +{rise * 100:.0f}¢ | {en * 100:.1f}¢ | "
                     f"{'' if ex is None else f'{ex * 100:.1f}¢'} | {reason or 'open'} | {'' if pnl is None else f'${pnl:+.2f}'} |")
        L.append("")
    fu = con.execute("SELECT entered, COUNT(*), AVG(yes_mid - mid_48h) FROM signals WHERE mid_48h IS NOT NULL GROUP BY entered").fetchall()
    if fu:
        L += ["## Did the price fall back within 48 hours? (all signals)", "", "| | Signals | Average fall |", "|---|---|---|"]
        for entered, n, avg in fu:
            L.append(f"| {'traded' if entered else 'not traded'} | {n} | {avg * 100:+.1f}¢ |")
        L.append("")
    reasons = con.execute("SELECT reason, COUNT(*) FROM signals WHERE entered=0 GROUP BY reason ORDER BY COUNT(*) DESC").fetchall()
    if reasons:
        L += ["## Signals not traded", "", "| Reason | Count |", "|---|---|"] + [f"| {r} | {n} |" for r, n in reasons] + [""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Version G: fade sudden rises (paper mode)")
    ap.add_argument("command", choices=["run", "report"])
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--db", default="paper_G.db")
    ap.add_argument("--budget", type=float, default=50.0)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    if args.command == "report":
        text = report(args.db)
        print(text)
        if args.out:
            open(args.out, "w").write(text + "\n")
        return 0
    cfg = ConfigG(budget=args.budget)
    JumpBot(cfg, args.db).run(args.hours)
    print(report(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
