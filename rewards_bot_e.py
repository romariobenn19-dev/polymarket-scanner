#!/usr/bin/env python3
"""
rewards_bot_e.py - Version E of the liquidity-rewards bot. PAPER MODE ONLY (no orders are sent).

    python rewards_bot_e.py run --hours 6 --db paper_E.db
    python rewards_bot_e.py pick
    python rewards_bot_e.py report --db paper_E.db

Version E = version D's protections + version A's appetite + rules aimed at the losses seen
in the overnight test (where nearly all profit and loss came from shares the bots got stuck with):

  From D (rewards_bot_d.py)
    live order books and trades over the websocket, reacting every second; large-trade and
    trade-burst alarm; microprice-centred quotes; markets that lose more than $0.30 are banned
    for 24 h (remembered across restarts); stuck shares are sold at half the usual distance.
  Changed from D
    market choice counts only HALF of the expected loss from a market's normal price swings
    (D counted all of it and ended up in markets that paid almost nothing).
  New in E
    1. Time limit on stuck shares: anything held longer than `max_hold_hours` is sold at the
       best available price (paying the taker fee) instead of waiting for the price to return.
    2. At most `max_stuck` markets with stuck shares at once; with that many, no new orders are
       placed in other markets until one is cleared.
    3. Queue cover: an order is only placed where other traders' orders at the same or a better
       price would be filled first (at least `queue_cover` x our size). Small trades then hit them,
       not us; only big moves reach our order. If no covered price exists within the reward zone,
       the order goes to the far edge of the zone.
    4. Wider quotes when jumpy: if the price moved 1c or more in the last 10 minutes, orders sit
       further from the price.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

import rewards_bot as rb
import rewards_bot_d as rd
import scanner as sc


@dataclass
class ConfigE(rd.ConfigD):
    risk_weight: float = 0.5        # count half of the expected swing loss when picking markets
    max_hold_hours: float = 3.0     # sell stuck shares after this long
    max_stuck: int = 2              # markets with stuck shares at once
    queue_cover: float = 2.0        # others' volume at our price or better, in multiples of our size
    jumpy_range: float = 0.01       # price range that counts as jumpy ...
    jumpy_window: int = 600         # ... within this many seconds
    jumpy_depth: float = 0.75       # quote this far out (fraction of max spread) when jumpy


def taker_fee(shares: float, price: float, rate: float) -> float:
    """Polymarket taker fee: shares x rate x p x (1 - p)."""
    return shares * rate * price * (1 - price)


def covered_bid(book: dict, start: float, floor: float, tick: float, need: float) -> float:
    """Highest price <= start (and > floor) where others already bid at least `need` shares
    at that price or higher. Falls back to the lowest price in the zone."""
    bids = [(float(l["price"]), float(l["size"])) for l in book.get("bids", [])]
    p = start
    while p > floor + 1e-9:
        if sum(s for q, s in bids if q >= p - 1e-9) >= need:
            return round(p, 4)
        p = round(p - tick, 4)
    return round(floor + tick, 4)


def covered_ask(book: dict, start: float, ceil: float, tick: float, need: float) -> float:
    """Lowest price >= start (and < ceil) where others already offer at least `need` shares
    at that price or lower. Falls back to the highest price in the zone."""
    asks = [(float(l["price"]), float(l["size"])) for l in book.get("asks", [])]
    p = start
    while p < ceil - 1e-9:
        if sum(s for q, s in asks if q <= p + 1e-9) >= need:
            return round(p, 4)
        p = round(p + tick, 4)
    return round(ceil - tick, 4)


class PaperBotE(rd.PaperBotD):
    def __init__(self, cfg: ConfigE, db_path="paper_E.db", feed: rd.LiveBooks | None = None):
        self.held_since: dict[str, float] = {}
        super().__init__(cfg, db_path, feed)

    # -- state --
    def extra_state(self) -> dict:
        return {**super().extra_state(), "held_since": self.held_since}

    def load_extra(self, st: dict) -> None:
        super().load_extra(st)
        self.held_since.update(st.get("held_since", {}))

    def holding(self, mid_id: str) -> bool:
        p = self.pos.get(mid_id)
        return bool(p and (p.yes > 0 or p.no > 0))

    def fill(self, mid_id, p, side, price, size, mid, now):
        super().fill(mid_id, p, side, price, size, mid, now)
        if p.yes > 0 or p.no > 0:
            self.held_since.setdefault(mid_id, now)
        else:
            self.held_since.pop(mid_id, None)

    def time_exit(self, mid_id: str, pk: rb.Pick, p: rb.Position, bb: float, ba: float, mid: float, now: float) -> None:
        """Sell stuck shares at the best available price (taker)."""
        before = self.cash
        if p.yes > 0:
            fee = taker_fee(p.yes, bb, pk.fee_rate)
            self.cash += p.yes * bb - fee
            self.con.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?)", (now, mid_id, pk.question, "exit_yes", bb, p.yes, mid))
            p.yes = 0.0
        if p.no > 0:
            no_bid = 1 - ba
            fee = taker_fee(p.no, no_bid, pk.fee_rate)
            self.cash += p.no * no_bid - fee
            self.con.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?)", (now, mid_id, pk.question, "exit_no", no_bid, p.no, mid))
            p.no = 0.0
        p.cost = 0.0
        self.market_cash[mid_id] += self.cash - before
        self.held_since.pop(mid_id, None)
        self.con.execute("INSERT INTO pauses VALUES (?,?,?,?)", (now, mid_id, pk.question, f"time limit: sold stuck shares after {self.cfg.max_hold_hours:g} h"))
        print(f"[{sc.iso(now)}] time limit: sold stuck shares in {pk.question[:50]}", flush=True)

    def cycle(self, dt: float):
        cfg = self.cfg
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if today != self.day:
            self.day, self.day_start_equity, self.stopped_day = today, self.equity(), None
        for mid_id in [i for i in self.pos if i in self.picks]:
            pk, p = self.picks[mid_id], self.pos[mid_id]
            if now - self.feed.updated.get(pk.token, 0) > cfg.stale_seconds:
                fresh = sc.fetch_books([pk.token]).get(pk.token)
                if fresh:
                    self.feed.set_book(pk.token, fresh, now)
            b = self.feed.book(pk.token)
            if not b:
                continue
            bb, ba = rb.best(b)
            if bb is None or ba is None:
                continue
            mid = (bb + ba) / 2
            self.last_mid[mid_id] = mid

            # 1) fills: price moved through our order, or a trade printed at/through it
            trades = self.feed.recent_trades(pk.token, max(dt, 1) + 1, now)
            if p.bid is not None and (ba <= p.bid or mid <= p.bid or any(t[1] <= p.bid for t in trades)):
                self.fill(mid_id, p, "buy_yes", p.bid, p.size, mid, now)
                p.bid = None
            if p.ask is not None and (bb >= p.ask or mid >= p.ask or any(t[1] >= p.ask for t in trades)):
                self.fill(mid_id, p, "sell_yes", p.ask, p.size, mid, now)
                p.ask = None

            # 2) rewards for the time our orders were resting
            share = 0.0
            if p.bid is not None or p.ask is not None:
                s1 = rb.order_score(pk.v, (mid - p.bid) * 100, p.size) if p.bid is not None else 0.0
                s2 = rb.order_score(pk.v, (p.ask - mid) * 100, p.size) if p.ask is not None else 0.0
                ours = rb.q_min(s1, s2, mid)
                q1, q2 = rb.book_scores(b, mid, pk.v)
                others = max(min(q1, q2), max(q1, q2) / rb.REWARD_SINGLE_SIDE_DIVISOR)
                if ours > 0:
                    share = ours / (ours + others)
                    amount = pk.daily_rate * share * dt / 86400
                    self.rewards += amount
                    self.market_rewards[mid_id] += amount
                    self.con.execute("INSERT INTO accruals VALUES (?,?,?,?,?)", (now, mid_id, pk.question, amount, share))

            # 3) time limit on stuck shares
            if self.holding(mid_id) and now - self.held_since.get(mid_id, now) >= cfg.max_hold_hours * 3600:
                p.bid = p.ask = None
                self.time_exit(mid_id, pk, p, bb, ba, mid, now)
                continue

            # 4) guards (as in D)
            hist = [h for h in self.mid_hist.get(mid_id, []) if now - h[0] <= max(cfg.move_window, cfg.jumpy_window)]
            hist.append((now, mid))
            self.mid_hist[mid_id] = hist
            recent = [h[1] for h in hist if now - h[0] <= cfg.move_window]
            move = max(abs(mid - x) for x in recent)
            jumpy = (max(h[1] for h in hist) - min(h[1] for h in hist)) >= cfg.jumpy_range - 1e-9
            reason, minutes = None, cfg.pause_minutes
            alarm = self.trade_alarm(pk, now)
            if move >= cfg.move_pause - 1e-9:
                reason = f"price moved {move * 100:.1f}c in {cfg.move_window}s"
            elif alarm:
                reason, minutes = alarm, cfg.trade_pause_minutes
            elif share > cfg.max_share:
                reason = f"our reward share {share * 100:.0f}% (other makers left)"
            if reason and self.paused_until.get(mid_id, 0) <= now:
                self.paused_until[mid_id] = now + minutes * 60
                self.con.execute("INSERT INTO pauses VALUES (?,?,?,?)", (now, mid_id, pk.question, reason))
                print(f"[{sc.iso(now)}] pause {pk.question[:50]}: {reason}", flush=True)

            # 5) requote
            p.bid = p.ask = None
            if (self.stopped_day == today or mid_id not in self.active or not (0.05 < mid < 0.95)
                    or self.paused_until.get(mid_id, 0) > now):
                continue
            holding = self.holding(mid_id)
            if not holding and sum(1 for i in self.pos if self.holding(i)) >= cfg.max_stuck:
                continue  # enough stuck positions already: no new risk anywhere else
            depth = cfg.quote_depth
            if jumpy:
                depth = max(depth, cfg.jumpy_depth)
            if holding:
                depth *= cfg.exit_depth_factor
            centre = min(max(rd.microprice(b) or mid, bb), ba)
            bid, ask = rb.quotes_for(centre, pk.v, pk.tick, depth, bb, ba)
            v = pk.v / 100
            lo, hi = round(mid - v, 4), round(mid + v, 4)  # reward zone (scored from the midpoint)
            bid, ask = max(bid, round(lo + pk.tick, 4)), min(ask, round(hi - pk.tick, 4))
            size = pk.min_size
            if not holding:  # only new risk needs cover; exits want to be filled
                need = cfg.queue_cover * size
                bid = covered_bid(b, bid, lo, pk.tick, need)
                ask = covered_ask(b, ask, hi, pk.tick, need)
            free = self.cash - sum(self.locked(i, q) for i, q in self.pos.items() if i != mid_id)
            if not holding:
                if free >= bid * size:
                    p.bid, free = bid, free - bid * size
                if not cfg.one_sided and free >= (1 - ask) * size:
                    p.ask = ask
                p.size = size
            elif p.yes > 0:
                p.ask, p.size = ask, p.yes
            else:
                p.bid, p.size = bid, p.no
        eq = self.equity()
        if self.stopped_day != today and self.day_start_equity - eq >= cfg.daily_loss_limit:
            self.stopped_day = today
            print(f"[{sc.iso(now)}] daily loss limit hit (${self.day_start_equity - eq:.2f}); quotes off until tomorrow", flush=True)
        self.con.execute("INSERT INTO cycles VALUES (?,?,?,?,?,?)", (now, eq, self.cash, self.holdings_value(), self.rewards, len(self.active)))
        self.con.commit()
        self.save_state()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Liquidity-rewards bot version E (paper mode)")
    ap.add_argument("command", choices=["run", "report", "pick"])
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--db", default="paper_E.db")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    cfg = ConfigE()
    if args.command == "pick":
        rd.pick_markets_d(cfg)
        return 0
    if args.command == "report":
        text = rb.report(args.db)
        print(text)
        if args.out:
            open(args.out, "w").write(text + "\n")
        return 0
    PaperBotE(cfg, args.db).run(args.hours)
    print(rb.report(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
