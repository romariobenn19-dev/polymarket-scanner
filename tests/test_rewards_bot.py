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
