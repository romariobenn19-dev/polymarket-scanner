#!/usr/bin/env python3
"""
research/calibration.py - Are Polymarket prices systematically wrong anywhere?

    python research/calibration.py fetch     # download resolved markets + price history (cached)
    python research/calibration.py report    # write research/CALIBRATION.md

For every resolved yes/no market from the last 12 months it looks up the YES price
1, 3, 7, 14 and 30 days before the market's scheduled end, and compares it with what
actually happened. Only information available at that moment is used: a market counts
at a given point only if it was still open then (no hindsight).

If, say, markets priced around 8c resolve YES only 4% of the time, buying NO at 92c
has a real edge of about 4c per share. If prices match outcomes, there is no edge.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
# SAMPLE=volume: the highest-volume markets (biased: surprises attract volume).
# SAMPLE=recent (default): every market with $5k+ volume, most recent end date first (outcome can't affect selection).
SAMPLE = __import__("os").environ.get("SAMPLE", "recent")
CACHE = HERE / ("cache.db" if SAMPLE == "volume" else f"cache_{SAMPLE}.db")
OUT = HERE / ("CALIBRATION_by_volume.md" if SAMPLE == "volume" else "CALIBRATION.md")
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HORIZONS = [1, 3, 7, 14, 30]
BUCKETS = [0, 0.02, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 0.98, 1.0001]
TAKER_RATE = 0.05   # typical 2026 taker fee rate (fee/share = rate x p x (1-p)); geopolitics is 0
session = requests.Session()


def ts(s: str) -> float:
    s = s.replace("Z", "+00:00").replace(" ", "T")
    if s.endswith("+00"):
        s += ":00"
    return datetime.fromisoformat(s).timestamp()


def get(url, **params):
    for attempt in range(5):
        try:
            r = session.get(url, params=params, timeout=40)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)


def db():
    con = sqlite3.connect(CACHE)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS markets (id TEXT PRIMARY KEY, event_id TEXT, question TEXT, token TEXT,
        end_ts REAL, closed_ts REAL, outcome INT, volume REAL, neg_risk INT, tags TEXT);
    CREATE TABLE IF NOT EXISTS history (id TEXT PRIMARY KEY, points TEXT);
    CREATE TABLE IF NOT EXISTS snaps (id TEXT, days INT, price REAL, age_min REAL, PRIMARY KEY (id, days));
    """)
    return con


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


def fetch_snaps(seconds: float) -> int:
    """Hourly price at each snapshot time (only kept if at most 2 hours old). Resumable."""
    con = db()
    todo = []
    for mid, token, end, closed in con.execute("SELECT id, token, end_ts, closed_ts FROM markets"):
        for d in HORIZONS:
            t = int(end - d * 86400)
            if t < closed - 3600:
                todo.append((mid, token, d, t))
    have = {(r[0], r[1]) for r in con.execute("SELECT id, days FROM snaps")}
    todo = [x for x in todo if (x[0], x[2]) not in have]
    stop = time.time() + seconds

    def one(x):
        mid, token, d, t = x
        if time.time() > stop:
            return None
        try:
            h = get(f"{CLOB}/prices-history", market=token, startTs=t - 86400, endTs=t, fidelity=60).get("history", [])
        except Exception:
            return None
        if h and t - h[-1]["t"] <= 2 * 3600:
            return (mid, d, float(h[-1]["p"]), (t - h[-1]["t"]) / 60)
        return (mid, d, None, None)  # no fresh price: recorded so it is not asked again

    done = 0
    with ThreadPoolExecutor(8) as pool:
        for r in pool.map(one, todo):
            if r is None:
                continue
            con.execute("INSERT OR REPLACE INTO snaps VALUES (?,?,?,?)", r)
            done += 1
            if done % 200 == 0:
                con.commit()
                print(f"\rsnapshots: {done}/{len(todo)}", end="", flush=True)
    con.commit()
    left = len(todo) - done
    print(f"\rsnapshots: {done} fetched, {left} left")
    return left


