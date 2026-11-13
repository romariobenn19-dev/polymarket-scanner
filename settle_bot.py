#!/usr/bin/env python3
"""
settle_bot.py - Version H: buy near-certain outcomes between the deadline and settlement.
PAPER MODE ONLY (no orders are sent).

    python settle_bot.py run --hours 6
    python settle_bot.py report

Why: after a market's deadline passes, the answer is often effectively known, but Polymarket's
settlement takes hours to days. Meanwhile the favourite side often still trades at 90-99c.
In 277 such cases from the last 12 months (research/FINDINGS.md), buying the favourite won 99.3%
of the time, +1.7% per dollar, about 2 days of waiting on average. Both losses were markets whose
final figure could still move (a temperature range, a vote count).

Every 10 minutes:
  1. Finds open markets whose deadline passed at least 6 hours ago, still taking orders, not
     disputed, with $5k+ total volume.
  2. Skips types whose final figure can still move: weather/temperature, vote counts/turnout,
     "between X and Y" ranges.
  3. Prices the favourite side from the live order book: 90-99c, spread <= 3c, enough shares for
     the stake, taker fee included (max 98.5c). Markets stuck over 7 days are left alone. Elections need 96c+ (counts can still swing).
     $10 per trade, up to $50 in use, one trade per event, safest (highest-priced) first.
  4. Holds until settlement (paid $1 or $0). Safety exit: if the side held falls below 70c,
     sells at the order book to limit the loss.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import jump_bot as jb
import scanner as sc

H = 3600


@dataclass
class ConfigH:
    budget: float = 50.0
    stake: float = 10.0
    min_price: float = 0.90
    max_price: float = 0.985             # above this the gain is too small to tie money up
    max_spread: float = 0.03
    min_volume: float = 5_000.0
    min_hours_past: float = 6.0          # sports 'deadlines' are kick-off times; give games time to finish
    election_min_price: float = 0.96     # elections still being counted are only bought when clearly decided
    max_days_past: float = 7.0           # stuck longer = waiting on certification/disputes
    safety_exit: float = 0.70
    poll_seconds: int = 600


ELECTION = re.compile(r"election|house race|senate race|mayor|governor|coalition|primary|parliament|seats", re.I)
RISKY = re.compile(r"temperature|°|weather|rain|snow|vote|turnout|between \$?[\d,.]+|\d+\s*-\s*\d+\s*(°|%|seats)", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (ts REAL, market_id TEXT, question TEXT, side TEXT, price REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, ts REAL, market_id TEXT, event_id TEXT, question TEXT,
    token TEXT, side TEXT, fee_rate REAL, hours_past REAL, shares REAL, entry REAL, entry_fee REAL,
    exit_ts REAL, exit REAL, exit_fee REAL, reason TEXT, pnl REAL);
CREATE TABLE IF NOT EXISTS marks (ts REAL, cash REAL, open_value REAL, open_trades INT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def favourite(book: dict, stake: float):
    """(side, shares, avg price) for buying the favoured side with `stake`, or None if too thin.
    YES is bought from the YES asks; NO is bought by selling YES into the YES bids."""
    bids, asks = jb.yes_levels(book)
    if not bids or not asks:
        return None
    if bids[0][0] + asks[0][0] > 1:          # YES favoured
        shares = stake / asks[0][0]
        avg = jb.walk(asks, shares)
        return None if avg is None else ("YES", shares, avg)
    got = jb.buy_no(book, stake)
    return None if got is None else ("NO", got[0], got[1])


def side_bid(book: dict, side: str, shares: float):
    """Average price when selling `shares` of `side` now."""
    bids, asks = jb.yes_levels(book)
    if side == "YES":
        return jb.walk(bids, shares)
    return jb.sell_no(book, shares)


def side_mid(book: dict, side: str):
    bids, asks = jb.yes_levels(book)
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    return mid if side == "YES" else 1 - mid


class SettleBot:
    def __init__(self, cfg: ConfigH, db_path="paper_H.db"):
        self.cfg = cfg
        self.con = sqlite3.connect(db_path)
        self.con.executescript(SCHEMA)
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('config', ?)", (json.dumps(asdict(cfg)),))
        row = self.con.execute("SELECT v FROM meta WHERE k='cash'").fetchone()
        self.cash = float(row[0]) if row else cfg.budget
        self.con.commit()

    def open_trades(self):
        return self.con.execute("SELECT id, market_id, event_id, token, side, shares, entry, entry_fee, fee_rate, ts "
                                "FROM trades WHERE exit_ts IS NULL").fetchall()

    def traded_before(self, market_id: str) -> bool:
        return self.con.execute("SELECT 1 FROM trades WHERE market_id=?", (market_id,)).fetchone() is not None

    # -- finding candidates --
    def candidates(self, markets: list, now: float) -> list:
        cfg, out = self.cfg, []
        for m in markets:
            if not m.get("acceptingOrders") or m.get("closed") or not sc.yes_token(m):
                continue
            if m.get("umaResolutionStatus") == "disputed":
                continue
            try:
                end = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                continue
            hours_past = (now - end) / H
            if not (cfg.min_hours_past <= hours_past <= cfg.max_days_past * 24):
                continue
            if (sc._f(m.get("volumeNum")) or 0) < cfg.min_volume or self.traded_before(m["id"]):
                continue
            bb, ba = sc._f(m.get("bestBid")), sc._f(m.get("bestAsk"))
            if bb is None or ba is None:
                continue
            fav = ba if bb + ba > 1 else 1 - bb
            if not (cfg.min_price - 0.02 <= fav <= cfg.max_price):   # rough prefilter; the book decides
                continue
            ev = (m.get("events") or [{}])[0]
            fee = float((m.get("feeSchedule") or {}).get("rate") or 0) if m.get("feesEnabled") else 0.0
            out.append({"market_id": m["id"], "event_id": str(ev.get("id", m["id"])), "question": m.get("question", ""),
                        "token": sc.yes_token(m), "fee_rate": fee, "hours_past": hours_past, "approx": fav})
        # 95-98.5c first (best record in the study), safest first; 90-95c only with spare budget
        return sorted(out, key=lambda c: (c["approx"] < 0.95, -c["approx"]))

    def consider(self, c: dict, book: dict, now: float) -> bool:
        cfg = self.cfg
        bids, asks = jb.yes_levels(book)
        reason = None
        if RISKY.search(c["question"]):
            reason = "final figure can still move (weather / votes / range)"
        elif not bids or not asks:
            reason = "empty order book"
        elif asks[0][0] - bids[0][0] > cfg.max_spread:
            reason = "spread over 3c (thin market)"
        elif c["event_id"] in {r[2] for r in self.open_trades()}:
            reason = "already holding this event"
        elif sum(r[5] * r[6] for r in self.open_trades()) + cfg.stake > cfg.budget or self.cash < 0.9 * cfg.stake:
            reason = "budget in use"
        got = None if reason else favourite(book, min(cfg.stake, self.cash - 0.05))
        if not reason and got is None:
            reason = "not enough shares on offer"
        if not reason:
            side, shares, price = got
            if not (cfg.min_price <= price <= cfg.max_price):
                reason = f"favourite costs {price * 100:.1f}c"
            elif ELECTION.search(c["question"]) and price < cfg.election_min_price:
                reason = "election not clearly decided (under 96c)"
        if reason:
            self.con.execute("INSERT INTO seen VALUES (?,?,?,?,?,?)", (now, c["market_id"], c["question"], "", c["approx"], reason))
            return False
        fee = jb.taker_fee(shares, price, c["fee_rate"])
        self.cash -= shares * price + fee
        self.con.execute("""INSERT INTO trades (ts, market_id, event_id, question, token, side, fee_rate, hours_past,
                            shares, entry, entry_fee) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                         (now, c["market_id"], c["event_id"], c["question"], c["token"], side, c["fee_rate"], c["hours_past"],
                          shares, price, fee))
        self.con.execute("INSERT INTO seen VALUES (?,?,?,?,?,?)", (now, c["market_id"], c["question"], side, price, "bought"))
        print(f"[{sc.iso(now)}] BUY {side} {shares:.1f} @ {price:.3f} | {c['hours_past']:.0f}h past deadline | {c['question'][:60]}", flush=True)
        return True

    # -- holding --
    def manage(self, books: dict, settled: dict, now: float) -> float:
        cfg, open_value = self.cfg, 0.0
        for tid, mid_id, ev, token, side, shares, entry, entry_fee, fee_rate, ts in self.open_trades():
            if mid_id in settled:
                yes_won = settled[mid_id]
                exit_price = 1.0 if (yes_won == 1) == (side == "YES") else 0.0
                fee, reason = 0.0, "settled"
            else:
                b = books.get(token)
                mid = side_mid(b, side) if b else None
                bid = side_bid(b, side, shares) if b else None
                if mid is None or bid is None:
                    open_value += shares * entry        # no book: value at cost
                    continue
                if mid >= cfg.safety_exit:
                    open_value += shares * bid
                    continue
                exit_price, fee, reason = bid, jb.taker_fee(shares, bid, fee_rate), "safety exit (price fell below 70c)"
            pnl = shares * (exit_price - entry) - entry_fee - fee
            self.cash += shares * exit_price - fee
            self.con.execute("UPDATE trades SET exit_ts=?, exit=?, exit_fee=?, reason=?, pnl=? WHERE id=?",
                             (now, exit_price, fee, reason, pnl, tid))
            print(f"[{sc.iso(now)}] {reason.upper()} trade {tid}: {side} {entry:.3f} -> {exit_price:.3f}, PnL ${pnl:+.2f}", flush=True)
        return open_value

    def poll(self) -> None:
        now = time.time()
        markets = past_deadline_markets(now, self.cfg.max_days_past)
        cands = self.candidates(markets, now)
        held = self.open_trades()
        tokens = list({c["token"] for c in cands} | {r[3] for r in held})
        books = sc.fetch_books(tokens) if tokens else {}
        for c in cands:
            if c["token"] in books:
                self.consider(c, books[c["token"]], now)
        settled = jb.resolved_markets([r[1] for r in self.open_trades()])
        open_value = self.manage(books, settled, now)
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('cash', ?)", (str(self.cash),))
        n_open = len(self.open_trades())
        self.con.execute("INSERT INTO marks VALUES (?,?,?,?)", (now, self.cash, open_value, n_open))
        self.con.commit()
        print(f"[{sc.iso(now)}] {len(markets)} past-deadline markets, {len(cands)} candidates | cash ${self.cash:.2f} | "
              f"open {n_open} worth ${open_value:.2f} | total ${self.cash + open_value:.2f}", flush=True)

    def run(self, hours: float) -> None:
        end = time.time() + hours * H
        while time.time() < end:
            started = time.time()
            try:
                self.poll()
            except Exception as e:
                print(f"[{sc.iso(started)}] poll failed: {e}", file=sys.stderr, flush=True)
            time.sleep(max(0, self.cfg.poll_seconds - (time.time() - started)))


