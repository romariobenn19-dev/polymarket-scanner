"""python -m unittest discover -s tests"""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jump_bot as jb

H = 3600


def lv(pairs):
    return [{"price": str(p), "size": str(s)} for p, s in pairs]


def book(bids, asks):
    return {"bids": lv(bids), "asks": lv(asks)}


class Pricing(unittest.TestCase):
    def test_buy_no_walks_the_yes_bids(self):
        b = book([(0.55, 5), (0.54, 100)], [(0.57, 100)])
        shares, no = jb.buy_no(b, 4.5)           # $4.50 at NO 45c = 10 shares: 5 @ 55, 5 @ 54 YES
        self.assertAlmostEqual(shares, 10)
        self.assertAlmostEqual(no, 1 - 0.545)

    def test_not_enough_buyers(self):
        self.assertIsNone(jb.buy_no(book([(0.55, 2)], [(0.57, 100)]), 5))

    def test_sell_no_buys_yes_from_asks(self):
        self.assertAlmostEqual(jb.sell_no(book([(0.40, 100)], [(0.42, 50)]), 10), 0.58)

    def test_rise_in_window(self):
        now = 100 * H
        hist = [(now - 20 * H, 0.20), (now - 10 * H, 0.30), (now - 2 * H, 0.40)]
        self.assertAlmostEqual(jb.rise_in_window(hist, 0.50, 12, now), 0.20)   # from 30c (20c is too old)

    def test_fee(self):
        self.assertAlmostEqual(jb.taker_fee(100, 0.5, 0.05), 1.25)


class Bot(unittest.TestCase):
    def setUp(self):
        self.bot = jb.JumpBot(jb.ConfigG(), ":memory:")
        self.now = 1_000_000.0
        self.c = {"market_id": "m1", "event_id": "e1", "question": "Will X happen?", "token": "t1", "fee_rate": 0.04}
        self.hist = [(self.now - 10 * H, 0.30), (self.now - 1 * H, 0.45)]

    def test_enters_on_a_real_rise_with_a_tight_book(self):
        self.bot.consider(self.c, book([(0.49, 200)], [(0.51, 200)]), self.hist, self.now)
        trades = self.bot.open_trades()
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0][5], 0.51)                 # NO bought at 1 - 49c
        self.assertLess(self.bot.cash, 45.0)                         # $5 + fee spent

    def test_skips_wide_spreads_and_records_why(self):
        self.bot.consider(self.c, book([(0.42, 200)], [(0.58, 200)]), self.hist, self.now)
        self.assertEqual(self.bot.open_trades(), [])
        reason = self.bot.con.execute("SELECT reason FROM signals").fetchone()[0]
        self.assertIn("spread", reason)

    def test_no_signal_without_a_big_enough_rise(self):
        self.bot.consider(self.c, book([(0.49, 200)], [(0.51, 200)]), [(self.now - 2 * H, 0.42)], self.now)
        self.assertEqual(self.bot.con.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)

    def test_one_trade_per_event(self):
        b = book([(0.49, 200)], [(0.51, 200)])
        self.bot.consider(self.c, b, self.hist, self.now)
        self.bot.consider({**self.c, "market_id": "m2", "token": "t2"}, b, self.hist, self.now)
        self.assertEqual(len(self.bot.open_trades()), 1)

    def test_take_profit_stop_loss_and_time_exit(self):
        b = book([(0.49, 200)], [(0.51, 200)])
        self.bot.consider(self.c, b, self.hist, self.now)
        self.bot.manage({"t1": book([(0.44, 200)], [(0.46, 200)])}, {}, self.now + 60)     # NO 54c: +3c, hold
        self.assertEqual(len(self.bot.open_trades()), 1)
        self.bot.manage({"t1": book([(0.30, 200)], [(0.33, 200)])}, {}, self.now + 120)    # NO 67c: +16c -> take profit
        r = self.bot.con.execute("SELECT reason, pnl FROM trades").fetchone()
        self.assertEqual(r[0], "take profit")
        self.assertGreater(r[1], 0)

    def test_resolution_settles_at_the_result(self):
        self.bot.consider(self.c, book([(0.49, 200)], [(0.51, 200)]), self.hist, self.now)
        self.bot.manage({}, {"m1": 0}, self.now + 3600)        # YES lost -> NO pays $1
        r = self.bot.con.execute("SELECT reason, exit_no FROM trades").fetchone()
        self.assertEqual(r, ("resolved", 1.0))


if __name__ == "__main__":
    unittest.main()
