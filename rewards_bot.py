#!/usr/bin/env python3
"""
rewards_bot.py - Polymarket liquidity-rewards market maker. PAPER MODE ONLY (no orders are sent).

    python rewards_bot.py run --hours 6        paper-trade against the live order books
    python rewards_bot.py report               results so far
    python rewards_bot.py pick                 show which markets it would quote right now

Strategy
  Polymarket pays a daily amount on some markets to makers whose resting orders sit within
  `max spread` of the midpoint. The bot picks the best-paying markets it can afford, keeps a
  buy order a little below the midpoint and a sell order a little above it (a sell of YES is
  placed as a buy of NO, so no shares are needed), and moves them as the price moves.

  It earns:  its share of each market's daily reward (+ the spread when both sides fill)
  It loses:  when the price moves through its order, it is left holding shares bought
             at the old price (adverse selection). Paper mode measures both.

How the paper test works
  * Every cycle it reads the real order book for each market it quotes.
  * Reward share each minute = our score / (our score + everyone else's score), using
    Polymarket's scoring: an order at distance s (cents) from the midpoint with max spread v
    scores ((v - s) / v)^2 x size; one-sided quoting counts 1/C (see REWARD_SINGLE_SIDE_DIVISOR).
  * A paper order is treated as filled when the market price moves through it (the best
    price on the other side reaches it). That is the costly kind of fill; harmless fills
    where the price comes back are not counted, so the spread income is understated.
  * Holdings are valued at the midpoint. A YES + NO pair is merged back into $1.
  Simplifications: our orders do not change other traders' behaviour; queue position is
  ignored; reward payouts happen daily in reality but are accrued continuously here.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import scanner as sc  # shared Polymarket data helpers

# --------------------------------------------------------------------------- #
# Settings for a $50 account
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    budget: float = 50.0               # total USDC the bot may use
    max_per_market: float = 25.0       # never commit more than this to one market
    max_markets: int = 2               # $50 covers about two markets at a 20-share minimum
    daily_loss_limit: float = 7.5      # stop for the day after losing 15% of the budget
    inventory_limit: float = 0.6       # stop buying one side once holdings use 60% of a market's allowance
    quote_depth: float = 0.5           # quote at this fraction of max spread from the midpoint (0.5 = halfway)
    min_mid: float = 0.15              # skip near-certain markets (prices jump at the end)
    max_mid: float = 0.85
    min_days_left: float = 3.0         # skip markets that resolve soon
    min_volume24h: float = 500.0       # skip dead markets
    cycle_seconds: int = 30            # requote every 30 s
    reselect_minutes: int = 60         # re-pick markets every hour
    skip_words: tuple = (" vs. ", " vs ", "O/U", "Spread:", "Up or Down", "Handicap")  # live sports / crypto ticks


REWARD_SINGLE_SIDE_DIVISOR = 3.0  # Polymarket's "c"; one-sided liquidity scores 1/c (check the official settings page)

# --------------------------------------------------------------------------- #
# Reward scoring
# --------------------------------------------------------------------------- #


def order_score(v_cents: float, s_cents: float, size: float) -> float:
    if s_cents < 0 or s_cents >= v_cents:
        return 0.0
    return ((v_cents - s_cents) / v_cents) ** 2 * size


def q_min(q_one: float, q_two: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q_one, q_two), max(q_one, q_two) / REWARD_SINGLE_SIDE_DIVISOR)
    return min(q_one, q_two)


def book_scores(book: dict, mid: float, v: float) -> tuple[float, float]:
    """Everyone else's scores: Q_one from YES bids, Q_two from YES asks (= NO bids)."""
    q1 = sum(order_score(v, (mid - float(l["price"])) * 100, float(l["size"])) for l in book.get("bids", []))
    q2 = sum(order_score(v, (float(l["price"]) - mid) * 100, float(l["size"])) for l in book.get("asks", []))
    return q1, q2


def best(book: dict):
    bids, asks = book.get("bids") or [], book.get("asks") or []
    bb = float(bids[-1]["price"]) if bids else None
    ba = float(asks[-1]["price"]) if asks else None
    return bb, ba


# --------------------------------------------------------------------------- #
# Market selection
# --------------------------------------------------------------------------- #


@dataclass
class Pick:
    market_id: str
    question: str
    token: str
    daily_rate: float
    v: float            # max spread, cents
    min_size: float
    tick: float
    end: str
    est_daily: float = 0.0
    cost: float = 0.0


def days_left(end_iso: str | None) -> float:
    if not end_iso:
        return 0.0
    try:
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (end - datetime.now(timezone.utc)).total_seconds() / 86400