def past_deadline_markets(now: float, max_days: float) -> list:
    """Open markets whose end date has passed (cursor paging)."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    params = {"closed": "false", "limit": 100,
              "end_date_max": datetime.fromtimestamp(now, timezone.utc).strftime(fmt),
              "end_date_min": datetime.fromtimestamp(now - max_days * 86400, timezone.utc).strftime(fmt)}
    out, cursor = [], None
    for _ in range(300):
        if cursor:
            params["after_cursor"] = cursor
        d = sc.get_json(f"{sc.GAMMA}/markets/keyset", params=params)
        page = d.get("markets") or []
        out += page
        cursor = d.get("next_cursor")
        if not cursor or not page:
            break
    return out


def report(db_path="paper_H.db") -> str:
    con = sqlite3.connect(db_path)
    cfg = json.loads((con.execute("SELECT v FROM meta WHERE k='config'").fetchone() or ["{}"])[0])
    budget = cfg.get("budget", 50)
    mark = con.execute("SELECT ts, cash, open_value, open_trades FROM marks ORDER BY ts DESC LIMIT 1").fetchone()
    first = con.execute("SELECT MIN(ts) FROM marks").fetchone()[0]
    if not mark:
        return "No paper trading yet."
    total = mark[1] + mark[2]
    done = con.execute("SELECT COUNT(*), SUM(pnl), SUM(pnl > 0), AVG((exit_ts - ts) / 86400.0) FROM trades WHERE exit_ts IS NOT NULL").fetchone()
    L = ["# Version H (buy near-certain outcomes before settlement): paper results", "",
         f"{(mark[0] - first) / 3600:.1f} hours, budget ${budget:.0f}, ${cfg.get('stake', 10):.0f} per trade", "",
         "| | |", "|---|---|",
         f"| Trades settled or exited | {done[0] or 0} ({done[2] or 0} profitable, average {done[3] or 0:.1f} days held) |",
         f"| Profit on those | ${done[1] or 0:+.2f} |",
         f"| Open trades | {mark[3]} (worth ${mark[2]:.2f} at today's prices) |",
         f"| **Total now** | **${total:.2f} ({total - budget:+.2f})** |", ""]
    rows = con.execute("SELECT question, side, entry, hours_past, exit, reason, pnl FROM trades ORDER BY ts DESC LIMIT 25").fetchall()
    if rows:
        L += ["## Trades", "", "| Market | Side | Paid | Hours past deadline | Result | PnL |", "|---|---|---|---|---|---|"]
        for q, side, entry, hp, ex, reason, pnl in rows:
            L.append(f"| {q[:60]} | {side} | {entry * 100:.1f}¢ | {hp:.0f} | {reason or 'waiting for settlement'} | "
                     f"{'' if pnl is None else f'${pnl:+.2f}'} |")
        L.append("")
    reasons = con.execute("SELECT reason, COUNT(DISTINCT market_id) FROM seen WHERE reason != 'bought' GROUP BY reason ORDER BY 2 DESC LIMIT 10").fetchall()
    if reasons:
        L += ["## Skipped", "", "| Reason | Markets |", "|---|---|"] + [f"| {r} | {n} |" for r, n in reasons] + [""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Version H: buy near-certain outcomes before settlement (paper mode)")
    ap.add_argument("command", choices=["run", "report"])
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--db", default="paper_H.db")
    ap.add_argument("--budget", type=float, default=50.0)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    if args.command == "report":
        text = report(args.db)
        print(text)
        if args.out:
            open(args.out, "w").write(text + "\n")
        return 0
    SettleBot(ConfigH(budget=args.budget), args.db).run(args.hours)
    print(report(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
