#!/bin/bash
# Test the N-way race guard WITHOUT touching AWS, by shimming `aws` on PATH.
# The guard's failure mode is expensive and silent: stopping the job that actually
# got capacity throws away hours of paid GPU time, so the decision logic gets
# exercised against every state combination that matters before it runs for real.
set -uo pipefail
D=$(mktemp -d)
PASS=0; FAIL=0

# Fake `aws`: reads each job's state from $D/state/<job> as "PRIMARY SECONDARY".
mkdir -p "$D/bin" "$D/state"
cat > "$D/bin/aws" <<'SHIM'
#!/bin/bash
# describe-training-job --training-job-name X ... --query Q --output text
# stop-training-job --training-job-name X
CMD=$2
NAME=""
for ((i=1;i<=$#;i++)); do [ "${!i}" = "--training-job-name" ] && { j=$((i+1)); NAME=${!j}; }; done
QUERY=""
for ((i=1;i<=$#;i++)); do [ "${!i}" = "--query" ] && { j=$((i+1)); QUERY=${!j}; }; done
case "$CMD" in
  describe-training-job)
    read -r P S < "$STATE_DIR/$NAME" 2>/dev/null || exit 1
    [ "$P" = "ERR" ] && exit 1          # simulate a describe failure
    case "$QUERY" in
      TrainingJobStatus) echo "$P";;
      SecondaryStatus)   echo "$S";;
    esac;;
  stop-training-job)
    echo "$NAME" >> "$STATE_DIR/.stopped";;
esac
SHIM
chmod +x "$D/bin/aws"
export PATH="$D/bin:$PATH" STATE_DIR="$D/state"

# Shorten the loop so tests finish instantly.
sed -e 's/seq 1 720/seq 1 2/' -e 's/^  sleep 60$/  :/' -e 's/sleep 60; continue 2/continue 2/' \
    -e "s|^LOG=.*|LOG=$D/log|" "$(dirname "$0")/../pipeline/training/capacity_race_guard.sh" > "$D/guard.sh"
chmod +x "$D/guard.sh"

setup() {  # setup "<job> <primary> <secondary>" ...
  rm -f "$D/state"/* "$D/state/.stopped" "$D/log"
  for spec in "$@"; do set -- $spec; echo "$2 $3" > "$D/state/$1"; done
}
stopped() { touch "$D/state/.stopped"; tr "\n" " " < "$D/state/.stopped"; }
check() {  # check <name> <expected-stopped-set> <expected-winner-or-->
  local name=$1 want_stop=$2 want_win=$3
  local got_stop got_win
  got_stop=$(stopped | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ *$//')
  want_stop=$(echo "$want_stop" | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ *$//')
  got_win=$(grep -o 'WINNER=.*' "$D/log" | tail -1 | cut -d= -f2)
  [ -z "$got_win" ] && got_win="-"
  if [ "$got_stop" = "$want_stop" ] && [ "$got_win" = "$want_win" ]; then
    echo "PASS  $name"; PASS=$((PASS+1))
  else
    echo "FAIL  $name"
    echo "        stopped: got [$got_stop] want [$want_stop]"
    echo "        winner : got [$got_win] want [$want_win]"
    sed 's/^/        | /' "$D/log"
    FAIL=$((FAIL+1))
  fi
}

echo "=== N-way race guard logic ==="

setup "a InProgress Pending" "b InProgress Pending" "c InProgress Pending"
"$D/guard.sh" a b c
check "all pending -> nothing stopped, no winner" "" "-"

setup "a InProgress Pending" "b InProgress Training" "c InProgress Pending"
"$D/guard.sh" a b c
check "middle job wins -> both others stopped" "a c" "b"

setup "a InProgress Starting" "b InProgress Pending" "c InProgress Pending"
"$D/guard.sh" a b c
check "Starting counts as running (billing began)" "b c" "a"

setup "a InProgress Downloading" "b InProgress Pending"
"$D/guard.sh" a b
check "Downloading counts as running" "b" "a"

setup "a InProgress Training" "b InProgress Training" "c InProgress Pending"
"$D/guard.sh" a b c
check "two started same window -> keep first, stop rest" "b c" "a"

setup "a Stopped Stopped" "b InProgress Training" "c InProgress Pending"
"$D/guard.sh" a b c
check "already-stopped job is not re-stopped" "c" "b"

setup "a Completed Completed" "b Stopped Stopped"
"$D/guard.sh" a b
check "no candidates left -> no action at all" "" "-"

setup "a InProgress Training" "b Completed Completed"
"$D/guard.sh" a b
check "sole running job with a finished sibling is never stopped" "" "a"

setup "a ERR ERR" "b InProgress Training"
"$D/guard.sh" a b
check "describe failure must NOT be read as not-running (skip round)" "" "-"

# The most expensive possible bug: stopping the job that got capacity.
setup "a InProgress Pending" "b InProgress Training"
"$D/guard.sh" a b
if grep -q "^b$" "$D/state/.stopped" 2>/dev/null; then
  echo "FAIL  the winner was stopped"; FAIL=$((FAIL+1))
else
  echo "PASS  the winner is never among the stopped"; PASS=$((PASS+1))
fi

echo "=== $PASS passed, $FAIL failed ==="
rm -rf "$D"
exit $((FAIL > 0))