def fetch(max_markets: int = 6000, min_volume: float = 5000) -> None:
    con = db()
    since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    until = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cursor, kept, seen = None, 0, 0
    while kept < max_markets:
        params = {"closed": "true", "limit": 100, "end_date_min": since, "end_date_max": until,
                  "volume_num_min": min_volume, "order": "volumeNum" if SAMPLE == "volume" else "endDate", "ascending": "false"}
        if cursor:
            params["after_cursor"] = cursor
        d = get(f"{GAMMA}/markets/keyset", **params)
        page = d.get("markets") or []
        for m in page:
            seen += 1
            try:
                prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                outcomes = json.loads(m.get("outcomes") or "[]")
                token = json.loads(m.get("clobTokenIds") or "[]")[0]
            except (ValueError, IndexError, TypeError):
                continue
            if outcomes != ["Yes", "No"] or sorted(prices) != [0.0, 1.0] or not m.get("endDate") or not m.get("closedTime"):
                continue
            ev = (m.get("events") or [{}])[0]
            tags = ",".join(t.get("label", "") for t in (ev.get("tags") or []))
            con.execute("INSERT OR IGNORE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?)", (
                m["id"], str(ev.get("id", m["id"])), m.get("question", ""), token, ts(m["endDate"]), ts(m["closedTime"]),
                int(prices[0] == 1.0), float(m.get("volumeNum") or 0), int(bool(m.get("negRisk"))), tags))
            kept += 1
        con.commit()
        cursor = d.get("next_cursor")
        print(f"\rmarkets: kept {kept} of {seen} seen", end="", flush=True)
        if not cursor or not page:
            break
    print()
    todo = [r for r in con.execute("SELECT id, token FROM markets WHERE id NOT IN (SELECT id FROM history)")]

    def one(row):
        mid, token = row
        try:
            h = get(f"{CLOB}/prices-history", market=token, interval="max", fidelity=720).get("history", [])
            return mid, [[int(x["t"]), float(x["p"])] for x in h]
        except Exception:
            return mid, None

    done = 0
    with ThreadPoolExecutor(8) as pool:
        for mid, pts in pool.map(one, todo):
            if pts is not None:
                con.execute("INSERT OR REPLACE INTO history VALUES (?,?)", (mid, json.dumps(pts)))
            done += 1
            if done % 200 == 0:
                con.commit()
                print(f"\rprice histories: {done}/{len(todo)}", end="", flush=True)
    con.commit()
    print(f"\rprice histories: {done}/{len(todo)}")


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


def price_at(points: list, t: float, max_age: float = 86400):
    """Last known price at or before t (no later than max_age old)."""
    best = None
    for pt, p in points:
        if pt <= t:
            best = (pt, p)
        else:
            break
    if best and t - best[0] <= max_age:
        return best[1]
    return None


def snapshots(con):
    """(horizon_days, yes_price, outcome, event_id, is_deadline, tags, market_id), from hourly prices
    no more than 2 hours old at the snapshot time, taken while the market was still open."""
    out = []
    for mid, ev, q, outcome, tags, d, p in con.execute(
            "SELECT m.id, m.event_id, m.question, m.outcome, m.tags, s.days, s.price FROM markets m JOIN snaps s USING (id) "
            "WHERE s.price IS NOT NULL"):
        if p <= 0.0 or p >= 1.0:
            continue
        deadline = (" by " in f" {q.lower()} ") or q.lower().startswith("by ")
        out.append((d, p, outcome, ev, deadline, tags, mid))
    return out


def bucket_of(p: float) -> int:
    for i in range(len(BUCKETS) - 1):
        if BUCKETS[i] <= p < BUCKETS[i + 1]:
            return i
    return len(BUCKETS) - 2


def label(i: int) -> str:
    return f"{BUCKETS[i] * 100:.0f}–{min(BUCKETS[i + 1], 1) * 100:.0f}¢"


def summarize(rows):
    """Market-weighted and event-weighted calibration for a set of snapshot rows."""
    n = len(rows)
    if not n:
        return None
    price = sum(r[1] for r in rows) / n
    yes = sum(r[2] for r in rows) / n
    se = math.sqrt(max(yes * (1 - yes), 1e-9) / n)
    by_ev = defaultdict(list)
    for r in rows:
        by_ev[r[3]].append(r)
    ev_price = sum(sum(r[1] for r in g) / len(g) for g in by_ev.values()) / len(by_ev)
    ev_yes = sum(sum(r[2] for r in g) / len(g) for g in by_ev.values()) / len(by_ev)
    # spread of event averages -> honest error bar when outcomes inside an event move together
    ev_vals = [sum(r[2] - r[1] for r in g) / len(g) for g in by_ev.values()]
    m = sum(ev_vals) / len(ev_vals)
    ev_se = math.sqrt(sum((v - m) ** 2 for v in ev_vals) / max(len(ev_vals) - 1, 1) / len(ev_vals))
    return dict(n=n, events=len(by_ev), price=price, yes=yes, se=se, ev_price=ev_price, ev_yes=ev_yes, ev_gap=ev_yes - ev_price, ev_se=ev_se)


def no_edge(s, taker: bool):
    """Average profit per $1 spent buying NO at (1 - price) and holding to the end (event-weighted)."""
    p_yes, yes = s["ev_price"], s["ev_yes"]
    cost = 1 - p_yes
    fee = TAKER_RATE * p_yes * (1 - p_yes) if taker else 0.0
    return ((1 - yes) - cost - fee) / cost


def yes_edge(s, taker: bool):
    p_yes, yes = s["ev_price"], s["ev_yes"]
    fee = TAKER_RATE * p_yes * (1 - p_yes) if taker else 0.0
    return (yes - p_yes - fee) / p_yes


