"""python -m unittest discover -s tests"""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import settle_bot as sb


def lv(pairs):
    return [{"price": str(p), "size": str(s)} for p, s in pairs]


def book(bids, asks):
    return {"bids": lv(bids), "asks": lv(asks)}


class Pricing(unittest.TestCase):
    def test_buys_yes_when_yes_is_favoured(self):
        side, shares, price = sb.favourite(book([(0.95, 100)], [(0.96, 100)]), 9.6)
        self.assertEqual((side, round(shares, 2), price), ("YES", 10.0, 0.96))

    def test_buys_no_when_no_is_favoured(self):
        side, shares, price = sb.favourite(book([(0.04, 300)], [(0.06, 300)]), 9.6)
        self.assertEqual(side, "NO")
        self.assertAlmostEqual(price, 0.96)

    def test_risky_types_are_flagged(self):
        for q in ("Will the highest temperature in Seattle be between 58-59°F on November 8?",
                  "Will total votes cast in the 2026 midterm election for Senator from Wyoming exceed 250k?",
                  "Will SpaceX's valuation be between $1.5T and $2T?"):
            self.assertTrue(sb.RISKY.search(q), q)
        self.assertFalse(sb.RISKY.search("Will the Democratic Party candidate win the 2026 CO-08 House race?"))


class Bot(unittest.TestCase):
    def setUp(self):
        self.bot = sb.SettleBot(sb.ConfigH(), ":memory:")
        self.c = {"market_id": "m1", "event_id": "e1", "question": "Will X be named CEO by Nov 10?", "token": "t1",
                  "fee_rate": 0.0, "hours_past": 30, "approx": 0.95}

    def test_buys_and_settles_a_win(self):
        self.assertTrue(self.bot.consider(self.c, book([(0.04, 500)], [(0.05, 500)]), 1000))
        t = self.bot.open_trades()[0]
        self.assertEqual(t[4], "NO")
        self.bot.manage({}, {"m1": 0}, 2000)                   # YES lost -> NO pays $1
        side, pnl = self.bot.con.execute("SELECT side, pnl FROM trades").fetchone()
        self.assertGreater(pnl, 0)
        self.assertAlmostEqual(self.bot.cash, 50 + pnl)

    def test_skips_wide_spread_and_risky_questions(self):
        self.assertFalse(self.bot.consider(self.c, book([(0.90, 500)], [(0.96, 500)]), 1000))
        risky = {**self.c, "market_id": "m2", "question": "Will turnout exceed 60%?"}
        self.assertFalse(self.bot.consider(risky, book([(0.95, 500)], [(0.96, 500)]), 1000))
        reasons = [r[0] for r in self.bot.con.execute("SELECT reason FROM seen")]
        self.assertTrue(any("spread" in r for r in reasons))
        self.assertTrue(any("can still move" in r for r in reasons))

    def test_too_expensive_or_too_cheap_is_skipped(self):
        self.assertFalse(self.bot.consider(self.c, book([(0.993, 500)], [(0.995, 500)]), 1000))   # 99.5c: too little gain
        self.assertFalse(self.bot.consider({**self.c, "market_id": "m3"}, book([(0.80, 500)], [(0.82, 500)]), 1000))

    def test_elections_need_96c(self):
        e = {**self.c, "question": "Will the Democratic Party candidate win the 2026 NY-04 House race?"}
        self.assertFalse(self.bot.consider(e, book([(0.93, 500)], [(0.94, 500)]), 1000))
        self.assertTrue(self.bot.consider({**e, "market_id": "m9"}, book([(0.96, 500)], [(0.97, 500)]), 1000))

    def test_safety_exit_when_the_favourite_collapses(self):
        self.bot.consider(self.c, book([(0.04, 500)], [(0.05, 500)]), 1000)
        self.bot.manage({"t1": book([(0.35, 500)], [(0.37, 500)])}, {}, 2000)   # NO now ~64c
        reason, pnl = self.bot.con.execute("SELECT reason, pnl FROM trades").fetchone()
        self.assertIn("safety exit", reason)
        self.assertLess(pnl, 0)

    def test_budget_and_one_per_event(self):
        b = book([(0.04, 5000)], [(0.05, 5000)])
        for i in range(7):
            self.bot.consider({**self.c, "market_id": f"m{i}", "event_id": f"e{i}", "token": f"t{i}"}, b, 1000)
        self.assertEqual(len(self.bot.open_trades()), 5)                    # $50 / $10, slippage tolerated
        self.assertFalse(self.bot.consider({**self.c, "market_id": "x", "token": "tx"}, b, 1000))  # event e1 held


if __name__ == "__main__":
    unittest.main()
