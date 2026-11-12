"""python -m unittest discover -s tests"""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import rewards_bot as rb
import rewards_bot_e as re_
from test_rewards_bot_d import FakeFeed, book_msg


def levels(pairs):
    return [{"price": str(p), "size": str(s)} for p, s in pairs]


class QueueCover(unittest.TestCase):
    def test_bid_steps_back_until_others_are_in_front(self):
        book = {"bids": levels([(0.45, 100), (0.47, 10), (0.48, 5)]), "asks": levels([(0.52, 100)])}
        # want 40 shares in front: 0.48 has 5, 0.47 has 15 cumulative, 0.45 has 115
        self.assertEqual(re_.covered_bid(book, 0.48, 0.455 - 0.01, 0.01, 40), 0.45)

    def test_falls_back_to_far_edge_when_book_is_empty(self):
        book = {"bids": [], "asks": []}
        self.assertEqual(re_.covered_bid(book, 0.48, 0.455, 0.01, 40), 0.465)
        self.assertEqual(re_.covered_ask(book, 0.52, 0.545, 0.01, 40), 0.535)

    def test_taker_fee(self):
        self.assertAlmostEqual(re_.taker_fee(100, 0.5, 0.05), 1.25)  # matches Polymarket's published example


class BotE(unittest.TestCase):
    def setUp(self):
        self.feed = FakeFeed()
        self.bot = re_.PaperBotE(re_.ConfigE(), ":memory:", feed=self.feed)
        for mid in ("m", "n", "o"):
            self.bot.picks[mid] = rb.Pick(mid, f"Market {mid}", f"t{mid}", 100, 4.5, 20, 0.01, "", fee_rate=0.05)
            self.bot.pos[mid] = rb.Position()
        self.bot.active = {"m", "n", "o"}
        self.feed.subscribe(["tm", "tn", "to"])
        self.now = 1000.0
        self._saved = re_.time.time
        re_.time.time = lambda: self.now

    def tearDown(self):
        re_.time.time = self._saved

    def tick(self, books=None, dt=1):
        for tok in ("tm", "tn", "to"):
            bid, ask = (books or {}).get(tok, (0.49, 0.51))
            self.feed.apply(book_msg(tok, [(bid, 500)], [(ask, 500)]), now=self.now)
        self.bot.cycle(dt=dt)
        self.now += dt

    def test_quotes_in_a_calm_market(self):
        self.tick()
        p = self.bot.pos["m"]
        self.assertIsNotNone(p.bid); self.assertIsNotNone(p.ask)

    def test_stuck_shares_are_sold_after_the_time_limit(self):
        self.bot.pos["m"] = rb.Position(yes=20, cost=9.6)
        self.bot.held_since["m"] = self.now
        self.bot.cash = 40.4
        self.tick()
        self.assertEqual(self.bot.pos["m"].yes, 20)          # still within the limit
        self.now += 3 * 3600
        self.tick()
        self.assertEqual(self.bot.pos["m"].yes, 0)           # sold
        self.assertNotIn("m", self.bot.held_since)
        sold = self.bot.con.execute("SELECT side, price, size FROM fills WHERE side='exit_yes'").fetchone()
        self.assertEqual(sold, ("exit_yes", 0.49, 20.0))
        self.assertAlmostEqual(self.bot.cash, 40.4 + 20 * 0.49 - re_.taker_fee(20, 0.49, 0.05), places=6)

    def test_no_new_risk_once_two_markets_are_stuck(self):
        self.bot.pos["m"] = rb.Position(yes=20)
        self.bot.pos["n"] = rb.Position(no=20)
        self.bot.held_since.update({"m": self.now, "n": self.now})
        self.tick()
        self.assertIsNotNone(self.bot.pos["m"].ask)          # still trying to sell what it holds
        self.assertEqual((self.bot.pos["o"].bid, self.bot.pos["o"].ask), (None, None))

    def test_quotes_sit_further_out_after_a_jumpy_spell(self):
        self.tick()
        calm_gap = 0.50 - self.bot.pos["o"].bid
        self.bot.cfg.move_pause = 1.0                        # isolate from the 2c pause
        self.tick(books={"to": (0.505, 0.525)})              # price up 1.5c within 10 minutes
        self.now += 120
        self.tick(books={"to": (0.505, 0.525)})
        jumpy_gap = 0.515 - self.bot.pos["o"].bid
        self.assertGreater(jumpy_gap, calm_gap)


if __name__ == "__main__":
    unittest.main()