def quotes_for(mid: float, v: float, tick: float, depth: float, bb, ba):
    d = max(tick, depth * v / 100)
    bid = math.floor((mid - d) / tick + 1e-9) * tick
    ask = math.ceil((mid + d) / tick - 1e-9) * tick
    if ba is not None and bid >= ba:      # never cross the book (that would be a taker order)
        bid = ba - tick
    if bb is not None and ask <= bb:
        ask = bb + tick
    return round(bid, 4), round(ask, 4)


def candidates(cfg: Config, events: list[dict]) -> list[Pick]:
    out = []
    for e in events:
        for m in e.get("markets") or []:
            if not sc.tradable(m) or not sc.yes_token(m):
                continue
            rate = sum(float(r.get("rewardsDailyRate") or 0) for r in (m.get("clobRewards") or []))
            bb, ba = sc._f(m.get("bestBid")), sc._f(m.get("bestAsk"))
            v, ms = sc._f(m.get("rewardsMaxSpread")), sc._f(m.get("rewardsMinSize"))
            if rate <= 0 or not v or not ms or bb is None or ba is None:
                continue
            mid = (bb + ba) / 2
            q = m.get("question", "")
            if not (cfg.min_mid <= mid <= cfg.max_mid) or (ba - bb) * 100 > v:
                continue
            if days_left(m.get("endDate")) < cfg.min_days_left:
                continue
            if (sc._f(m.get("volume24hrClob")) or sc._f(m.get("volume24hr")) or 0) < cfg.min_volume24h:
                continue
            if any(w in q for w in cfg.skip_words):
                continue
            cost = ms * (1 - cfg.quote_depth * v / 100 * 2)  # buy YES + buy NO at min size ≈ min_size x (1 - 2d)
            if cost > cfg.max_per_market:
                continue
            out.append(Pick(m["id"], q, sc.yes_token(m), rate, v, ms, float(m.get("orderPriceMinTickSize") or 0.01),
                            m.get("endDate", ""), cost=cost))
    return out


def pick_markets(cfg: Config, verbose=True) -> list[Pick]:
    events = sc.fetch_events(min_liquidity=500)
    cands = sorted(candidates(cfg, events), key=lambda p: -p.daily_rate)[:60]
    books = sc.fetch_books([p.token for p in cands])
    for p in cands:
        b = books.get(p.token)
        if not b:
            continue
        bb, ba = best(b)
        if bb is None or ba is None:
            continue
        mid = (bb + ba) / 2
        bid, ask = quotes_for(mid, p.v, p.tick, cfg.quote_depth, bb, ba)
        ours = q_min(order_score(p.v, (mid - bid) * 100, p.min_size), order_score(p.v, (ask - mid) * 100, p.min_size), mid)
        q1, q2 = book_scores(b, mid, p.v)
        others = max(min(q1, q2), max(q1, q2) / REWARD_SINGLE_SIDE_DIVISOR)
        p.est_daily = p.daily_rate * ours / (ours + others) if ours > 0 else 0.0
    ranked = sorted([p for p in cands if p.est_daily > 0], key=lambda p: -p.est_daily / max(p.cost, 1))
    chosen, used = [], 0.0
    for p in ranked:
        if len(chosen) >= cfg.max_markets:
            break
        if used + p.cost <= cfg.budget:
            chosen.append(p)
            used += p.cost
    if verbose:
        print(f"{len(cands)} reward markets fit the rules; chose {len(chosen)} (uses ${used:.2f} of ${cfg.budget:.0f}):")
        for p in chosen:
            print(f"  ${p.daily_rate:>5.0f}/day pool, est. our share ${p.est_daily:5.2f}/day, needs ${p.cost:5.2f}  {p.question[:70]}")
    return chosen


# --------------------------------------------------------------------------- #
# Paper trading
# --------------------------------------------------------------------------- #


@dataclass
class Position:
    yes: float = 0.0
    no: float = 0.0
    cost: float = 0.0          # cash spent on shares still held
    bid: float | None = None   # resting paper orders
    ask: float | None = None
    size: float = 0.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (ts REAL, equity REAL, cash REAL, holdings REAL, rewards REAL, markets INT);
