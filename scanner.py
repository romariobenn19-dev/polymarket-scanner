#!/usr/bin/env python3
"""
scanner.py - READ-ONLY Polymarket opportunity scanner. No account, no orders, no money.

    python scanner.py scan                  scan every 2 minutes for 60 minutes
    python scanner.py scan --minutes 480
    python scanner.py once                  one scan, print what it found
    python scanner.py report                summarize everything logged so far

What it looks for
  1. Multi-outcome arbitrage ("neg-risk" events: exactly one outcome can win)
       buy_all_yes: buy YES on every outcome. One pays $1. Profit if total cost + fees < $1.
                    Only valid when the outcome list is complete (not "augmented").
       buy_all_no:  buy NO on every outcome. All but one pay $1 (N-1 total).
                    Profit if total cost + fees < N-1.
     Candidates from the market list are re-checked against the live order books, and
     the number of complete sets you could actually buy at the best prices is recorded.
  2. Liquidity rewards: markets that pay a daily amount to resting orders near the price.
  3. Wide spreads on active markets (room for a market maker).

Fees (Polymarket docs, 2026): taker fee per share = rate * (p * (1 - p)) ** exponent,
read live from each market. Makers pay no fee. Arbitrage here assumes taker orders.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DB_PATH = "scan.db"
MIN_EDGE = 0.005
PAGE = 100  # the events API returns at most 100 per request  # ignore gaps under 0.5 cents per set (rounding / stale quotes)

session = requests.Session()
session.headers["User-Agent"] = "market-scanner/1.0"


def now_ts() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_json(url: str, **kw):
    for attempt in range(4):
        try:
            r = session.get(url, timeout=30, **kw)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


def fetch_events(min_liquidity: float = 1000) -> list[dict]:
    """All open events with at least `min_liquidity` (cursor paging; plain offsets stop at ~2,000)."""
    events, cursor = [], None
    for _ in range(1000):
        params = {"active": "true", "closed": "false", "limit": PAGE}
        if min_liquidity:
            params["liquidity_min"] = min_liquidity
        if cursor:
            params["after_cursor"] = cursor
        d = get_json(f"{GAMMA}/events/keyset", params=params)
        page = d.get("events") or []
        events += page
        cursor = d.get("next_cursor")
        if not cursor or not page:
            break
    return events


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def tradable(m: dict) -> bool:
    return bool(m.get("active")) and not m.get("closed") and m.get("acceptingOrders", True) and m.get("enableOrderBook", True)


def yes_token(m: dict) -> str | None:
    try:
        return json.loads(m.get("clobTokenIds") or "[]")[0]
    except (ValueError, IndexError):
        return None


def fee_per_share(m: dict, p: float) -> float:
    fs = m.get("feeSchedule") or {}
    if not m.get("feesEnabled") or not fs:
        return 0.0
    rate = float(fs.get("rate") or 0)
    exp = float(fs.get("exponent") or 1)
    return rate * (p * (1 - p)) ** exp


def yes_price(m: dict):
    try:
        return float(json.loads(m.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError, TypeError):
        return None


def resolved_no(m: dict) -> bool:
    p = yes_price(m)
    return bool(m.get("closed")) and p is not None and p <= 0.01


def resolved_yes(m: dict) -> bool:
    try:
        return m.get("closed") and float(json.loads(m.get("outcomePrices") or "[]")[0]) >= 0.99
    except (ValueError, IndexError, TypeError):
        return False


def fetch_books(token_ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(token_ids), 100):
        chunk = token_ids[i:i + 100]
        r = session.post(f"{CLOB}/books", json=[{"token_id": t} for t in chunk], timeout=30)
        r.raise_for_status()
        for b in r.json():
            out[b["asset_id"]] = b
    return out


def top(levels: list[dict], best_is_last=True):
    """(price, size) of the best level. CLOB lists the best bid/ask last."""
    if not levels:
        return None, 0.0
    lv = levels[-1] if best_is_last else levels[0]
    return float(lv["price"]), float(lv["size"])


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def check_event(e: dict) -> list[dict]:
    """Quick check from the market list's best bid/ask. Returns candidate opportunities."""
    if not (e.get("negRisk") or e.get("enableNegRisk")):
        return []
    markets = e.get("markets") or []
    if any(resolved_yes(m) for m in markets):
        return []  # already decided
    legs = [m for m in markets if tradable(m) and yes_token(m)]
    if len(legs) < 2:
        return []
    out = []
    asks = [_f(m.get("bestAsk")) for m in legs]
    bids = [_f(m.get("bestBid")) for m in legs]
    # Buying YES on every outcome only pays if the winner is among them: every outcome must be
    # tradable now or already settled as NO. A paused/unsettled outcome (often the winner) breaks it.
    complete = not e.get("negRiskAugmented") and all(tradable(m) or resolved_no(m) for m in markets)
    if complete and all(a and 0 < a < 1 for a in asks):
        cost = sum(asks) + sum(fee_per_share(m, a) for m, a in zip(legs, asks))
        if 1 - cost > MIN_EDGE:
            out.append({"kind": "buy_all_yes", "edge": 1 - cost})
    if all(b and 0 < b < 1 for b in bids):
        n = len(legs)
        no_prices = [1 - b for b in bids]
        cost = sum(no_prices) + sum(fee_per_share(m, q) for m, q in zip(legs, no_prices))
        if (n - 1) - cost > MIN_EDGE:
            out.append({"kind": "buy_all_no", "edge": (n - 1) - cost})
    for o in out:
        o.update(event_id=e["id"], title=e.get("title", ""), legs=legs, n=len(legs), slug=e.get("slug", ""))
    return out


