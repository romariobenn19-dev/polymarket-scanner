#!/usr/bin/env python3
"""
rewards_bot_f.py - Version F of the liquidity-rewards bot. PAPER MODE ONLY (no orders are sent).

    python rewards_bot_f.py run --hours 6 --db paper_F.db
    python rewards_bot_f.py pick
    python rewards_bot_f.py report --db paper_F.db

Version E plus eight changes (see docs/HOW_IT_WORKS.md for the full rules):

  1. Realistic paper fills: each order remembers how many shares were already waiting at its
     price. Trades at that price use up that queue first; the order only fills once the queue
     ahead is gone, a trade happens at a worse price, or the other side crosses it. People
     ahead cancelling moves the order up the queue.
  2. Polymarket does not pay reward amounts under $1: a market is only used if it is expected
     to earn at least $1.25 a day. If minimum-size orders don't get there, the order size is raised
     (up to 4x, using at most 80% of the budget in one market) until they do.
  3. Adjusted midpoint: book levels smaller than the market's minimum reward size are ignored
     when working out the midpoint, the microprice and other traders' reward scores
     (Polymarket scores from this "size-cutoff-adjusted midpoint").
  4. No near-worthless orders: a new order must have queue cover within 80% of the reward
     zone; otherwise that side is not quoted (E fell back to the far edge, scoring ~0).
  5. One market per event (e.g. different NVIDIA CEO candidates are one bet).
  6. Selling stuck shares: first a waiting order at break-even (purchase price minus rewards
     already earned in that market), stepping one tick lower every 15 minutes after 90 minutes;
     only at 3 hours a forced sale at the best price, paying the taker fee.
  7. Losses judged against earnings: a market that loses money gets orders 0.10 of the zone
     further out; it is only banned (24 h) once it has lost more than a day's expected rewards.
  8. Market choice uses the last day of minute-by-minute prices (catches spikes hidden in hourly
     data) and the cost of a possible forced sale (the market's own taker fee rate). Markets with
     a finer price step score better automatically, because orders land closer to the target.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass

import rewards_bot as rb
import rewards_bot_d as rd
import rewards_bot_e as re_
import scanner as sc


@dataclass
class ConfigF(re_.ConfigE):
    min_daily_per_market: float = 1.25   # Polymarket pays nothing under $1 per day
    cover_zone: float = 0.80             # covered price must be within 80% of max spread
    ramp_start_minutes: float = 90.0     # after this, lower the exit price ...
    ramp_step_minutes: float = 15.0      # ... one tick every 15 minutes
    loss_widen: float = 0.10             # orders this much further out after a market loses money
    max_widen: float = 0.30
    max_size_mult: int = 4               # orders up to 4x the minimum size ...
    max_single_share: float = 0.8        # ... using at most 80% of the budget in one market


# --------------------------------------------------------------------------- #
# Book helpers
# --------------------------------------------------------------------------- #


def filtered(book: dict, min_size: float) -> dict:
    """Book without levels smaller than the minimum reward size (Polymarket's size cutoff)."""
    return {"bids": [l for l in book.get("bids", []) if float(l["size"]) >= min_size],
            "asks": [l for l in book.get("asks", []) if float(l["size"]) >= min_size]}


def level_size(book: dict, side: str, price: float) -> float:
    return sum(float(l["size"]) for l in book.get(side, []) if abs(float(l["price"]) - price) < 1e-9)


def minute_swing_loss(prices: list[float], distance: float, size: float) -> float:
    """Daily loss estimate from minute-by-minute moves bigger than our distance."""
    if len(prices) < 120:
        return float("inf")
    moves = [abs(b - a) for a, b in zip(prices, prices[1:])]
    excess = sum(max(0.0, m - distance) for m in moves) * size
    return excess / (len(moves) / 1440)


def minute_history(token: str) -> list[float]:
    try:
        d = sc.get_json(f"{sc.CLOB}/prices-history", params={"market": token, "interval": "1d", "fidelity": 1})
        return [float(h["p"]) for h in d.get("history", [])]
    except Exception:
        return []


def plan_quotes(book: dict, pk: rb.Pick, depth: float, cfg: ConfigF, size: float | None = None):
    """Where version F would quote right now. Returns (mid, bid or None, ask or None, adjusted book)."""
    adj = filtered(book, pk.min_size)
    bb, ba = rb.best(adj)
    rbb, rba = rb.best(book)
    if bb is None or ba is None:
        bb, ba = rbb, rba
    if bb is None or ba is None:
        return None, None, None, adj
    mid = (bb + ba) / 2
    centre = min(max(rd.microprice(adj) or mid, bb), ba)
    bid, ask = rb.quotes_for(centre, pk.v, pk.tick, depth, rbb, rba)
    v = pk.v / 100
    zone = cfg.cover_zone * v
    need = cfg.queue_cover * (size or pk.min_size)
    lo, hi = round(mid - zone, 4), round(mid + zone, 4)
    bid = max(bid, round(math.ceil(lo / pk.tick - 1e-9) * pk.tick, 4))
    ask = min(ask, round(math.floor(hi / pk.tick + 1e-9) * pk.tick, 4))
    bid = cover_price(book, "bids", bid, lo, pk.tick, need)
    ask = cover_price(book, "asks", ask, hi, pk.tick, need)
    return mid, bid, ask, adj


def cover_price(book: dict, side: str, start: float, limit: float, tick: float, need: float):
    """Closest price to `start` (moving away from the midpoint, not past `limit`) where others
    already wait with at least `need` shares at that price or better. None if there is none."""
    levels = [(float(l["price"]), float(l["size"])) for l in book.get(side, [])]
    p = start
    for _ in range(1000):
        if (side == "bids" and p < limit - 1e-9) or (side == "asks" and p > limit + 1e-9):
            return None
        ahead = sum(s for q, s in levels if (q >= p - 1e-9 if side == "bids" else q <= p + 1e-9))
        if ahead >= need:
            return round(p, 4)
        p = round(p - tick if side == "bids" else p + tick, 4)
    return None


# --------------------------------------------------------------------------- #
# Market selection
# --------------------------------------------------------------------------- #


def pick_markets_f(cfg: ConfigF, banned: set[str] = frozenset(), avoid_events: set[str] = frozenset(), verbose=True):
    events = sc.fetch_events(min_liquidity=500)
    cands = [p for p in rb.candidates(cfg, events) if p.market_id not in banned and p.event_id not in avoid_events]
    cands = sorted(cands, key=lambda p: -p.daily_rate)[:40]
    books = sc.fetch_books([p.token for p in cands])
    scored = []
    for p in cands:
        b = books.get(p.token)
        if not b:
            continue
        base_cost = p.cost
        for k in range(1, cfg.max_size_mult + 1):       # smallest order size that clears the payout floor
            if base_cost * k > cfg.budget * (cfg.max_single_share if k > 1 else 1):
                break
            size = p.min_size * k
            mid, bid, ask, adj = plan_quotes(b, p, cfg.quote_depth, cfg, size)
            if mid is None or (bid is None and ask is None):
                break
            s1 = rb.order_score(p.v, (mid - bid) * 100, size) if bid is not None else 0.0
            s2 = rb.order_score(p.v, (ask - mid) * 100, size) if ask is not None and not cfg.one_sided else 0.0
            ours = rb.q_min(s1, s2, mid)
            q1, q2 = rb.book_scores(adj, mid, p.v)
            others = max(min(q1, q2), max(q1, q2) / rb.REWARD_SINGLE_SIDE_DIVISOR)
            p.est_daily = p.daily_rate * ours / (ours + others) if ours > 0 else 0.0
            p.size_mult, p.cost = k, base_cost * k
            if p.est_daily >= cfg.min_daily_per_market:
                break
        if mid is None or (bid is None and ask is None) or p.est_daily < cfg.min_daily_per_market:
            continue
        size = p.min_size * p.size_mult
        distance = min(d for d in ((mid - bid) if bid is not None else 1, (ask - mid) if ask is not None else 1))
        p.risk = minute_swing_loss(minute_history(p.token), distance, size)
        p.fee_cost = p.fee_rate * mid * (1 - mid) * size  # one forced sale a day, worst case
        p.net = p.est_daily - cfg.risk_weight * p.risk - p.fee_cost
        if p.net > 0:
            scored.append(p)
    ranked = sorted(scored, key=lambda p: -p.net / max(p.cost, 1))
    chosen, used, seen = [], 0.0, set()
    for p in ranked:
        if len(chosen) >= cfg.max_markets:
            break
        if p.event_id in seen:
            continue  # one market per event
        if used + p.cost <= cfg.budget:
            chosen.append(p)
            used += p.cost
            seen.add(p.event_id)
    if verbose:
        print(f"{len(cands)} reward markets fit the rules, {len(scored)} expected to pay ${cfg.min_daily_per_market:.2f}+/day "
              f"and stay positive after risk; chose {len(chosen)} (uses ${used:.2f} of ${cfg.budget:.0f}):")
        for p in chosen:
            print(f"  est. rewards ${p.est_daily:5.2f}/day - risk ${cfg.risk_weight * p.risk:5.2f} - fee ${p.fee_cost:4.2f} "
                  f"= ${p.net:5.2f}/day, {p.size_mult}x size, needs ${p.cost:5.2f}  {p.question[:50]}")
    return chosen


# --------------------------------------------------------------------------- #
# Paper bot F
# --------------------------------------------------------------------------- #


class PaperBotF(re_.PaperBotE):
    def __init__(self, cfg: ConfigF, db_path="paper_F.db", feed: rd.LiveBooks | None = None):
        self.queue: dict[str, float] = {}          # "market:bid" -> shares still ahead of us
        self.queue_price: dict[str, float] = {}    # price that queue belongs to
        self.trade_seen: dict[str, float] = {}     # last trade time processed per token
        self.widen: dict[str, float] = {}
        super().__init__(cfg, db_path, feed)

    def extra_state(self) -> dict:
        return {**super().extra_state(), "widen": self.widen}

    def load_extra(self, st: dict) -> None:
        super().load_extra(st)
        self.widen.update(st.get("widen", {}))

    # -- selection and learning --
    def reselect(self):
        cfg = self.cfg
        now = time.time()
        for mid_id, pk in list(self.picks.items()):
            net = self.market_net(mid_id)
            if net < 0:
                self.widen[mid_id] = min(cfg.max_widen, self.widen.get(mid_id, 0.0) + cfg.loss_widen)
            if net < -max(pk.est_daily, 0.30):
                self.con.execute("INSERT INTO bans VALUES (?,?,?)", (mid_id, now + cfg.ban_hours * 3600, f"net ${net:.2f}"))
                print(f"[{sc.iso(now)}] banning {pk.question[:50]} for {cfg.ban_hours:.0f}h (net ${net:.2f})", flush=True)
        held_events = {self.picks[i].event_id for i in self.pos if self.holding(i) and i in self.picks}
        new = {p.market_id: p for p in pick_markets_f(cfg, rd.banned_markets(self.con, cfg), held_events)}
        for mid_id, p in new.items():
            self.picks[mid_id] = p
            self.pos.setdefault(mid_id, rb.Position())
            self.con.execute("INSERT INTO picks VALUES (?,?,?,?,?,?)", (now, mid_id, p.question, p.daily_rate, p.est_daily, p.cost))
        self.active = set(new)
        watch = [self.picks[i].token for i in self.pos if i in self.picks and (i in new or self.holding(i))]
        self.feed.subscribe(watch)
        for tok, b in sc.fetch_books(watch).items():
            self.feed.set_book(tok, b)
        self.con.commit()

    # -- queue-aware fills --
    def new_trades(self, token: str, now: float) -> list:
        seen = self.trade_seen.get(token, 0.0)
        trades = [t for t in self.feed.recent_trades(token, 120, now) if t[0] > seen]
        if trades:
            self.trade_seen[token] = max(t[0] for t in trades)
        return trades

    def order_filled(self, key: str, side: str, price: float, book: dict, trades: list) -> bool:
        """side 'bid' (we buy YES) or 'ask' (we sell YES)."""
        bb, ba = rb.best(book)
        if side == "bid":
            if ba is not None and ba < price - 1e-9:
                return True                                   # someone offers below our bid
            if any(t[1] < price - 1e-9 for t in trades):
                return True                                   # traded through our price
            at_level = [t for t in trades if abs(t[1] - price) < 1e-9]
        else:
            if bb is not None and bb > price + 1e-9:
                return True
            if any(t[1] > price + 1e-9 for t in trades):
                return True
            at_level = [t for t in trades if abs(t[1] - price) < 1e-9]
        ahead = self.queue.get(key, 0.0) - sum(t[2] for t in at_level)
        ahead = min(ahead, level_size(book, "bids" if side == "bid" else "asks", price))  # cancels ahead move us up
        self.queue[key] = ahead
        return ahead < 0 or (at_level and ahead <= 0)

    def place(self, key: str, side: str, price: float, book: dict) -> None:
        if self.queue_price.get(key) == price and key in self.queue:
            return  # same price as before: keep our place in the queue
        self.queue_price[key] = price
        self.queue[key] = level_size(book, "bids" if side == "bid" else "asks", price)

    def forget(self, key: str) -> None:
        self.queue.pop(key, None)
        self.queue_price.pop(key, None)

    # -- exits --
    def exit_price(self, mid_id: str, pk: rb.Pick, p: rb.Position, bb: float, ba: float, now: float) -> float:
        """Waiting sell order for stuck shares: break-even after rewards, stepping down after 90 min."""
        cfg = self.cfg
        held = p.yes if p.yes > 0 else p.no
        avg = p.cost / held if held else 0.0
        earned = self.market_rewards.get(mid_id, 0.0) / held if held else 0.0
        minutes = (now - self.held_since.get(mid_id, now)) / 60
        steps = max(0, math.floor((minutes - cfg.ramp_start_minutes) / cfg.ramp_step_minutes) + 1) if minutes >= cfg.ramp_start_minutes else 0
        if p.yes > 0:   # sell YES
            target = math.ceil((avg - earned) / pk.tick - 1e-9) * pk.tick - steps * pk.tick
            return round(max(target, bb + pk.tick), 4)
        target = 1 - (math.ceil((avg - earned) / pk.tick - 1e-9) * pk.tick - steps * pk.tick)  # buy YES = sell NO
        return round(min(target, ba - pk.tick), 4)

    def cycle(self, dt: float):
        cfg = self.cfg
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if today != self.day:
            self.day, self.day_start_equity, self.stopped_day = today, self.equity(), None
        stuck = sum(1 for i in self.pos if self.holding(i))
        for mid_id in [i for i in self.pos if i in self.picks]:
            pk, p = self.picks[mid_id], self.pos[mid_id]
            if now - self.feed.updated.get(pk.token, 0) > cfg.stale_seconds:
                fresh = sc.fetch_books([pk.token]).get(pk.token)
                if fresh:
                    self.feed.set_book(pk.token, fresh, now)
            b = self.feed.book(pk.token)
            if not b:
                continue
            rbb, rba = rb.best(b)
            if rbb is None or rba is None:
                continue
            adj = filtered(b, pk.min_size)
            abb, aba = rb.best(adj)
            mid = ((abb + aba) / 2) if abb is not None and aba is not None else (rbb + rba) / 2
            self.last_mid[mid_id] = mid
            trades = self.new_trades(pk.token, now)

            # 1) fills (queue-aware)
            kb, ka = f"{mid_id}:bid", f"{mid_id}:ask"
            if p.bid is not None and self.order_filled(kb, "bid", p.bid, b, trades):
                self.fill(mid_id, p, "buy_yes", p.bid, p.size, mid, now)
                p.bid = None
                self.forget(kb)
            if p.ask is not None and self.order_filled(ka, "ask", p.ask, b, trades):
                self.fill(mid_id, p, "sell_yes", p.ask, p.size, mid, now)
                p.ask = None
                self.forget(ka)

            # 2) rewards (scored from the adjusted midpoint, others counted only at full-size levels)
            share = 0.0
            if p.bid is not None or p.ask is not None:
                s1 = rb.order_score(pk.v, (mid - p.bid) * 100, p.size) if p.bid is not None else 0.0
                s2 = rb.order_score(pk.v, (p.ask - mid) * 100, p.size) if p.ask is not None else 0.0
                ours = rb.q_min(s1, s2, mid)
                q1, q2 = rb.book_scores(adj, mid, pk.v)
                others = max(min(q1, q2), max(q1, q2) / rb.REWARD_SINGLE_SIDE_DIVISOR)
                if ours > 0:
                    share = ours / (ours + others)
                    amount = pk.daily_rate * share * dt / 86400
                    self.rewards += amount
                    self.market_rewards[mid_id] += amount
                    self.con.execute("INSERT INTO accruals VALUES (?,?,?,?,?)", (now, mid_id, pk.question, amount, share))

            # 3) time limit: forced sale at 3 h
            if self.holding(mid_id) and now - self.held_since.get(mid_id, now) >= cfg.max_hold_hours * 3600:
                p.bid = p.ask = None
                self.forget(kb); self.forget(ka)
                self.time_exit(mid_id, pk, p, rbb, rba, mid, now)
                continue

            # 4) guards
            hist = [h for h in self.mid_hist.get(mid_id, []) if now - h[0] <= max(cfg.move_window, cfg.jumpy_window)]
            hist.append((now, mid))
            self.mid_hist[mid_id] = hist
            move = max(abs(mid - h[1]) for h in hist if now - h[0] <= cfg.move_window)
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
            holding = self.holding(mid_id)
            paused = (self.stopped_day == today or not (0.05 < mid < 0.95) or self.paused_until.get(mid_id, 0) > now)
            if holding:
                # waiting exit order keeps working even while paused (it only reduces risk)
                price = self.exit_price(mid_id, pk, p, rbb, rba, now)
                if p.yes > 0:
                    p.ask, p.size = price, p.yes
                    self.place(ka, "ask", price, b); self.forget(kb)
                else:
                    p.bid, p.size = price, p.no
                    self.place(kb, "bid", price, b); self.forget(ka)
                continue
            if paused or mid_id not in self.active or stuck >= cfg.max_stuck:
                self.forget(kb); self.forget(ka)
                continue
            depth = max(cfg.quote_depth, cfg.jumpy_depth if jumpy else 0.0) + self.widen.get(mid_id, 0.0)
            size = pk.min_size * pk.size_mult
            _, bid, ask, _ = plan_quotes(b, pk, min(depth, cfg.cover_zone), cfg, size)
            free = self.cash - sum(self.locked(i, q) for i, q in self.pos.items() if i != mid_id)
            if bid is not None and free >= bid * size:
                p.bid, free = bid, free - bid * size
                self.place(kb, "bid", bid, b)
            else:
                self.forget(kb)
            if ask is not None and not cfg.one_sided and free >= (1 - ask) * size:
                p.ask = ask
                self.place(ka, "ask", ask, b)
            else:
                self.forget(ka)
            p.size = size
        eq = self.equity()
        if self.stopped_day != today and self.day_start_equity - eq >= cfg.daily_loss_limit:
            self.stopped_day = today
            print(f"[{sc.iso(now)}] daily loss limit hit (${self.day_start_equity - eq:.2f}); quotes off until tomorrow", flush=True)
        self.con.execute("INSERT INTO cycles VALUES (?,?,?,?,?,?)", (now, eq, self.cash, self.holdings_value(), self.rewards, len(self.active)))
        self.con.commit()
        self.save_state()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Liquidity-rewards bot version F (paper mode)")
    ap.add_argument("command", choices=["run", "report", "pick"])
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--db", default="paper_F.db")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    cfg = ConfigF()
    if args.command == "pick":
        pick_markets_f(cfg)
        return 0
    if args.command == "report":
        text = rb.report(args.db)
        print(text)
        if args.out:
            open(args.out, "w").write(text + "\n")
        return 0
    PaperBotF(cfg, args.db).run(args.hours)
    print(rb.report(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