CREATE TABLE IF NOT EXISTS fills (ts REAL, market_id TEXT, question TEXT, side TEXT, price REAL, size REAL, mid_after REAL);
CREATE TABLE IF NOT EXISTS accruals (ts REAL, market_id TEXT, question TEXT, amount REAL, share REAL);
CREATE TABLE IF NOT EXISTS picks (ts REAL, market_id TEXT, question TEXT, daily_rate REAL, est_daily REAL, cost REAL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class PaperBot:
    def __init__(self, cfg: Config, db_path="paper.db"):
        self.cfg = cfg
        self.con = sqlite3.connect(db_path)
        self.con.executescript(SCHEMA)
        self.cash = cfg.budget
        self.rewards = 0.0
        self.pos: dict[str, Position] = {}
        self.picks: dict[str, Pick] = {}
        self.last_mid: dict[str, float] = {}
        self.stopped_day: str | None = None
        self.day_start_equity = cfg.budget
        self.day = None
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('config', ?)", (json.dumps(asdict(cfg)),))
        self.con.execute("INSERT OR IGNORE INTO meta VALUES ('started', ?)", (str(time.time()),))
        self.con.commit()

    def holdings_value(self) -> float:
        return sum(p.yes * self.last_mid.get(mid, 0.5) + p.no * (1 - self.last_mid.get(mid, 0.5)) for mid, p in self.pos.items())

    def equity(self) -> float:
        return self.cash + self.holdings_value() + self.rewards

    def locked(self, mid_id: str, p: Position) -> float:
        lock = 0.0
        if p.bid is not None:
            lock += p.bid * p.size
        if p.ask is not None:
            lock += (1 - p.ask) * p.size
        return lock

    def reselect(self):
        new = {p.market_id: p for p in pick_markets(self.cfg)}
        for mid_id in list(self.picks):
            if mid_id not in new:  # stop quoting; keep any holdings until they can be sold
                self.pos[mid_id].bid = self.pos[mid_id].ask = None
        for mid_id, p in new.items():
            self.picks[mid_id] = p
            self.pos.setdefault(mid_id, Position())
            self.con.execute("INSERT INTO picks VALUES (?,?,?,?,?,?)", (time.time(), mid_id, p.question, p.daily_rate, p.est_daily, p.cost))
        self.active = set(new)
        self.con.commit()

    def fill(self, mid_id: str, p: Position, side: str, price: float, size: float, mid: float, now: float):
        if side == "buy_yes":
            p.yes += size
            self.cash -= price * size
            p.cost += price * size
        else:  # our "sell" is a buy of NO at 1 - price
            p.no += size
            self.cash -= (1 - price) * size
            p.cost += (1 - price) * size
        pairs = min(p.yes, p.no)
        if pairs > 0:  # YES + NO = $1: merge back to cash
            avg = p.cost / (p.yes + p.no)
            p.yes -= pairs
            p.no -= pairs
            p.cost -= avg * 2 * pairs
            self.cash += pairs
        self.con.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?)",
                         (now, mid_id, self.picks[mid_id].question if mid_id in self.picks else "", side, price, size, mid))

    def cycle(self, dt: float):
        cfg = self.cfg
        now = time.time()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.day_start_equity, self.stopped_day = today, self.equity(), None
        ids = [i for i in self.pos if i in self.picks]
        books = sc.fetch_books([self.picks[i].token for i in ids]) if ids else {}
        for mid_id in ids:
            pk, p = self.picks[mid_id], self.pos[mid_id]
            b = books.get(pk.token)
            if not b:
                continue
            bb, ba = best(b)
            if bb is None or ba is None:
                continue
            mid = (bb + ba) / 2
            self.last_mid[mid_id] = mid
            # 1) did the price move through our resting orders since last cycle?
            if p.bid is not None and (ba <= p.bid or mid <= p.bid):
                self.fill(mid_id, p, "buy_yes", p.bid, p.size, mid, now)
                p.bid = None
            if p.ask is not None and (bb >= p.ask or mid >= p.ask):
                self.fill(mid_id, p, "sell_yes", p.ask, p.size, mid, now)
                p.ask = None
            # 2) rewards for the time our orders were resting
            v = pk.v
            if p.bid is not None or p.ask is not None:
                s1 = order_score(v, (mid - p.bid) * 100, p.size) if p.bid is not None else 0.0
                s2 = order_score(v, (p.ask - mid) * 100, p.size) if p.ask is not None else 0.0
                ours = q_min(s1, s2, mid)
                q1, q2 = book_scores(b, mid, v)
                others = max(min(q1, q2), max(q1, q2) / REWARD_SINGLE_SIDE_DIVISOR)
                if ours > 0:
                    share = ours / (ours + others)
                    amount = pk.daily_rate * share * dt / 86400
                    self.rewards += amount
                    self.con.execute("INSERT INTO accruals VALUES (?,?,?,?,?)", (now, mid_id, pk.question, amount, share))
            # 3) requote around the new midpoint (or stop)
            p.bid = p.ask = None
            if self.stopped_day == today or mid_id not in self.active or not (0.05 < mid < 0.95):
                continue
            bid, ask = quotes_for(mid, v, pk.tick, cfg.quote_depth, bb, ba)
            size = pk.min_size
            allowance = cfg.max_per_market
            free = self.cash - sum(self.locked(i, q) for i, q in self.pos.items() if i != mid_id)
            if p.yes * mid < cfg.inventory_limit * allowance and free >= bid * size:
                p.bid, free = bid, free - bid * size
            if p.no * (1 - mid) < cfg.inventory_limit * allowance and free >= (1 - ask) * size:
                p.ask = ask
            p.size = size
        eq = self.equity()
        if self.stopped_day != today and self.day_start_equity - eq >= cfg.daily_loss_limit:
            self.stopped_day = today
            print(f"[{sc.iso(now)}] daily loss limit hit (${self.day_start_equity - eq:.2f}); quotes off until tomorrow")
        self.con.execute("INSERT INTO cycles VALUES (?,?,?,?,?,?)", (now, eq, self.cash, self.holdings_value(), self.rewards, len(self.active)))
        self.con.commit()

    def run(self, hours: float):
        cfg = self.cfg
        end = time.time() + hours * 3600
        self.active = set()
        next_pick = 0.0
        last = time.time()
        while time.time() < end:
            started = time.time()
            try:
                if started >= next_pick:
                    self.reselect()
                    next_pick = started + cfg.reselect_minutes * 60
                self.cycle(dt=started - last if last < started else cfg.cycle_seconds)
                last = started
                print(f"[{sc.iso(started)}] equity ${self.equity():.2f} | rewards ${self.rewards:.3f} | holdings ${self.holdings_value():.2f}", flush=True)
            except Exception as e:
                print(f"[{sc.iso(started)}] cycle failed: {e}", file=sys.stderr, flush=True)
            time.sleep(max(0, cfg.cycle_seconds - (time.time() - started)))


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def report(db_path="paper.db") -> str:
    con = sqlite3.connect(db_path)
    cfg = json.loads((con.execute("SELECT v FROM meta WHERE k='config'").fetchone() or ["{}"])[0])
    first = con.execute("SELECT ts, equity FROM cycles ORDER BY ts LIMIT 1").fetchone()
    last = con.execute("SELECT ts, equity, cash, holdings, rewards FROM cycles ORDER BY ts DESC LIMIT 1").fetchone()
    if not first:
        return "No paper trading yet. Run: python rewards_bot.py run --hours 6"
    budget = cfg.get("budget", 50)
    hours = (last[0] - first[0]) / 3600
    pnl = last[1] - budget
    trading = pnl - last[4]
    per_day = pnl / hours * 24 if hours > 0.25 else float("nan")
    L = ["# Rewards bot: paper results", "",
         f"{hours:.1f} hours, {sc.iso(first[0])} → {sc.iso(last[0])} UTC, budget ${budget:.0f}", "",
         "| | |", "|---|---|",
         f"| Rewards earned | ${last[4]:+.2f} |",
         f"| Gain/loss on shares bought and sold (valued at midpoint) | ${trading:+.2f} |",
         f"| **Net result** | **${pnl:+.2f}** ({pnl / budget * 100:+.1f}%) |",
         f"| At this rate, per day | ${per_day:+.2f} |",
         f"| Still holding (at midpoint) | ${last[3]:.2f} |", ""]
    L += ["## Markets quoted", "", "| Market | Rewards | Fills | Share of rewards (avg) |", "|---|---|---|---|"]
    for r in con.execute("""SELECT a.question, SUM(a.amount), AVG(a.share),
                              (SELECT COUNT(*) FROM fills f WHERE f.market_id = a.market_id)
                            FROM accruals a GROUP BY a.market_id ORDER BY SUM(a.amount) DESC"""):
        L.append(f"| {r[0][:70]} | ${r[1]:.3f} | {r[3]} | {r[2] * 100:.1f}% |")
    fills = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    L += ["", f"{fills} paper fills (price moved through our order).",
          "", "_Paper test against live order books; see rewards_bot.py for the assumptions. Hours of data say little: "
          "judge it over several days that include news-driven moves._"]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Polymarket liquidity-rewards bot (paper mode)")
    ap.add_argument("command", choices=["run", "report", "pick"])
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--budget", type=float, help="override the $50 budget")
    ap.add_argument("--db", default="paper.db")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    cfg = Config()
    if args.budget:
        cfg.budget = args.budget
        cfg.max_per_market = args.budget / 2
        cfg.daily_loss_limit = args.budget * 0.15
    if args.command == "pick":
        pick_markets(cfg)
        return 0
    if args.command == "report":
        text = report(args.db)
        print(text)
        if args.out:
            open(args.out, "w").write(text + "\n")
        return 0
    PaperBot(cfg, args.db).run(args.hours)
    print(report(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