def confirm(opp: dict, books: dict) -> dict:
    """Re-price an opportunity from live order books and size it at the best levels."""
    legs = opp["legs"]
    total, size = 0.0, float("inf")
    for m in legs:
        b = books.get(yes_token(m))
        if not b:
            return {**opp, "confirmed": False, "live_edge": None, "sets": 0, "profit": 0}
        if opp["kind"] == "buy_all_yes":
            p, s = top(b.get("asks", []))
            if p is None:
                return {**opp, "confirmed": False, "live_edge": None, "sets": 0, "profit": 0}
        else:  # NO ask = 1 - YES bid (books mirror each other)
            bp, s = top(b.get("bids", []))
            if bp is None:
                return {**opp, "confirmed": False, "live_edge": None, "sets": 0, "profit": 0}
            p = 1 - bp
        total += p + fee_per_share(m, p)
        size = min(size, s)
    payout = 1 if opp["kind"] == "buy_all_yes" else opp["n"] - 1
    edge = payout - total
    ok = edge > MIN_EDGE
    sets = size if ok else 0
    return {**opp, "confirmed": ok, "live_edge": edge, "sets": sets, "profit": edge * sets if ok else 0, "cost_per_set": total}


def reward_rows(events: list[dict]) -> list[dict]:
    rows = []
    for e in events:
        for m in e.get("markets") or []:
            if not tradable(m):
                continue
            rate = sum(float(r.get("rewardsDailyRate") or 0) for r in (m.get("clobRewards") or []))
            bid, ask = _f(m.get("bestBid")), _f(m.get("bestAsk"))
            rows.append({
                "market_id": m["id"], "question": m.get("question", ""), "slug": e.get("slug", ""),
                "daily_rate": rate, "max_spread": _f(m.get("rewardsMaxSpread")), "min_size": _f(m.get("rewardsMinSize")),
                "bid": bid, "ask": ask, "spread": (ask - bid) if bid is not None and ask is not None else None,
                "liquidity": _f(m.get("liquidityClob")) or 0.0, "volume24h": _f(m.get("volume24hrClob")) or _f(m.get("volume24hr")) or 0.0,
                "fee_rate": float(((m.get("feeSchedule") or {}).get("rate")) or 0) if m.get("feesEnabled") else 0.0,
            })
    return rows


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (ts REAL PRIMARY KEY, events INT, markets INT, candidates INT, seconds REAL);
CREATE TABLE IF NOT EXISTS opps (ts REAL, event_id TEXT, title TEXT, slug TEXT, kind TEXT, n INT,
  list_edge REAL, live_edge REAL, confirmed INT, sets REAL, profit REAL, cost_per_set REAL);
