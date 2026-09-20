#!/bin/bash
set -uo pipefail
task_app=${AIDLC_TASK_APP:-/app}
task_tests=${AIDLC_TASK_TESTS:-/tests}
reward_dir=${AIDLC_REWARD_DIR:-/logs/verifier}
mkdir -p "$reward_dir"
export PYTHONPATH="$task_app"
export PYTHONDONTWRITEBYTECODE=1
if python -m unittest discover -s "$task_tests" -p 'test_*.py' -v > "$reward_dir/test-output.txt" 2>&1; then
  echo 1 > "$reward_dir/reward.txt"
else
  echo 0 > "$reward_dir/reward.txt"
fi
