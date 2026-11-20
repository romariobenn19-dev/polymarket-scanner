"""python -m unittest discover -s tests"""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import arb_watch as aw


def book(asks):
    return {"bids": [{"price": "0.01", "size": "100"}], "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


class SetCost(unittest.TestCase):
    def mk(self, tok, fee=False):
        return {"clobTokenIds": f'["{tok}", "x"]', "feesEnabled": fee, "feeSchedule": {"rate": 0.02}}

    def test_sums_every_outcome_with_fees(self):
        mks = [self.mk("a"), self.mk("b", fee=True)]
        cost = aw.set_cost(mks, {"a": book([(0.40, 10)]), "b": book([(0.55, 10)])})
        self.assertAlmostEqual(cost, 0.40 + 0.55 + 0.02 * 0.55 * 0.45)

    def test_thin_book_gives_none(self):
        mks = [self.mk("a"), self.mk("b")]
        self.assertIsNone(aw.set_cost(mks, {"a": book([(0.40, 10)]), "b": book([(0.55, 2)])}))


if __name__ == "__main__":
    unittest.main()