CREATE TABLE IF NOT EXISTS rewards (ts REAL, market_id TEXT, question TEXT, slug TEXT, daily_rate REAL, max_spread REAL,
  min_size REAL, bid REAL, ask REAL, spread REAL, liquidity REAL, volume24h REAL, fee_rate REAL);
CREATE INDEX IF NOT EXISTS opps_key ON opps(event_id, kind, ts);
"""


def db(path=DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def scan_once(con: sqlite3.Connection, keep_rewards: bool, min_liquidity: float = 1000, verbose=True) -> list[dict]:
    t0 = now_ts()
    events = fetch_events(min_liquidity)
    cands = [o for e in events for o in check_event(e)]
    tokens = sorted({yes_token(m) for o in cands for m in o["legs"]})
    books = fetch_books(tokens) if tokens else {}
    results = [confirm(o, books) for o in cands]
    n_markets = sum(1 for e in events for m in e.get("markets") or [] if tradable(m))
    con.execute("INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?)", (t0, len(events), n_markets, len(cands), now_ts() - t0))
    con.executemany("INSERT INTO opps VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        (t0, r["event_id"], r["title"], r["slug"], r["kind"], r["n"], r["edge"], r["live_edge"], int(r["confirmed"]),
         r["sets"], r["profit"], r.get("cost_per_set")) for r in results])
    if keep_rewards:
        con.executemany("INSERT INTO rewards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (t0, r["market_id"], r["question"], r["slug"], r["daily_rate"], r["max_spread"], r["min_size"], r["bid"], r["ask"],
             r["spread"], r["liquidity"], r["volume24h"], r["fee_rate"]) for r in reward_rows(events)])
    con.commit()
    if verbose:
        live = [r for r in results if r["confirmed"]]
        print(f"[{iso(t0)}] {len(events)} events, {n_markets} markets | {len(cands)} candidates, "
              f"{len(live)} confirmed on live books ({now_ts() - t0:.1f}s)")
        for r in sorted(live, key=lambda r: -r["profit"])[:5]:
            print(f"   {r['kind']:11s} edge {r['live_edge'] * 100:5.2f}¢/set x {r['sets']:,.0f} sets = ${r['profit']:,.2f}  {r['title'][:70]}")
    return results


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def report(con: sqlite3.Connection) -> str:
    L = []
    scans = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts), AVG(events), AVG(markets), AVG(seconds) FROM scans").fetchone()
    if not scans[0]:
        return "No scans yet. Run: python scanner.py scan"
    n, t_min, t_max, ev, mk, secs = scans
    hours = (t_max - t_min) / 3600
    gap = (t_max - t_min) / max(n - 1, 1)
    L += [f"# Polymarket scan report", "",
          f"{n} scans from {iso(t_min)} to {iso(t_max)} UTC ({hours:.1f} h, one every ~{gap:.0f}s)",
          f"About {ev:.0f} open events and {mk:.0f} open markets per scan.", ""]

    # --- arbitrage
    L += ["## 1. Multi-outcome arbitrage (after taker fees)", ""]
    rows = con.execute("""SELECT event_id, kind, title, slug, n, COUNT(*), SUM(confirmed), MAX(live_edge), MAX(sets), MAX(profit),
                                 MIN(ts), MAX(ts) FROM opps GROUP BY event_id, kind""").fetchall()
    cand_scans = con.execute("SELECT COUNT(*) FROM opps").fetchone()[0]
    conf = [r for r in rows if r[6]]
    L.append(f"- Looked promising from the market list: {len(rows)} distinct events ({cand_scans} sightings)")
    L.append(f"- Still there on the live order books: **{len(conf)} distinct events**")
    if conf:
        total_once = sum(r[9] or 0 for r in conf)
        L.append(f"- If you had caught each one once at its best: **${total_once:,.2f}** in total")
        L += ["", "| Event | Type | Outcomes | Best edge per $1 set | Sets available | Max profit | Seen in scans | First → last seen |",
              "|---|---|---|---|---|---|---|---|"]
        for r in sorted(conf, key=lambda r: -(r[9] or 0))[:20]:
            flag = " ⚠️ check by hand" if r[7] > 0.20 else ""
            L.append(f"| {r[2][:60]}{flag} | {r[1]} | {r[4]} | {r[7] * 100:.2f}¢ | {r[8]:,.0f} | ${r[9]:,.2f} | {r[6]} | "
                     f"{iso(r[10])[11:16]} → {iso(r[11])[11:16]} |")
    else:
        L.append("- None survived the live check: the gaps were stale quotes or too small after fees.")
    L.append("")

    # --- rewards
    last = con.execute("SELECT MAX(ts) FROM rewards").fetchone()[0]
    if last:
        L += ["## 2. Liquidity rewards (latest scan)", "",
              "Paid daily to resting buy and sell orders kept within the max spread of the middle price. The money is shared "
              "with everyone else quoting there, so less existing liquidity means a bigger share. Listed: markets with "
              "at least $1,000 traded in 24h and a spread of 10¢ or less.", ""]
        tot = con.execute("SELECT COUNT(*), SUM(daily_rate) FROM rewards WHERE ts=? AND daily_rate>0", (last,)).fetchone()
        L.append(f"{tot[0]} markets pay rewards, **${tot[1] or 0:,.0f}/day** in total.")
        L += ["", "| Market | $/day | Max spread | Min size | Spread now | Liquidity | $/day per $1k liquidity |", "|---|---|---|---|---|---|---|"]
        for r in con.execute("""SELECT question, daily_rate, max_spread, min_size, spread, liquidity, daily_rate*1000/(liquidity+100) AS ratio
                                FROM rewards WHERE ts=? AND daily_rate>0 AND volume24h>=1000 AND spread IS NOT NULL AND spread<=0.10
                                ORDER BY ratio DESC LIMIT 15""", (last,)):
            L.append(f"| {r[0][:60]} | ${r[1]:,.0f} | {r[2] or 0:.1f}¢ | {r[3] or 0:,.0f} | "
                     f"{(r[4] or 0) * 100:.1f}¢ | ${r[5]:,.0f} | ${r[6]:.2f} |")
        L.append("")

        # --- spreads
        L += ["## 3. Widest spreads on busy markets (latest scan)", "",
              "Markets with at least $5,000 traded in 24h. A wide spread is room for a maker, but also a sign of risk.", ""]
        L += ["| Market | Bid | Ask | Spread | 24h volume | Taker fee rate |", "|---|---|---|---|---|---|"]
        for r in con.execute("""SELECT question, bid, ask, spread, volume24h, fee_rate FROM rewards
                                WHERE ts=? AND volume24h>=5000 AND spread IS NOT NULL ORDER BY spread DESC LIMIT 15""", (last,)):
            L.append(f"| {r[0][:60]} | {r[1]:.3f} | {r[2]:.3f} | {r[3] * 100:.1f}¢ | ${r[4]:,.0f} | {r[5] * 100:.1f}% |")
    L += ["", "_Read-only scan. Profits assume taker orders at the best prices shown and ignore gas/withdrawal costs and the "
          "risk that some legs fill and others don't._"]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Polymarket opportunity scanner")
    ap.add_argument("command", choices=["scan", "once", "report"])
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--every", type=float, default=120, help="seconds between scan starts (a scan takes ~1 min)")
    ap.add_argument("--min-liquidity", type=float, default=1000, help="skip events with less liquidity ($)")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out", help="also write the report to this file")
    args = ap.parse_args(argv)
    con = db(args.db)

    if args.command == "report":
        text = report(con)
        print(text)
        if args.out:
            open(args.out, "w").write(text + "\n")
        return 0
    if args.command == "once":
        scan_once(con, keep_rewards=True, min_liquidity=args.min_liquidity)
        return 0

    end = now_ts() + args.minutes * 60
    i = 0
    while now_ts() < end:
        started = now_ts()
        try:
            # reward/spread snapshot every 10th scan keeps the database small
            scan_once(con, keep_rewards=(i % 10 == 0), min_liquidity=args.min_liquidity)
        except Exception as e:  # keep going through network hiccups
            print(f"[{iso(started)}] scan failed: {e}", file=sys.stderr)
        i += 1
        time.sleep(max(0, args.every - (now_ts() - started)))
    print(report(con))
    return 0


if __name__ == "__main__":
    sys.exit(main())
