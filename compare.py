#!/usr/bin/env python3
"""compare.py - side-by-side results of the paper-test versions."""
import os
import sqlite3
import sys
from datetime import datetime, timezone

VERSIONS = [
    ("A", "paper_A.db", "Version 2 as is: both sides, halfway out"),
    ("B", "paper_B.db", "Both sides, quarter of the way out (closer to the price)"),
    ("C", "paper_C.db", "Buy side only, 4 markets"),
    ("D", "paper_D.db", "Live feed, trade alarm, history-based picks, learns (started ~1h later)"),
]


def summary(path):
    if not os.path.exists(path):
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    first = con.execute("SELECT MIN(ts), MAX(ts) FROM cycles").fetchone()
    if not first or not first[0]:
        return None
    last = con.execute("SELECT equity, rewards, holdings FROM cycles ORDER BY ts DESC LIMIT 1").fetchone()
    worst = con.execute("SELECT MIN(equity) FROM cycles").fetchone()[0]
    hours = (first[1] - first[0]) / 3600
    fills = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    pauses = con.execute("SELECT COUNT(*) FROM pauses").fetchone()[0]
    markets = [r[0] for r in con.execute("SELECT DISTINCT question FROM accruals")]
    net = last[0] - 50
    return dict(hours=hours, net=net, rewards=last[1], trading=net - last[1], holdings=last[2], worst=worst - 50,
                fills=fills, pauses=pauses, per_day=net / hours * 24 if hours > 0.5 else None, markets=markets)


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = [f"# Paper test comparison ({now})", "", "Budget $50 each, same live order books, no real orders.", "",
         "| | " + " | ".join(f"**{v}**" for v, _, _ in VERSIONS) + " |", "|---|" + "---|" * len(VERSIONS)]
    rows = {v: summary(p) for v, p, _ in VERSIONS}
    def line(label, fn):
        L.append(f"| {label} | " + " | ".join(fn(rows[v]) if rows[v] else "–" for v, _, _ in VERSIONS) + " |")
    line("What it does", lambda r: "")
    L[-1] = "| What it does | " + " | ".join(d for _, _, d in VERSIONS) + " |"
    line("Hours running", lambda r: f"{r['hours']:.1f}")
    line("Rewards", lambda r: f"${r['rewards']:+.2f}")
    line("Gain/loss on shares", lambda r: f"${r['trading']:+.2f}")
    line("**Net result**", lambda r: f"**${r['net']:+.2f}**")
    line("Net per day (at this pace)", lambda r: f"${r['per_day']:+.2f}" if r["per_day"] is not None else "–")
    line("Worst point", lambda r: f"${r['worst']:+.2f}")
    line("Fills", lambda r: str(r["fills"]))
    line("Safety pauses", lambda r: str(r["pauses"]))
    L += [""]
    for v, _, _ in VERSIONS:
        if rows[v]:
            L.append(f"- **{v}** markets: " + "; ".join(m[:60] for m in rows[v]["markets"]))
    text = "\n".join(L)
    print(text)
    if len(sys.argv) > 1:
        open(sys.argv[1], "w").write(text + "\n")


if __name__ == "__main__":
    main()
