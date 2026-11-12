#!/usr/bin/env python3
"""
rewards_bot_d.py - Version D of the liquidity-rewards bot. PAPER MODE ONLY (no orders are sent).

    python rewards_bot_d.py run --hours 16 --db paper_D.db
    python rewards_bot_d.py pick                 which markets it would quote now, and why
    python rewards_bot_d.py report --db paper_D.db

Same strategy and safety rules as version 2 (rewards_bot.py), plus:
  1. Live feed: order books and trades are pushed over Polymarket's websocket the moment they
     change, and the bot reacts every second instead of every 5 s.
  2. Trade alarm: a large trade, or a burst of trades in one direction, pulls the orders
     before the price follows.
  3. Picks markets using a week of hourly price history: each market's reward estimate is
     reduced by the loss its normal price swings would cause, and only markets that still
     come out ahead are used.
  4. Learns from its own results: a market that cost more than it paid is banned for 24 h,
     and the record survives restarts.
  5. Smarter quoting: centres orders on the "microprice" (leans toward the side with less
     waiting volume) and, when holding shares, prices the selling order closer to exit faster.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import threading
import time
from dataclasses import dataclass

import rewards_bot as rb
import scanner as sc

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class ConfigD(rb.Config):
    cycle_seconds: int = 1             # react every second (prices arrive live)
    big_trade_usd: float = 100.0       # one trade this large -> pull orders
    big_trade_min_sizes: float = 5.0   # ... or this many times the reward minimum size
    burst_trades: int = 3              # this many same-direction trades ...
    burst_seconds: float = 10.0        # ... within this many seconds -> pull orders
    trade_pause_minutes: float = 5.0
    history_days: int = 7
    ban_loss: float = 0.30             # a market that lost more than this (after rewards) is banned 24 h
    ban_hours: float = 24.0
    exit_depth_factor: float = 0.5     # sell held shares at half the usual distance
    stale_seconds: float = 30.0        # no feed update this long -> refresh the book over HTTP


# --------------------------------------------------------------------------- #
# Live order books and trades from the websocket
# --------------------------------------------------------------------------- #


class LiveBooks:
    """Keeps local copies of order books and recent trades, updated from the websocket."""

    def __init__(self):
        self.lock = threading.Lock()
        self.bids: dict[str, dict[float, float]] = {}
        self.asks: dict[str, dict[float, float]] = {}
        self.updated: dict[str, float] = {}
        self.trades: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=200))
        self.assets: list[str] = []
        self.generation = 0
        self.connected = False
        self.messages = 0
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    # -- message handling (pure, testable) --
    def apply(self, msg: dict, now: float | None = None) -> None:
        now = now or time.time()
        et = msg.get("event_type")
        with self.lock:
            if et == "book":
                a = msg.get("asset_id")
                if a not in self.assets:
                    return
                self.bids[a] = {float(l["price"]): float(l["size"]) for l in msg.get("bids") or []}
                self.asks[a] = {float(l["price"]): float(l["size"]) for l in msg.get("asks") or []}
                self.updated[a] = now
            elif et == "price_change":
                for ch in msg.get("price_changes") or []:
                    a = ch.get("asset_id")
                    if a not in self.assets or a not in self.bids:
                        continue
                    side = self.bids[a] if ch.get("side") == "BUY" else self.asks[a]
                    p, s = float(ch["price"]), float(ch["size"])
                    if s <= 0:
                        side.pop(p, None)
                    else:
                        side[p] = s
                    self.updated[a] = now
            elif et == "last_trade_price":
                a = msg.get("asset_id")
                if a in self.assets:
                    self.trades[a].append((now, float(msg.get("price") or 0), float(msg.get("size") or 0), msg.get("side", "")))
            self.messages += 1

    def set_book(self, asset: str, book: dict, now: float | None = None) -> None:
        """Replace a book from an HTTP snapshot (start-up or stale feed)."""
        self.apply({"event_type": "book", "asset_id": asset, "bids": book.get("bids"), "asks": book.get("asks")}, now)

    def book(self, asset: str) -> dict | None:
        """Same shape as the HTTP order book: best bid and best ask are the LAST entries."""
        with self.lock:
            if asset not in self.bids:
                return None
            bids = sorted(self.bids[asset].items())
            asks = sorted(self.asks[asset].items(), reverse=True)
        return {"bids": [{"price": str(p), "size": str(s)} for p, s in bids],
                "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}

    def recent_trades(self, asset: str, seconds: float, now: float | None = None) -> list:
        now = now or time.time()
        with self.lock:
            return [t for t in self.trades.get(asset, ()) if now - t[0] <= seconds]

    # -- connection --
    def subscribe(self, assets: list[str]) -> None:
        with self.lock:
            self.assets = list(assets)
            self.generation += 1

    def _run(self):
        from websockets.sync.client import connect
        while not self._stop:
            gen, assets = self.generation, list(self.assets)
            if not assets:
                time.sleep(1)
                continue
            try:
                with connect(WS_URL, open_timeout=20, ping_interval=20) as ws:
                    ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
                    self.connected = True
                    last_ping = time.time()
                    while not self._stop and gen == self.generation:
                        if time.time() - last_ping > 10:
                            ws.send("PING")
                            last_ping = time.time()
                        try:
                            raw = ws.recv(timeout=2)
                        except TimeoutError:
                            continue
                        if not raw or raw[0] not in "[{":
                            continue  # "PONG"
                        data = json.loads(raw)
                        for msg in data if isinstance(data, list) else [data]:
                            if isinstance(msg, dict):
                                self.apply(msg)
            except Exception as e:  # reconnect after network trouble
                print(f"[{sc.iso(time.time())}] feed reconnecting: {e}", file=sys.stderr, flush=True)
                time.sleep(3)
            finally:
                self.connected = False


# --------------------------------------------------------------------------- #
# Market selection with price history and memory
# --------------------------------------------------------------------------- #


def price_history(token: str, days: int = 7) -> list[float]:
    try:
        d = sc.get_json(f"{sc.CLOB}/prices-history", params={"market": token, "interval": "1w" if days <= 7 else "max", "fidelity": 60})
        return [float(h["p"]) for h in d.get("history", [])]
    except Exception:
        return []


def swing_loss_per_day(prices: list[float], quote_distance: float, size: float) -> float:
    """Estimated daily loss from price swings: each hourly move beyond our quote distance
    costs (move - distance) x size. Assumes about one point per hour."""
    if len(prices) < 24:
        return float("inf")  # not enough history: treat as unknown risk
    moves = [abs(b - a) for a, b in zip(prices, prices[1:])]
    excess = sum(max(0.0, m - quote_distance) for m in moves) * size
    return excess / (len(moves) / 24)


def microprice(book: dict) -> float | None:
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None
    bb, bs = float(bids[-1]["price"]), float(bids[-1]["size"])
    ba, as_ = float(asks[-1]["price"]), float(asks[-1]["size"])
    if bs + as_ <= 0:
        return (bb + ba) / 2
    # more waiting buyers than sellers -> the price is more likely to tick up
    return (bb * as_ + ba * bs) / (bs + as_)


def banned_markets(con, cfg: ConfigD, now: float | None = None) -> set[str]:
    now = now or time.time()
    con.execute("CREATE TABLE IF NOT EXISTS bans (market_id TEXT, until REAL, reason TEXT)")
    return {r[0] for r in con.execute("SELECT market_id FROM bans WHERE until > ?", (now,))}


def pick_markets_d(cfg: ConfigD, banned: set[str] = frozenset(), verbose=True) -> list[rb.Pick]:
    events = sc.fetch_events(min_liquidity=500)
    cands = [p for p in rb.candidates(cfg, events) if p.market_id not in banned]
    cands = sorted(cands, key=lambda p: -p.daily_rate)[:40]
    books = sc.fetch_books([p.token for p in cands])
    scored = []
    for p in cands:
        b = books.get(p.token)
        if not b:
            continue
        bb, ba = rb.best(b)
        if bb is None or ba is None:
            continue
        mid = (bb + ba) / 2
        bid, ask = rb.quotes_for(mid, p.v, p.tick, cfg.quote_depth, bb, ba)
        s_ask = 0.0 if cfg.one_sided else rb.order_score(p.v, (ask - mid) * 100, p.min_size)
        ours = rb.q_min(rb.order_score(p.v, (mid - bid) * 100, p.min_size), s_ask, mid)
        q1, q2 = rb.book_scores(b, mid, p.v)
        others = max(min(q1, q2), max(q1, q2) / rb.REWARD_SINGLE_SIDE_DIVISOR)
        p.est_daily = p.daily_rate * ours / (ours + others) if ours > 0 else 0.0
        risk = swing_loss_per_day(price_history(p.token, cfg.history_days), mid - bid, p.min_size)
        p.risk = risk
        p.net = p.est_daily - getattr(cfg, "risk_weight", 1.0) * risk
        if p.net > 0:
            scored.append(p)
    ranked = sorted(scored, key=lambda p: -p.net / max(p.cost, 1))
    chosen, used = [], 0.0
    for p in ranked:
        if len(chosen) >= cfg.max_markets:
            break
        if used + p.cost <= cfg.budget:
            chosen.append(p)
            used += p.cost
    if verbose:
        print(f"{len(cands)} reward markets fit the rules, {len(scored)} still positive after expected swing losses; "
              f"chose {len(chosen)} (uses ${used:.2f} of ${cfg.budget:.0f}):")
        for p in chosen:
            w = getattr(cfg, "risk_weight", 1.0)
            print(f"  est. rewards ${p.est_daily:5.2f}/day - swing risk ${w * p.risk:5.2f}/day = ${p.net:5.2f}/day, needs ${p.cost:5.2f}  {p.question[:60]}")
    return chosen


# --------------------------------------------------------------------------- #
# Paper bot D
# --------------------------------------------------------------------------- #


class PaperBotD(rb.PaperBot):
    def __init__(self, cfg: ConfigD, db_path="paper_D.db", feed: LiveBooks | None = None):
        self.market_cash: dict[str, float] = collections.defaultdict(float)   # cash in/out per market
        self.market_rewards: dict[str, float] = collections.defaultdict(float)
        super().__init__(cfg, db_path)  # may resume saved state, so the dicts above must exist first
        self.con.execute("CREATE TABLE IF NOT EXISTS bans (market_id TEXT, until REAL, reason TEXT)")
        self.feed = feed if feed is not None else LiveBooks()
        self.last_trade_check: dict[str, float] = {}
        self.active = set()

    def fill(self, mid_id, p, side, price, size, mid, now):
        before = self.cash
        super().fill(mid_id, p, side, price, size, mid, now)
        self.market_cash[mid_id] += self.cash - before

    def market_net(self, mid_id: str) -> float:
        p = self.pos.get(mid_id)
        m = self.last_mid.get(mid_id, 0.5)
        held = (p.yes * m + p.no * (1 - m)) if p else 0.0
        return self.market_rewards[mid_id] + self.market_cash[mid_id] + held

    def reselect(self):
        cfg = self.cfg
        now = time.time()
        # learn: ban markets that have cost more than they paid
        for mid_id in list(self.picks):
            net = self.market_net(mid_id)
            if net < -cfg.ban_loss:
                self.con.execute("INSERT INTO bans VALUES (?,?,?)", (mid_id, now + cfg.ban_hours * 3600, f"net ${net:.2f}"))
                print(f"[{sc.iso(now)}] banning {self.picks[mid_id].question[:50]} for {cfg.ban_hours:.0f}h (net ${net:.2f})", flush=True)
        new = {p.market_id: p for p in pick_markets_d(cfg, banned_markets(self.con, cfg))}
        for mid_id, p in new.items():
            self.picks[mid_id] = p
            self.pos.setdefault(mid_id, rb.Position())
            self.con.execute("INSERT INTO picks VALUES (?,?,?,?,?,?)", (now, mid_id, p.question, p.daily_rate, p.est_daily, p.cost))
        self.active = set(new)
        # keep watching markets where we still hold shares, so we can sell them
        watch = [self.picks[i].token for i in self.pos if i in self.picks and (i in new or self.pos[i].yes > 0 or self.pos[i].no > 0)]
        self.feed.subscribe(watch)
        for tok, b in sc.fetch_books(watch).items():
            self.feed.set_book(tok, b)
        self.con.commit()

    def trade_alarm(self, pk: rb.Pick, now: float) -> str | None:
        cfg = self.cfg
        recent = self.feed.recent_trades(pk.token, cfg.burst_seconds, now)
        seen = self.last_trade_check.get(pk.token, 0)
        new = [t for t in recent if t[0] > seen]
        self.last_trade_check[pk.token] = now
        for t in new:
            if t[1] * t[2] >= cfg.big_trade_usd or t[2] >= cfg.big_trade_min_sizes * pk.min_size:
                return f"large trade: {t[2]:.0f} shares at {t[1]:.3f}"
        for side in ("BUY", "SELL"):
            if sum(1 for t in recent if t[3] == side) >= cfg.burst_trades:
                return f"{cfg.burst_trades}+ {side.lower()} trades in {cfg.burst_seconds:.0f}s"
        return None

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
            trades = [t for t in self.feed.recent_trades(pk.token, max(dt, 1) + 1, now)]
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
            # 3) guards
            hist = [h for h in self.mid_hist.get(mid_id, []) if now - h[0] <= cfg.move_window]
            hist.append((now, mid))
            self.mid_hist[mid_id] = hist
            move = max(abs(mid - h[1]) for h in hist)
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
            # 4) requote around the microprice (or stay out)
            p.bid = p.ask = None
            if (self.stopped_day == today or mid_id not in self.active or not (0.05 < mid < 0.95)
                    or self.paused_until.get(mid_id, 0) > now):
                continue
            centre = microprice(b) or mid
            centre = min(max(centre, bb), ba)
            holding = p.yes > 0 or p.no > 0
            depth = cfg.quote_depth * (cfg.exit_depth_factor if holding else 1.0)
            bid, ask = rb.quotes_for(centre, pk.v, pk.tick, depth, bb, ba)
            # rewards are scored from the midpoint: stay inside max spread of it
            v = pk.v / 100
            bid, ask = max(bid, round(mid - v + pk.tick, 4)), min(ask, round(mid + v - pk.tick, 4))
            free = self.cash - sum(self.locked(i, q) for i, q in self.pos.items() if i != mid_id)
            size = pk.min_size
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

    def extra_state(self) -> dict:
        return {"market_cash": dict(self.market_cash), "market_rewards": dict(self.market_rewards)}

    def load_extra(self, st: dict) -> None:
        self.market_cash.update(st.get("market_cash", {}))
        self.market_rewards.update(st.get("market_rewards", {}))

    def run(self, hours: float):
        cfg = self.cfg
        end = time.time() + hours * 3600
        next_pick, last, last_print = 0.0, time.time(), 0.0
        while time.time() < end:
            started = time.time()
            try:
                if started >= next_pick:
                    self.reselect()
                    next_pick = started + cfg.reselect_minutes * 60
                self.cycle(dt=max(started - last, 0.0) or cfg.cycle_seconds)
                last = started
                if started - last_print >= 60:
                    last_print = started
                    print(f"[{sc.iso(started)}] equity ${self.equity():.2f} | rewards ${self.rewards:.3f} | holdings ${self.holdings_value():.2f} "
                          f"| feed {'live' if self.feed.connected else 'DOWN'} ({self.feed.messages} msgs)", flush=True)
            except Exception as e:
                print(f"[{sc.iso(started)}] cycle failed: {e}", file=sys.stderr, flush=True)
            time.sleep(max(0, cfg.cycle_seconds - (time.time() - started)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Liquidity-rewards bot version D (paper mode)")
    ap.add_argument("command", choices=["run", "report", "pick"])
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--db", default="paper_D.db")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    cfg = ConfigD()
    if args.command == "pick":
        pick_markets_d(cfg)
        return 0
    if args.command == "report":
        text = rb.report(args.db)
        print(text)
        if args.out:
            open(args.out, "w").write(text + "\n")
        return 0
    PaperBotD(cfg, args.db).run(args.hours)
    print(rb.report(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
