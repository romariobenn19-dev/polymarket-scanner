"""python -m unittest discover -s tests"""
import os, sys, unittest
from types import SimpleNamespace as NS
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import judge_bot as jdg
import settle_bot as sb


def book(bids, asks):
    lv = lambda ps: [{"price": str(p), "size": str(s)} for p, s in ps]
    return {"bids": lv(bids), "asks": lv(asks)}


class FakeClient:
    """Stands in for anthropic.Anthropic(): returns a fixed verdict and counts calls."""
    def __init__(self, verdict):
        self.verdict, self.calls = verdict, 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        usage = NS(input_tokens=10000, output_tokens=1000, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                   server_tool_use=NS(web_search_requests=2))
        return NS(stop_reason="tool_use", usage=usage,
                  content=[NS(type="tool_use", name="record_verdict", input=self.verdict)])


C = {"market_id": "m1", "event_id": "e1", "question": "Will X be named CEO by Nov 10?", "token": "t1", "fee_rate": 0.0,
     "hours_past": 30, "approx": 0.96, "end": "2026-11-10T00:00:00Z", "description": "Resolves YES if...", "source": ""}
YES_BOOK = book([(0.95, 500)], [(0.96, 500)])
V = {"decided": True, "outcome": "YES", "confidence": 0.99, "evidence": "Announced Nov 9", "source_url": "x", "risks": ""}


class Judge(unittest.TestCase):
    def bot(self, verdict, cap=1.0):
        return jdg.JudgeBot(sb.ConfigH(), ":memory:", client=FakeClient(verdict), daily_cap=cap)

    def test_buys_when_claude_confirms(self):
        b = self.bot(V)
        self.assertTrue(b.consider(C, YES_BOOK, 1000))
        cost = b.con.execute("SELECT cost FROM verdicts").fetchone()[0]
        self.assertAlmostEqual(cost, 10000 * 4e-6 + 1000 * 20e-6 + 2 * 0.01)

    def test_skips_when_not_public_or_opposite_or_unsure(self):
        for v, why in (({**V, "decided": False}, "not public"), ({**V, "outcome": "NO"}, "OTHER way"),
                       ({**V, "confidence": 0.9}, "not sure")):
            b = self.bot(v)
            self.assertFalse(b.consider(C, YES_BOOK, 1000))
            self.assertIn(why, b.con.execute("SELECT reason FROM seen").fetchone()[0])

    def test_verdict_is_reused_and_cap_respected(self):
        b = self.bot({**V, "decided": False})
        b.consider(C, YES_BOOK, 1000)
        b.consider(C, YES_BOOK, 2000)                       # within 12h: no new call
        self.assertEqual(b.client.calls, 1)
        b2 = self.bot(V, cap=0.0)
        self.assertFalse(b2.consider(C, YES_BOOK, 1000))
        self.assertEqual(b2.client.calls, 0)

    def test_api_failure_means_no_trade(self):
        b = self.bot(V)
        b.client.create = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertFalse(b.consider(C, YES_BOOK, 1000))
        self.assertIn("failed", b.con.execute("SELECT reason FROM seen").fetchone()[0])

    def test_report_lists_checks(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "i.db")
        b = jdg.JudgeBot(sb.ConfigH(), path, client=FakeClient(V))
        b.consider(C, YES_BOOK, 1000)
        b.con.execute("INSERT INTO marks VALUES (1000, 40, 9.6, 1)"); b.con.commit()
        text = jdg.report(path)
        self.assertIn("Version I", text)
        self.assertIn("Announced Nov 9", text)


if __name__ == "__main__":
    unittest.main()
