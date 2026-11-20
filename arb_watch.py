#!/usr/bin/env python3
"""
arb_watch.py - WATCH ONLY: counts "sum-to-one" gaps. Places no trades, not even paper ones.

    python arb_watch.py run --hours 6
    python arb_watch.py report

In a multi-outcome market (e.g. "Fed decision: cut / hold / hike") exactly one outcome pays $1.
If buying one YES share of EVERY outcome from the live order book costs less than $1 after fees,
the difference is locked-in profit at settlement. (Two-sided yes/no markets cannot have this gap on
Polymarket: the NO price is the mirror of the YES book.)

Every 30 minutes this records each complete set that costs under $0.99 for 5 shares of every outcome:
cost, depth, days until the market's end date, and the yearly return that implies. The question it
answers: do gaps appear often enough, with short enough waits, to be worth building a bot for?

Caveats recorded with each gap: some events can add new outcomes later ("augmented" sets). Then a
set bought today may not cover the final winner, so those gaps are not risk-free and are flagged.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime

import jump_bot as jb
import scanner as sc

SHARES = 5
SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (ts REAL, events INT, sets INT, gaps INT);
CREATE TABLE IF NOT EXISTS gaps (ts REAL, event_id TEXT, title TEXT, outcomes INT, cost REAL, days_left REAL,
    yearly REAL, augmented INT);
"""


def set_cost(markets: list, books: dict):
    """Cost of SHARES of every outcome's YES (per share, fees included), or None if any book is too thin."""
    total = 0.0
    for m in markets:
        b = books.get(sc.yes_token(m))
        asks = jb.yes_levels(b)[1] if b else []
        avg = jb.walk(asks, SHARES) if asks else None
        if avg is None:
            return None
        fee = float((m.get("feeSchedule") or {}).get("rate") or 0) if m.get("feesEnabled") else 0.0
        total += avg + fee * avg * (1 - avg)
    return total


def scan(con, now: float) -> int:
    events = [e for e in sc.fetch_events(min_liquidity=5000) if e.get("negRisk")]
    n_sets, found = 0, 0
    for e in events:
        open_mk = [m for m in e.get("markets", []) if not m.get("closed")]
        mk = [m for m in open_mk if sc.tradable(m)]
        if len(mk) < 2 or len(mk) != len(open_mk):
            continue                                   # not a complete, tradable set
        cost = set_cost(mk, sc.fetch_books([sc.yes_token(m) for m in mk]))
        if cost is None:
            continue
        n_sets += 1
        if cost < 0.99:
            try:
                end = datetime.fromisoformat(e["endDate"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                end = now + 365 * 86400
            days = max((end - now) / 86400, 1)
            yearly = ((1 / cost) ** (365 / days) - 1) * 100
            con.execute("INSERT INTO gaps VALUES (?,?,?,?,?,?,?,?)",
                        (now, str(e.get("id")), e.get("title", ""), len(mk), cost, days, yearly,
                         int(bool(e.get("negRiskAugmented")))))
            found += 1
    con.execute("INSERT INTO scans VALUES (?,?,?,?)", (now, len(events), n_sets, found))
    con.commit()
    print(f"[{sc.iso(now)}] {n_sets} complete sets checked, {found} under $0.99", flush=True)
    return found


def report(db_path="paper_ARB.db") -> str:
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    n, first, last = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM scans").fetchone()
    if not n:
        return "No scans yet."
    L = ["# Sum-to-one gaps (watch only, no trades)", "",
         f"{n} scans over {(last - first) / 3600:.1f} hours. A gap = buying {SHARES} shares of every outcome costs under $0.99.", ""]
    rows = con.execute("""SELECT title, outcomes, MIN(cost), MIN(days_left), MAX(yearly), MAX(augmented), COUNT(*)
                          FROM gaps GROUP BY event_id ORDER BY MIN(cost) LIMIT 20""").fetchall()
    short = con.execute("SELECT COUNT(DISTINCT event_id) FROM gaps WHERE days_left <= 14 AND augmented = 0").fetchone()[0]
    L += [f"**Different markets with a gap: {len(rows)}. Of those, settling within 14 days and not open to new outcomes: {short}.**", ""]
    if rows:
        L += ["| Market | Outcomes | Best cost | Profit | Days to end | Yearly | New outcomes possible | Times seen |",
              "|---|---|---|---|---|---|---|---|"]
        for t, o, cost, days, yr, aug, seen in rows:
            L.append(f"| {t[:55]} | {o} | ${cost:.3f} | {(1 - cost) * 100:.1f}% | {days:.0f} | {yr:.0f}% | {'yes' if aug else 'no'} | {seen} |")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Watch-only counter of sum-to-one gaps")
    ap.add_argument("command", choices=["run", "report"])
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--db", default="paper_ARB.db")
    ap.add_argument("--every", type=int, default=1800, help="seconds between scans")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    if args.command == "report":
        text = report(args.db)
        print(text)
        if args.out:
            open(args.out, "w").write(text + "\n")
        return 0
    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)
    end = time.time() + args.hours * 3600
    while time.time() < end:
        started = time.time()
        try:
            scan(con, started)
        except Exception as e:
            print(f"[{sc.iso(started)}] scan failed: {e}", file=sys.stderr, flush=True)
        time.sleep(max(0, min(args.every - (time.time() - started), end - time.time())))
    print(report(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