def jumps(con, min_jump=0.10):
    """After a 12-hour move of 10c+ (3+ days before the end), what happened next?"""
    res = []
    for mid, end, closed, outcome, pts in con.execute(
            "SELECT m.id, m.end_ts, m.closed_ts, m.outcome, h.points FROM markets m JOIN history h USING (id)"):
        points = json.loads(pts)
        for (t0, p0), (t1, p1) in zip(points, points[1:]):
            if t1 > min(end, closed) - 3 * 86400 or t1 - t0 > 13 * 3600:
                continue
            move = p1 - p0
            if abs(move) < min_jump or p1 <= 0.03 or p1 >= 0.97:
                continue
            later = price_at(points, t1 + 2 * 86400, max_age=86400)
            if later is None:
                continue
            res.append((move, p1, later, outcome))
            break  # first big jump per market only (keeps markets independent)
    return res


def report() -> str:
    con = db()
    n_mk = con.execute("SELECT COUNT(DISTINCT id) FROM snaps WHERE price IS NOT NULL").fetchone()[0]
    n_ev = con.execute("SELECT COUNT(DISTINCT m.event_id) FROM markets m JOIN snaps s USING (id) WHERE s.price IS NOT NULL").fetchone()[0]
    snaps = snapshots(con)
    L = ["# Are Polymarket prices wrong anywhere?", "",
         f"{n_mk:,} resolved yes/no markets from {n_ev:,} events (last 12 months, $5k+ volume, "
         + ("the highest-volume markets: biased toward surprises" if SAMPLE == "volume" else "chosen by end date, most recent first, so outcomes can't affect which markets are included") + ").",
         "For each, the YES price 1, 3, 7, 14 and 30 days before its scheduled end, compared with what happened.",
         "A market only counts at a point in time if it was still open then, using its hourly price "
         "from no more than 2 hours earlier (no stale prices, no hindsight).", "",
         "**How to read it:** *Price* is what the market said; *Happened* is how often YES actually won. "
         "If *Happened* is lower than *Price*, YES was overpriced and buying NO had an edge. "
         "Numbers are averaged per event (a 20-candidate election counts once), with ± = 2 standard errors. "
         "A gap is only meaningful when it is bigger than its ±.", ""]

    for d in HORIZONS:
        rows = [r for r in snaps if r[0] == d]
        L += [f"## {d} day{'s' if d > 1 else ''} before the end ({len(rows):,} market snapshots)", "",
              "| YES price | Markets | Events | Price | Happened | Gap | NO edge (waiting order) | NO edge (instant, fee) | YES edge (waiting) |",
              "|---|---|---|---|---|---|---|---|---|"]
        for i in range(len(BUCKETS) - 1):
            s = summarize([r for r in rows if bucket_of(r[1]) == i])
            if not s or s["events"] < 10:
                continue
            L.append(f"| {label(i)} | {s['n']} | {s['events']} | {s['ev_price'] * 100:.1f}% | {s['ev_yes'] * 100:.1f}% | "
                     f"{s['ev_gap'] * 100:+.1f} ± {2 * s['ev_se'] * 100:.1f} | {no_edge(s, False) * 100:+.1f}% | "
                     f"{no_edge(s, True) * 100:+.1f}% | {yes_edge(s, False) * 100:+.1f}% |")
        L.append("")

    L += ["## Deadline markets (\"... by <date>?\") vs others, long shots under 20¢", "",
          "| Days before end | Type | Events | Price | Happened | Gap | NO edge (waiting order) |", "|---|---|---|---|---|---|---|"]
    for d in HORIZONS:
        for kind, flag in (("deadline", True), ("other", False)):
            s = summarize([r for r in snaps if r[0] == d and r[4] == flag and r[1] < 0.20])
            if s and s["events"] >= 10:
                L.append(f"| {d} | {kind} | {s['events']} | {s['ev_price'] * 100:.1f}% | {s['ev_yes'] * 100:.1f}% | "
                         f"{s['ev_gap'] * 100:+.1f} ± {2 * s['ev_se'] * 100:.1f} | {no_edge(s, False) * 100:+.1f}% |")
    L.append("")

    js = jumps(con)
    if js:
        L += ["## After a sudden 10¢+ move (12 hours, at least 3 days before the end)", "",
              "| Direction | Markets | Moved back within 2 days | Average move back | Price after jump | Happened |", "|---|---|---|---|---|---|"]
        for name, sign in (("Up", 1), ("Down", -1)):
            g = [j for j in js if (j[0] > 0) == (sign > 0)]
            if len(g) < 10:
                continue
            back = sum(1 for m, p1, later, _ in g if (later - p1) * sign < 0) / len(g)
            avg_back = sum(-(later - p1) * sign for m, p1, later, _ in g) / len(g)
            L.append(f"| {name} | {len(g)} | {back * 100:.0f}% | {avg_back * 100:+.1f}¢ | "
                     f"{sum(j[1] for j in g) / len(g) * 100:.0f}% | {sum(j[3] for j in g) / len(g) * 100:.0f}% |")
        L.append("")
    text = "\n".join(L)
    OUT.write_text(text + "\n")
    return text


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "fetch":
        fetch(int(sys.argv[2]) if len(sys.argv) > 2 else 6000)
    elif cmd == "snaps":
        left = fetch_snaps(float(sys.argv[2]) if len(sys.argv) > 2 else 520)
        if left:
            sys.exit(0)
    print(report())
