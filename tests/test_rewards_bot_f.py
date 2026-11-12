"""python -m unittest discover -s tests"""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import rewards_bot as rb
import rewards_bot_f as rf
from test_rewards_bot_d import FakeFeed


def lv(pairs):
    return [{"price": str(p), "size": str(s)} for p, s in pairs]


def book(bids, asks):  # best bid/ask LAST, like the API
    return {"bids": lv(sorted(bids)), "asks": lv(sorted(asks, reverse=True))}


class Helpers(unittest.TestCase):
    def test_dust_is_ignored_for_the_midpoint(self):
        b = book([(0.40, 100), (0.47, 2)], [(0.60, 100), (0.53, 3)])  # 2- and 3-share dust inside
        self.assertEqual(rb.best(rf.filtered(b, 20)), (0.40, 0.60))

    def test_no_order_where_it_would_score_almost_nothing(self):
        pk = rb.Pick("m", "Q", "t", 100, 4.5, 20, 0.01, "")
        empty = book([(0.40, 100)], [(0.60, 100)])                  # nobody waiting inside the zone
        _, bid, ask, _ = rf.plan_quotes(empty, pk, 0.5, rf.ConfigF())
        self.assertEqual((bid, ask), (None, None))
        deep = book([(0.48, 60), (0.40, 100)], [(0.52, 60), (0.60, 100)])
        _, bid, ask, _ = rf.plan_quotes(deep, pk, 0.5, rf.ConfigF())
        self.assertEqual((bid, ask), (0.47, 0.53))                  # 2.25c out, behind others, inside 80% of zone

    def test_minute_swing_loss(self):
        self.assertEqual(rf.minute_swing_loss([0.5] * 200, 0.02, 20), 0)
        self.assertEqual(rf.minute_swing_loss([0.5] * 50, 0.02, 20), float("inf"))


class BotF(unittest.TestCase):
    def setUp(self):
        self.feed = FakeFeed()
        self.bot = rf.PaperBotF(rf.ConfigF(), ":memory:", feed=self.feed)
        self.pk = rb.Pick("m", "Q", "t", 100, 4.5, 20, 0.01, "", fee_rate=0.05, event_id="e1")
        self.bot.picks["m"] = self.pk
        self.bot.pos["m"] = rb.Position()
        self.bot.active = {"m"}
        self.feed.subscribe(["t"])
        self.now = 1000.0
        self._saved = rf.time.time
        rf.time.time = lambda: self.now

    def tearDown(self):
        rf.time.time = self._saved

    def tick(self, bids, asks, trades=()):
        self.feed.apply({"event_type": "book", "asset_id": "t", "bids": lv(bids), "asks": lv(asks)}, now=self.now)
        for price, size in trades:
            self.feed.apply({"event_type": "last_trade_price", "asset_id": "t", "price": str(price), "size": str(size), "side": "SELL"}, now=self.now)
        self.bot.cycle(dt=1)
        self.now += 1

    def test_queue_ahead_must_trade_before_we_fill(self):
        bids, asks = [(0.47, 60), (0.40, 100)], [(0.52, 60), (0.60, 100)]
        self.bot.cfg.big_trade_usd = 1e9; self.bot.cfg.big_trade_min_sizes = 1e9; self.bot.cfg.burst_trades = 99
        self.tick(bids, asks)
        p = self.bot.pos["m"]
        self.assertEqual(p.bid, 0.47)                    # we join behind 60 shares at 47c
        self.tick(bids, asks, trades=[(0.47, 40)])       # 40 traded: 20 still ahead
        self.assertEqual(p.yes, 0)
        self.tick([(0.47, 20), (0.40, 100)], asks, trades=[(0.47, 25)])  # queue used up and then some
        self.assertEqual(p.yes, 20)

    def test_trade_through_our_price_fills_at_once(self):
        bids, asks = [(0.47, 60), (0.40, 100)], [(0.52, 60), (0.60, 100)]
        self.bot.cfg.big_trade_usd = 1e9; self.bot.cfg.big_trade_min_sizes = 1e9
        self.tick(bids, asks)
        self.tick(bids, asks, trades=[(0.46, 5)])
        self.assertEqual(self.bot.pos["m"].yes, 20)

    def test_exit_order_starts_at_break_even_after_rewards_then_steps_down(self):
        p = self.bot.pos["m"] = rb.Position(yes=20, cost=0.50 * 20)
        self.bot.held_since["m"] = self.now
        self.bot.market_rewards["m"] = 0.20                # 1c per share already earned
        self.assertEqual(self.bot.exit_price("m", self.pk, p, 0.40, 0.60, self.now), 0.49)
        self.assertEqual(self.bot.exit_price("m", self.pk, p, 0.40, 0.60, self.now + 90 * 60), 0.48)
        self.assertEqual(self.bot.exit_price("m", self.pk, p, 0.40, 0.60, self.now + 120 * 60), 0.46)
        self.assertEqual(self.bot.exit_price("m", self.pk, p, 0.47, 0.60, self.now + 170 * 60), 0.48)  # never below best bid + 1 tick


