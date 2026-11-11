"""python -m unittest discover -s tests"""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rewards_bot as rb


class Scoring(unittest.TestCase):
    def test_order_score(self):
        self.assertAlmostEqual(rb.order_score(4.5, 0, 20), 20)          # at the midpoint: full size
        self.assertAlmostEqual(rb.order_score(4.5, 2.25, 20), 5)        # halfway: a quarter
        self.assertEqual(rb.order_score(4.5, 4.5, 20), 0)               # at/after max spread: nothing
        self.assertEqual(rb.order_score(4.5, -1, 20), 0)

    def test_qmin_two_sided_vs_one_sided(self):
        self.assertAlmostEqual(rb.q_min(10, 10, 0.5), 10)
        self.assertAlmostEqual(rb.q_min(9, 0, 0.5), 3)                  # one side only: divided by 3
        self.assertEqual(rb.q_min(9, 0, 0.95), 0)                       # near 0/1 both sides are required

    def test_quotes_stay_inside_and_never_cross(self):
        bid, ask = rb.quotes_for(0.50, 4.5, 0.01, 0.5, 0.49, 0.51)
        self.assertLess(bid, 0.50); self.assertGreater(ask, 0.50)
        self.assertLess((0.50 - bid) * 100, 4.5); self.assertLess((ask - 0.50) * 100, 4.5)
        bid, ask = rb.quotes_for(0.50, 4.5, 0.01, 0.5, 0.495, 0.497)   # very tight book
        self.assertLess(bid, 0.497); self.assertGreater(ask, 0.495)


class PaperAccounting(unittest.TestCase):
    def test_both_sides_fill_merges_to_cash_and_keeps_spread(self):
        bot = rb.PaperBot(rb.Config(), ":memory:")
        bot.picks["m"] = rb.Pick("m", "Q", "t", 100, 4.5, 20, 0.01, "")
        p = bot.pos["m"] = rb.Position()
        bot.fill("m", p, "buy_yes", 0.48, 20, 0.47, 0)   # paid 9.60
        bot.fill("m", p, "sell_yes", 0.52, 20, 0.53, 0)  # bought NO at 0.48: paid 9.60
        self.assertEqual((p.yes, p.no), (0, 0))
        self.assertAlmostEqual(bot.cash, 50 - 9.6 - 9.6 + 20)  # +$0.80 = the 4c spread on 20 shares


if __name__ == "__main__":
    unittest.main()


def _book(bid, ask, size=500):
    return {"bids": [{"price": str(bid), "size": str(size)}], "asks": [{"price": str(ask), "size": str(size)}]}


class Version2Guards(unittest.TestCase):
    def setUp(self):
        self.bot = rb.PaperBot(rb.Config(), ":memory:")
        self.bot.picks["m"] = rb.Pick("m", "Calm market", "t", 100, 4.5, 20, 0.01, "")
        self.bot.pos["m"] = rb.Position()
        self.bot.active = {"m"}
        self.books = {}
        rb.sc.fetch_books = lambda tokens: {"t": self.books["t"]}

    def step(self, bid, ask, size=500):
        self.books["t"] = _book(bid, ask, size)
        self.bot.cycle(dt=5)
        return self.bot.pos["m"]

    def test_quotes_both_sides_in_a_calm_market(self):
        p = self.step(0.49, 0.51)
        self.assertIsNotNone(p.bid); self.assertIsNotNone(p.ask)

    def test_fast_move_pulls_orders_and_pauses(self):
        self.step(0.49, 0.51)
        p = self.step(0.46, 0.48)   # mid falls 3c: bid filled, then pause
        self.assertEqual((p.bid, p.ask), (None, None))
        self.assertEqual(self.bot.con.execute("SELECT COUNT(*) FROM pauses").fetchone()[0], 1)
        p = self.step(0.46, 0.48)   # still paused
        self.assertEqual((p.bid, p.ask), (None, None))

    def test_after_a_fill_only_the_selling_side_is_quoted(self):
        self.step(0.49, 0.51)
        self.bot.cfg.move_pause = 1.0          # isolate the inventory rule from the move guard
        p = self.step(0.455, 0.465)          # price drops through our 47c bid: we now hold YES
        self.assertGreater(p.yes, 0)
        self.assertIsNone(p.bid)
        self.assertIsNotNone(p.ask)

    def test_leaves_when_other_makers_leave(self):
        self.step(0.49, 0.51)
        self.step(0.49, 0.51, size=1)        # book empties: our share jumps
        p = self.step(0.49, 0.51, size=1)
        self.assertEqual((p.bid, p.ask), (None, None))
        reason = self.bot.con.execute("SELECT reason FROM pauses").fetchone()[0]
        self.assertIn("other makers left", reason)


class OneSided(unittest.TestCase):
    def test_one_sided_only_places_the_buy_order(self):
        bot = rb.PaperBot(rb.Config(one_sided=True), ":memory:")
        bot.picks["m"] = rb.Pick("m", "Calm", "t", 100, 4.5, 20, 0.01, "")
        bot.pos["m"] = rb.Position()
        bot.active = {"m"}
        rb.sc.fetch_books = lambda tokens: {"t": _book(0.49, 0.51)}
        bot.cycle(dt=5)
        self.assertIsNotNone(bot.pos["m"].bid)
        self.assertIsNone(bot.pos["m"].ask)
