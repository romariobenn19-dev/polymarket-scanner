#!/usr/bin/env python3
"""saver.py - every 30 min: comparison + reports + database copies -> paper-results branch."""
import sqlite3
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "pm-results"


def run(cmd, cwd=HERE):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)


while True:
    try:
        run(["python3", "compare.py", str(OUT / "COMPARISON.md")])
        for v in "ABC":
            db = HERE / f"paper_{v}.db"
            if db.exists():
                src, dst = sqlite3.connect(db), sqlite3.connect(OUT / f"paper_{v}.db")
                src.backup(dst)
                src.close(); dst.close()
                run(["python3", "rewards_bot.py", "report", "--db", str(db), "--out", str(OUT / f"report_{v}.md")])
        run(["git", "add", "-A"], OUT)
        stamp = time.strftime("%H:%M", time.gmtime())
        if run(["git", "-c", "user.name=Scanner", "-c", "user.email=dev@localhost", "commit", "-qm", f"Paper results {stamp} UTC"], OUT).returncode == 0:
            r = run(["git", "push", "-q", "-u", "origin", "paper-results"], OUT)
            print(stamp, "pushed" if r.returncode == 0 else f"push failed: {r.stderr.strip()[:200]}", flush=True)
    except Exception as e:
        print("saver error:", e, flush=True)
    time.sleep(1800)