class Selection(unittest.TestCase):
    def test_one_market_per_event_and_payout_floor(self):
        cfg = rf.ConfigF()
        mk = lambda mid, ev, pool: rb.Pick(mid, mid, "t" + mid, pool, 4.5, 20, 0.01, "", cost=19, event_id=ev)
        cands = [mk("a", "e1", 400), mk("b", "e1", 400), mk("c", "e2", 400), mk("d", "e3", 2)]
        deep = book([(0.48, 60), (0.40, 100)], [(0.52, 60), (0.60, 100)])
        saved = (rf.sc.fetch_events, rf.rb.candidates, rf.sc.fetch_books, rf.minute_history)
        try:
            rf.sc.fetch_events = lambda min_liquidity=0: []
            rf.rb.candidates = lambda cfg, events: [rb.Pick(**vars(c)) for c in cands]
            rf.sc.fetch_books = lambda tokens: {t: deep for t in tokens}
            rf.minute_history = lambda token: [0.5] * 500
            chosen = rf.pick_markets_f(cfg, verbose=False)
        finally:
            rf.sc.fetch_events, rf.rb.candidates, rf.sc.fetch_books, rf.minute_history = saved
        ids = [p.market_id for p in chosen]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len({p.event_id for p in chosen}), 2)   # not a and b together
        self.assertNotIn("d", ids)                                 # $2/day pool can't clear $1.25


if __name__ == "__main__":
    unittest.main()


class SelectionShare(unittest.TestCase):
    def test_market_where_we_would_be_alone_is_skipped(self):
        cfg = rf.ConfigF()
        pk = rb.Pick("x", "alone", "tx", 5, 4.5, 20, 0.01, "", cost=19, event_id="e9")
        # plenty of shares waiting (cover), but all in orders below the 20-share reward minimum,
        # so nobody else scores and our share would be ~100%
        thin = book([(0.49, 15), (0.48, 15), (0.47, 15), (0.40, 100)], [(0.51, 15), (0.52, 15), (0.53, 15), (0.60, 100)])
        saved = (rf.sc.fetch_events, rf.rb.candidates, rf.sc.fetch_books, rf.minute_history)
        try:
            rf.sc.fetch_events = lambda min_liquidity=0: []
            rf.rb.candidates = lambda cfg, events: [rb.Pick(**vars(pk))]
            rf.sc.fetch_books = lambda tokens: {t: thin for t in tokens}
            rf.minute_history = lambda token: [0.5] * 500
            chosen = rf.pick_markets_f(cfg, verbose=False)
        finally:
            rf.sc.fetch_events, rf.rb.candidates, rf.sc.fetch_books, rf.minute_history = saved
        self.assertEqual(chosen, [])
