#!/bin/bash
# N identical training jobs are queued across DIFFERENT capacity pools to race for
# whichever frees first. Exactly one may ever run: at ~$1.2-2/hr for 9+ hours,
# letting two start would double the bill for a duplicate result.
#
# Why this is free to do: SageMaker training quotas are PER INSTANCE TYPE (this
# account holds a separate limit of 1 for each ml.g5.* and ml.g6.* size), and Pending
# time is unbilled — billing starts at Starting, when an instance is allocated. So
# queueing one job across N pools buys N independent lottery tickets at no cost.
#
# Each racer needs its OWN checkpoint prefix: sharing one lets the winner resume the
# loser's partial checkpoint, silently mixing two runs.
#
# Usage: capacity_race_guard.sh <job1> <job2> [job3 ...]
#        RACE_GUARD_LOG=/path/to.log to redirect the log
# Invariants: never stops the last candidate, and never stops a winner.
# Tested (AWS stubbed, no network): tests/test_capacity_race_guard.sh
set -uo pipefail
REGION=${AWS_REGION:-us-east-1}
LOG=${RACE_GUARD_LOG:-/tmp/capacity_race_guard.log}
JOBS=("$@")
[ ${#JOBS[@]} -lt 2 ] && { echo "need >=2 jobs"; exit 2; }

sec()  { aws sagemaker describe-training-job --training-job-name "$1" --region $REGION \
           --query 'SecondaryStatus' --output text 2>/dev/null; }
prim() { aws sagemaker describe-training-job --training-job-name "$1" --region $REGION \
           --query 'TrainingJobStatus' --output text 2>/dev/null; }

# Anything past the queue means capacity was granted and billing has begun.
running() { case "$1" in Starting|Downloading|Training|Uploading) return 0;; *) return 1;; esac; }

echo "$(date '+%F %T') race guard started over ${#JOBS[@]} pools: ${JOBS[*]}" >> $LOG

for i in $(seq 1 720); do   # 720 x 60s = 12h ceiling
  line="$(date '+%F %T') [$i]"
  winner=""; losers=(); alive=0
  for J in "${JOBS[@]}"; do
    p=$(prim "$J"); s=$(sec "$J")
    line="$line  ${J##*-}=$p/$s"
    # Describe failures (throttle, transient) must not be read as "not running" —
    # that could stop a job that is actually training. Skip this round instead.
    if [ -z "$p" ] || [ -z "$s" ]; then
      echo "$line  DESCRIBE-FAILED on $J, skipping round" >> $LOG
      sleep 60; continue 2
    fi
    [ "$p" = "InProgress" ] || continue        # already stopped/finished: out of the race
    alive=$((alive + 1))
    if running "$s"; then
      # First running job encountered wins; any other running job is a duplicate.
      [ -z "$winner" ] && winner="$J" || losers+=("$J")
    else
      losers+=("$J")
    fi
  done
  echo "$line" >> $LOG

  if [ $alive -eq 0 ]; then
    echo "$(date '+%F %T') no candidates left InProgress; exiting" >> $LOG; exit 0
  fi

  if [ -n "$winner" ]; then
    echo "$(date '+%F %T') $winner got capacity -> stopping ${#losers[@]} loser(s)" >> $LOG
    # ${losers[@]} on an empty array is an unbound-variable error under `set -u`,
    # which would kill the guard before it recorded the winner. That happens exactly
    # when the winner is the only candidate left — the case that must work.
    for L in ${losers[@]+"${losers[@]}"}; do
      aws sagemaker stop-training-job --training-job-name "$L" --region $REGION >> $LOG 2>&1
      echo "$(date '+%F %T')   stopped $L" >> $LOG
    done
    echo "WINNER=$winner" >> $LOG; exit 0
  fi
  sleep 60
done
echo "$(date '+%F %T') 12h ceiling reached with none started" >> $LOG
