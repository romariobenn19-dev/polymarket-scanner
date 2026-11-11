#!/usr/bin/env bash
# Start paper versions A, B, C (unless already running) and a saver that pushes results
# to the paper-results branch every 30 minutes. Safe to run again: it only starts what is missing.
set -u
cd "$(dirname "$0")"
HOURS="${HOURS:-17}"
start() {  # name, extra args
  if ! pgrep -f "rewards_bot.py run .*paper_$1.db" >/dev/null; then
    setsid nohup python3 rewards_bot.py run --hours "$HOURS" --db "paper_$1.db" ${2:-} >> "paper_$1.log" 2>&1 < /dev/null &
    echo "started $1"
  fi
}
start A ""
start B "--depth 0.25"
start C "--one-sided --markets 4"
if ! pgrep -f "rewards_bot_d.py run" >/dev/null; then
  setsid nohup python3 rewards_bot_d.py run --hours "${HOURS_D:-16}" --db paper_D.db >> paper_D.log 2>&1 < /dev/null &
  echo "started D"
fi

RESULTS=../pm-results
if [ ! -d "$RESULTS" ]; then
  git fetch -q origin paper-results 2>/dev/null && git worktree add -q "$RESULTS" paper-results 2>/dev/null \
    || git worktree add -q --orphan -b paper-results "$RESULTS"
fi
if ! pgrep -f "saver.py" >/dev/null; then
  setsid nohup python3 saver.py >> saver.log 2>&1 < /dev/null &
  echo "started saver"
fi
