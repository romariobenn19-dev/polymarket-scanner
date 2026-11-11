"""python -m unittest discover -s tests"""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rewards_bot as rb
import rewards_bot_d as rd


class FakeFeed(rd.LiveBooks):
    """LiveBooks without the network thread."""
    def __init__(self):
        import collections, threading
        self.lock = threading.Lock(); self.bids = {}; self.asks = {}; self.updated = {}
        self.trades = collections.defaultdict(lambda: collections.deque(maxlen=200))
        self.assets = []; self.generation = 0; self.connected = True; self.messages = 0; self._stop = True


def book_msg(asset, bids, asks):
    return {"event_type": "book", "asset_id": asset,
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


class Feed(unittest.TestCase):
    def test_book_snapshot_and_changes(self):
        f = FakeFeed(); f.subscribe(["t"])
        f.apply(book_msg("t", [(0.48, 100), (0.49, 50)], [(0.52, 80), (0.51, 40)]), now=1)
        self.assertEqual(rb.best(f.book("t")), (0.49, 0.51))
        f.apply({"event_type": "price_change", "price_changes": [
            {"asset_id": "t", "price": "0.49", "size": "0", "side": "BUY"},      # best bid removed
            {"asset_id": "t", "price": "0.505", "size": "25", "side": "SELL"},   # new best ask
            {"asset_id": "other", "price": "0.1", "size": "5", "side": "BUY"}]}, now=2)
        self.assertEqual(rb.best(f.book("t")), (0.48, 0.505))
        self.assertIsNone(f.book("other"))

    def test_trades_are_kept_with_time_window(self):
        f = FakeFeed(); f.subscribe(["t"])
        f.apply({"event_type": "last_trade_price", "asset_id": "t", "price": "0.5", "size": "30", "side": "SELL"}, now=100)
        self.assertEqual(len(f.recent_trades("t", 10, now=105)), 1)
        self.assertEqual(len(f.recent_trades("t", 10, now=120)), 0)


class Selection(unittest.TestCase):
    def test_swing_loss(self):
        flat = [0.5] * 48
        self.assertEqual(rd.swing_loss_per_day(flat, 0.02, 20), 0)
        jumpy = [0.5, 0.55] * 24          # 5c every hour, quote 2c away -> 3c x 20 shares x 24 per day
        self.assertAlmostEqual(rd.swing_loss_per_day(jumpy, 0.02, 20), 0.03 * 20 * 24, places=6)
        self.assertEqual(rd.swing_loss_per_day([0.5] * 5, 0.02, 20), float("inf"))  # too little history

    def test_microprice_leans_to_thin_side(self):
        # many buyers waiting, few sellers -> microprice above the plain midpoint
        b = {"bids": [{"price": "0.49", "size": "900"}], "asks": [{"price": "0.51", "size": "100"}]}
        self.assertGreater(rd.microprice(b), 0.50)


class BotD(unittest.TestCase):
    def setUp(self):
        self.feed = FakeFeed()
        self.bot = rd.PaperBotD(rd.ConfigD(), ":memory:", feed=self.feed)
        self.bot.picks["m"] = rb.Pick("m", "Calm", "t", 100, 4.5, 20, 0.01, "")
        self.bot.pos["m"] = rb.Position()
        self.bot.active = {"m"}
        self.feed.subscribe(["t"])
        self.now = 1000.0
        self._saved = (rd.time.time, rd.pick_markets_d, rd.sc.fetch_books)
        rd.time.time = lambda: self.now

    def tearDown(self):
        rd.time.time, rd.pick_markets_d, rd.sc.fetch_books = self._saved

    def tick(self, bid=0.49, ask=0.51, size=500):
        self.feed.apply(book_msg("t", [(bid, size)], [(ask, size)]), now=self.now)
        self.bot.cycle(dt=1)
        self.now += 1
        return self.bot.pos["m"]

    def test_quotes_in_calm_market(self):
        p = self.tick()
        self.assertIsNotNone(p.bid); self.assertIsNotNone(p.ask)

    def test_large_trade_pulls_orders(self):
        self.tick()
        self.feed.apply({"event_type": "last_trade_price", "asset_id": "t", "price": "0.5", "size": "400", "side": "BUY"}, now=self.now)
        p = self.tick()
        self.assertEqual((p.bid, p.ask), (None, None))
        reason = self.bot.con.execute("SELECT reason FROM pauses").fetchone()[0]
        self.assertIn("large trade", reason)

    def test_trade_through_our_bid_is_a_fill(self):
        p = self.tick()
        our_bid = p.bid
        self.feed.apply({"event_type": "last_trade_price", "asset_id": "t", "price": str(our_bid), "size": "20", "side": "SELL"}, now=self.now)
        self.bot.cfg.big_trade_usd = 1e9
        p = self.tick()
        self.assertEqual(p.yes, 20)

    def test_losing_market_gets_banned(self):
        self.bot.market_cash["m"] = -1.0       # lost $1 with nothing held
        rd.pick_markets_d = lambda cfg, banned, verbose=True: []
        rd.sc.fetch_books = lambda tokens: {}
        self.bot.reselect()
        self.assertIn("m", rd.banned_markets(self.bot.con, self.bot.cfg, now=self.now))


if __name__ == "__main__":
    unittest.main()
