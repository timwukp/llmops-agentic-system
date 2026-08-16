#!/usr/bin/env python3
"""How reliable is the agent protocol, really? Plan, dispatch and score the probe.

    python3 tools/probe_protocol_reliability.py plan   --target 0.80
    python3 tools/probe_protocol_reliability.py launch --n 14 --spread-days 7 \\
        --artifact s3://BUCKET/runs/RUN/model.tar.gz --student ORG/MODEL [--dry-run]
    python3 tools/probe_protocol_reliability.py collect --state probe.json [--json out.json]

Why this exists. One `deploy_only` rehearsal succeeded on 2026-08-15, and one success is
1/1 -- Wilson 95% [0.207, 1.000], an interval that contains "works four times in five" and
"works one time in five" with equal comfort. Every claim this platform makes about
stability currently rests on that interval. The only way out is to run the cheap mode
enough times to make the interval say something, and the only honest way to decide "enough"
is to derive it rather than pick it (see `runs_needed`).

WHAT IT COSTS, AND WHY THAT NUMBER. `launch` spends real money: each probe run provisions a
GPU endpoint and tears it down. `PROBE_UNIT_COST_USD` is the MEASURED cost of the
2026-08-15 rehearsal, not an estimate, and `plan` prints the total before anything is
dispatched so the spend is a decision somebody made rather than a surprise on a bill.
Building this tool is free; running it is not, and the two are deliberately separate
authorizations. `--dry-run` is the default in tests and CI and makes no AWS call at all.

WHAT IT MEASURES. Two different rates, reported side by side because they answer different
questions and only the second one is actionable:

  * run level -- did the run reach `completed`? This is the number a human asks for
    ("is it stable?") and the one the sample size is derived from.
  * per stage -- what share of agent turns ended in a structured tool call? Computed from
    the `protocol#` rows this PR's driver change writes (`protocol_rollup`, reused from the
    driver module rather than reimplemented). At p = 0.8421 per stage, a five-stage lane
    predicts 42% and a twelve-stage path 13%, which is WHY the run-level number is low.
    The per-stage rate is the one any fix has to move, so it is the one to watch.

An unknown outcome is never a pass. A run still executing, a run row that is absent, and a
dispatch whose invoke never confirmed all read as `unknown`, and `collect` cannot exit 0
while one exists -- the same rule tools/audit_landed.py and tools/audit_drift.py follow.

EXIT CODES (pinned by tests/test_probe_runner.py):

    0   every dispatched slot reached `completed`, and there are at least as many of them
        as `runs_needed` requires -- the claim is earned
    1   at least one probe run FAILED (failed / escalated / stopped)
    2   no failures, but not conclusive: a slot is still running, unconfirmed, or the
        sample is smaller than the target needs
    3   usage error
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import math
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent


def _module(name: str, rel: str):
    """Import an orchestration handler by path, under a name that cannot collide.

    Both handlers are called `handler.py`, and the driver's is imported for exactly one
    pure function (`protocol_rollup`) while start_pipeline's is imported for the mode
    prerequisite tables. Importing them is safe: neither reads a required environment
    variable at module scope.
    """
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


start_pipeline = _module("probe_start_pipeline", "orchestration/start_pipeline/handler.py")
driver = _module("probe_harness_driver", "orchestration/harness_driver/handler.py")

#: Measured, not estimated: the 2026-08-15 `deploy_only` rehearsal (run
#: run-20260815T...-r6e, five states, endpoint created and torn down) cost $0.53 end to
#: end. Probe runs are that same mode with the same artifact, so this is the unit price.
#: A test pins both totals below, because a silent edit here changes what a human is
#: agreeing to spend.
PROBE_UNIT_COST_USD = 0.53
#: 95% confidence, the same alpha the eval gate's Wilson bounds use.
DEFAULT_ALPHA = 0.05
Z_95 = 1.96

START_FN = "llmops-start-pipeline"
RUNS_TABLE = "llmops-pipeline-runs"
EVENTS_TABLE = "llmops-stage-events"

#: The mode being probed. Cheapest path that still exercises a real agent loop across
#: several stages, and the only one with a measured unit cost.
PIPELINE_MODE = "deploy_only"
#: Self-describing, so a probe run is never mistaken for work somebody asked for -- the
#: rule tools/probe_liveness_resurrection.py's synthetic subject follows.
TRIGGER_SOURCE = "protocol-probe"

#: A run in this state is over; `completed` is the only one that is a pass. Taken from the
#: driver rather than restated: that tuple is derived from the only writers of a terminal
#: runs.status, and a probe that scored `escalated` as a pass would report the exact
#: failure it exists to count as a success.
TERMINAL_STATES = driver.UNREACHABLE_RUN_STATES
PASS_STATE = "completed"


def runs_needed(target: float, alpha: float = DEFAULT_ALPHA) -> int:
    """How many consecutive successes justify "the true pass rate is at least `target`"?

    Derived, never picked. If the true rate were exactly `target`, the chance of `n`
    successes in a row is `target ** n`; requiring that to be at most `alpha` before we
    are allowed to make the claim gives `n >= log(alpha) / log(target)`.

    CEILING, not rounding: `log(.05)/log(.8)` is 13.42, and 13 runs leave a 5.5% chance of
    a clean sweep from a system that only works 80% of the time -- outside the confidence
    the number is being quoted at. Rounding down to 13 would make the whole exercise claim
    something it did not buy, which is why a negative control mutates `ceil` to `round`.
    """
    if not 0.0 < target < 1.0:
        raise ValueError(f"target must be strictly between 0 and 1, got {target!r}: "
                         "a target of 1.0 is unfalsifiable by any finite number of runs.")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be strictly between 0 and 1, got {alpha!r}")
    return math.ceil(math.log(alpha) / math.log(target))


def cost_usd(n: int) -> float:
    """What `n` probe runs cost, to the cent."""
    return round(n * PROBE_UNIT_COST_USD, 2)


def wilson(score: float, n: int, z: float = Z_95):
    """Wilson score interval -- the same formula as the console's `_wilson`, verbatim.

    Deliberately a second copy rather than an import: the console is a separately deployed
    bundle (deploy/console/) and this is a repo tool, so there is no shared module to put it
    in. The DIRECTIVE_SK precedent applies -- a duplicated contract is allowed as long as a
    test compares the two implementations over a table of inputs and turns red the day they
    disagree, which `test_the_wilson_interval_matches_the_consoles` does to 1e-12.
    """
    if n <= 0:
        return None
    d = 1.0 + z * z / n
    centre = (score + z * z / (2 * n)) / d
    half = z * math.sqrt(max(score * (1 - score), 0.0) / n + z * z / (4 * n * n)) / d
    return centre - half, centre + half


def _iso(when: datetime.datetime) -> str:
    return when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str) -> datetime.datetime:
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ") \
        .replace(tzinfo=datetime.timezone.utc)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def slot_schedule(n: int, spread_days: float, start: datetime.datetime) -> list:
    """`n` due times spread evenly over `spread_days`, first one due immediately.

    Spread rather than burst for a reason that is not politeness to the API: fourteen runs
    dispatched in one minute share one model deployment, one regional capacity state and one
    throttling window, so they measure that minute rather than the system. Correlated
    samples would inflate the confidence this whole exercise is built to earn honestly.
    """
    step = datetime.timedelta(days=spread_days) / n if n else datetime.timedelta(0)
    return [{"slot": i, "due_at": _iso(start + step * i), "attempted_at": None,
             "run_id": None, "dispatched_at": None, "error": None}
            for i in range(n)]


def new_state(n: int, target: float, alpha: float, spread_days: float, artifact: str,
              student: str, start: datetime.datetime) -> dict:
    return {"created_at": _iso(start), "pipeline_mode": PIPELINE_MODE,
            "target": target, "alpha": alpha, "n": n,
            "unit_cost_usd": PROBE_UNIT_COST_USD, "budget_usd": cost_usd(n),
            "artifact": artifact, "student": student,
            "slots": slot_schedule(n, spread_days, start)}


def load_state(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def save_state(path: str, state: dict) -> None:
    """Write the ledger atomically. A half-written slot list is a double-dispatch risk."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def dispatch_payload(artifact: str, student: str, k: int, n: int) -> dict:
    """The event `llmops-start-pipeline` is invoked with, one probe run.

    Every key is here because `MODE_REQUIRED_PARAMS` / `MODE_REQUIRED_ROLES` demand it for
    `deploy_only`: the artifact because this mode enters at Deploy and has no finetune stage
    to read one from, and an EXPLICIT student because `DEFAULT_MODELS` would otherwise fill
    the role silently with a 1.7B base and merge an 8B artifact's adapters onto the wrong
    weights -- a failure that costs forty minutes and a GPU endpoint to discover.
    """
    return {"trigger_source": TRIGGER_SOURCE,
            "params": {"pipeline_mode": PIPELINE_MODE,
                       "model_artifact_uri": artifact,
                       "models": {"student": student},
                       "note": f"protocol probe {k}/{n}"}}


def refuse_undispatchable(payload: dict) -> None:
    """Run start_pipeline's own prerequisite check over the payload, here, for free.

    Reusing `_check_mode_prerequisites` instead of restating the tables is the point: a mode
    that grows a new requirement gets it enforced here the same day, and the refusal costs
    nothing because it happens before the invoke. Raises ValueError with the real message.
    """
    params = payload.get("params") or {}
    merged = start_pipeline._merge_params(params, {})
    start_pipeline._check_mode_prerequisites(
        merged, set(start_pipeline._role_assignments(params, "params")))


def due_slots(state: dict, now: datetime.datetime) -> list:
    """Slots whose time has come and which were never attempted.

    `attempted_at` and not `run_id` is what makes re-running safe: the attempt is recorded
    BEFORE the invoke, so a crash between the two leaves a slot that is skipped forever
    rather than dispatched twice. That slot is then reported as unconfirmed, which `collect`
    scores as unknown -- a lost sample is cheap, a duplicate charge is not, and a silent
    double dispatch would also break the independence the spread exists to protect.
    """
    return [s for s in state["slots"]
            if not s.get("attempted_at") and _parse_iso(s["due_at"]) <= now]


def launch(state: dict, path, clients, now: datetime.datetime, dry_run: bool,
           out=print) -> int:
    """Dispatch every due slot. Returns the number dispatched (0 under --dry-run)."""
    due = due_slots(state, now)
    pending = [s for s in state["slots"] if not s.get("attempted_at")]
    out("probe %s: %d of %d slot(s) dispatched, %d due now, %d still pending"
        % (state["pipeline_mode"], sum(1 for s in state["slots"] if s.get("run_id")),
           state["n"], len(due), len(pending)))
    if dry_run:
        out("  schedule (UTC), $%.2f total, one slot per invocation:" % state["budget_usd"])
        for s in state["slots"]:
            out("    slot %2d/%d  %s" % (s["slot"] + 1, state["n"], s["due_at"]))
    if not due:
        nxt = min((s["due_at"] for s in pending), default=None)
        out("  nothing due" + (f"; next slot at {nxt}" if nxt else "; all slots used"))
        return 0

    sent = 0
    for slot in due:
        payload = dispatch_payload(state["artifact"], state["student"],
                                   slot["slot"] + 1, state["n"])
        refuse_undispatchable(payload)
        if dry_run:
            out("  [dry-run] slot %d/%d due %s would invoke %s with:\n%s"
                % (slot["slot"] + 1, state["n"], slot["due_at"], START_FN,
                   json.dumps(payload, indent=4)))
            continue
        slot["attempted_at"] = _iso(now)
        if path:
            save_state(path, state)           # before the invoke, never after
        try:
            resp = clients["lambda"].invoke(
                FunctionName=START_FN, InvocationType="RequestResponse",
                Payload=json.dumps(payload).encode())
            body = json.loads(resp["Payload"].read().decode() or "{}")
            slot["run_id"] = (body or {}).get("run_id")
            if not slot["run_id"]:
                slot["error"] = f"invoke returned no run_id: {str(body)[:200]}"
        except Exception as exc:              # noqa: BLE001 — recorded, never retried here
            slot["error"] = f"{type(exc).__name__}: {exc}"
        slot["dispatched_at"] = _iso(_now())
        if path:
            save_state(path, state)
        if slot["run_id"]:
            sent += 1
            out("  slot %d/%d dispatched: %s ($%.2f)"
                % (slot["slot"] + 1, state["n"], slot["run_id"], PROBE_UNIT_COST_USD))
        else:
            out("  slot %d/%d UNCONFIRMED, not retried: %s"
                % (slot["slot"] + 1, state["n"], slot["error"]))
    if dry_run:
        out("  dry run: nothing was dispatched, nothing was spent, no AWS call was made")
    else:
        out("  spent this pass: $%.2f" % cost_usd(sent))
    return sent


def _protocol_rows(ddb, run_id: str) -> list:
    """Every `protocol#` row of one run, paginated. Read-only."""
    from boto3.dynamodb.conditions import Key
    table = ddb.Table(EVENTS_TABLE)
    kc = Key("run_id").eq(run_id) & Key("sk").begins_with(driver.PROTOCOL_SK)
    items, start_key = [], None
    while True:
        kw = {"KeyConditionExpression": kc}
        if start_key:
            kw["ExclusiveStartKey"] = start_key
        resp = table.query(**kw)
        items += resp.get("Items", [])
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            return items


def outcome_of(row: dict | None, slot: dict) -> tuple:
    """(outcome, why) for one slot: "pass", "fail" or "unknown". Never guesses upward."""
    if not slot.get("run_id"):
        if slot.get("attempted_at"):
            return "unknown", f"dispatch never confirmed ({slot.get('error') or '?'})"
        return "unknown", "not dispatched yet"
    if not row:
        return "unknown", "no run row (not visible yet, or unreadable)"
    status = str(row.get("status") or "")
    if status == PASS_STATE:
        return "pass", status
    if status in TERMINAL_STATES:
        return "fail", status
    return "unknown", f"still {status or 'unknown'}"


def collect(state: dict, clients) -> dict:
    """Score every slot and roll the protocol counters up. Every call here is a read."""
    runs = clients["ddb"].Table(RUNS_TABLE)
    results, protocol_rows = [], []
    for slot in state["slots"]:
        row, why_unreadable = None, None
        if slot.get("run_id"):
            try:
                row = runs.get_item(Key={"run_id": slot["run_id"]}).get("Item")
            except Exception as exc:          # noqa: BLE001 — unreadable is not a pass
                why_unreadable = f"{type(exc).__name__}: {exc}"
            try:
                protocol_rows += _protocol_rows(clients["ddb"], slot["run_id"])
            except Exception as exc:          # noqa: BLE001 — the SLI degrades, alone
                why_unreadable = why_unreadable or f"{type(exc).__name__}: {exc}"
        outcome, why = outcome_of(row, slot)
        results.append({"slot": slot["slot"], "run_id": slot.get("run_id"),
                        "outcome": outcome, "why": why_unreadable or why,
                        "mode": (row or {}).get("pipeline_mode")})

    passes = sum(1 for r in results if r["outcome"] == "pass")
    fails = sum(1 for r in results if r["outcome"] == "fail")
    unknown = [r for r in results if r["outcome"] == "unknown"]
    decided = passes + fails
    rollup = driver.protocol_rollup(protocol_rows)
    return {"target": state["target"], "alpha": state["alpha"],
            "needed": runs_needed(state["target"], state["alpha"]),
            "dispatched": sum(1 for r in results if r["run_id"]),
            "passes": passes, "failures": fails, "unknown": len(unknown),
            "decided": decided,
            "run_rate": (passes / decided if decided else None),
            "run_interval": wilson(passes / decided, decided) if decided else None,
            "structured_call_rate": rollup.get("structured_call_rate"),
            "turns": rollup.get("turns", 0),
            "call_interval": (wilson(rollup["structured_call_rate"], rollup["turns"])
                              if rollup.get("turns") else None),
            "per_stage": {stage: {"turns": b["turns"],
                                  "structured_call_rate": b["structured_call_rate"],
                                  "interval": wilson(b["structured_call_rate"], b["turns"])
                                  if b["turns"] else None}
                          for stage, b in sorted(rollup.get("per_stage", {}).items())},
            "slots": results, "spent_usd": cost_usd(sum(1 for r in results if r["run_id"]))}


def _band(interval) -> str:
    return "unavailable" if not interval else "[%.3f, %.3f]" % interval


def report(summary: dict, out=print) -> int:
    """Print the scored probe and RETURN the exit code -- audit_drift.py's shape."""
    out("protocol probe: %d dispatched, %d passed, %d failed, %d unknown ($%.2f spent)"
        % (summary["dispatched"], summary["passes"], summary["failures"],
           summary["unknown"], summary["spent_usd"]))
    for r in summary["slots"]:
        out("  slot %-3d %-8s %s (%s)" % (r["slot"] + 1, r["outcome"],
                                          r["run_id"] or "-", r["why"]))
    out("\nrun level:   %s over %d decided run(s), Wilson 95%% %s"
        % ("%.3f" % summary["run_rate"] if summary["run_rate"] is not None else "n/a",
           summary["decided"], _band(summary["run_interval"])))
    if summary["turns"]:
        out("per stage:   %.4f of %d turn(s) ended in a structured call, Wilson 95%% %s"
            % (summary["structured_call_rate"], summary["turns"],
               _band(summary["call_interval"])))
        for stage, b in summary["per_stage"].items():
            out("  %-14s %.4f over %d turn(s) %s"
                % (stage, b["structured_call_rate"], b["turns"], _band(b["interval"])))
    else:
        # Zero rows is not a rate of zero. Before this PR's driver is DEPLOYED there are no
        # `protocol#` rows to read, and the per-stage number -- the one that is actually
        # actionable -- simply is not available yet. Saying so beats printing 0.0000.
        out("per stage:   no protocol# rows found. Either the deployed driver predates "
            "them (run tools/audit_drift.py) or these runs never took an agent turn.")

    if summary["failures"]:
        out("\n%d probe run(s) FAILED: the claim is refuted, not unproven."
            % summary["failures"])
        return 1
    if summary["unknown"]:
        out("\n%d slot(s) have no verdict. An unknown outcome is not a pass."
            % summary["unknown"])
        return 2
    if summary["passes"] < summary["needed"]:
        out("\n%d clean run(s) buys 95%% confidence only in a rate below the %.2f target; "
            "%d are needed." % (summary["passes"], summary["target"], summary["needed"]))
        return 2
    out("\n%d consecutive clean runs: the pass rate is at or above %.2f with %d%% "
        "confidence." % (summary["passes"], summary["target"],
                         round((1 - summary["alpha"]) * 100)))
    return 0


def cmd_plan(args, out=print) -> int:
    n = runs_needed(args.target, args.alpha)
    out("target pass rate >= %.2f at %d%% confidence (alpha=%g)"
        % (args.target, round((1 - args.alpha) * 100), args.alpha))
    out("  %d runs, $%.2f  (%d x $%.2f, measured 2026-08-15)"
        % (n, cost_usd(n), n, PROBE_UNIT_COST_USD))
    out("  derivation: %.2f ** n <= %g  =>  n >= log(%g)/log(%.2f) = %.2f  =>  ceil = %d"
        % (args.target, args.alpha, args.alpha, args.target,
           math.log(args.alpha) / math.log(args.target), n))
    out("  every one of the %d must reach `completed`; one failure refutes the claim and "
        "the remaining runs are not worth their money." % n)
    out("\nnothing was dispatched and nothing was spent: `plan` makes no AWS call.")
    return 0


def cmd_launch(args, out=print) -> int:
    path = args.state
    if os.path.exists(path) and not args.dry_run:
        state = load_state(path)
        out("resuming %s (created %s)" % (path, state.get("created_at")))
    else:
        state = new_state(args.n, args.target, args.alpha, args.spread_days,
                          args.artifact, args.student, _now())
        if not args.dry_run:
            save_state(path, state)
    clients = None if args.dry_run else {
        "lambda": _boto("lambda", args.region)}
    launch(state, None if args.dry_run else path, clients, _now(), args.dry_run, out=out)
    return 0


def cmd_collect(args, out=print) -> int:
    state = load_state(args.state)
    clients = {"ddb": _boto_resource("dynamodb", args.region)}
    summary = collect(state, clients)
    rc = report(summary, out=out)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({**summary, "exit_code": rc}, fh, indent=2)
    return rc


def _boto(service: str, region: str):
    import boto3
    return boto3.client(service, region_name=region)


def _boto_resource(service: str, region: str):
    import boto3
    return boto3.resource(service, region_name=region)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="derive the sample size and the price (offline)")
    p.add_argument("--target", type=float, default=0.80,
                   help="the pass rate to be able to claim (default 0.80)")
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)

    lp = sub.add_parser("launch", help="dispatch every due probe slot (SPENDS MONEY)")
    lp.add_argument("--n", type=int, default=None,
                    help="how many runs; default is runs_needed(--target)")
    lp.add_argument("--target", type=float, default=0.80)
    lp.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    lp.add_argument("--spread-days", type=float, default=7.0)
    lp.add_argument("--artifact", required=True, help="s3:// URI of the model.tar.gz")
    lp.add_argument("--student", default=None,
                    help="the base model id the artifact's adapters merge into. NO "
                         "DEFAULT: see MODE_REQUIRED_ROLES in start_pipeline")
    lp.add_argument("--state", default="probe.json", help="the dispatch ledger")
    lp.add_argument("--region", default="us-east-1")
    lp.add_argument("--dry-run", action="store_true",
                   help="print the payload and the schedule, make no AWS call")

    cp = sub.add_parser("collect", help="score the dispatched runs (read-only)")
    cp.add_argument("--state", default="probe.json")
    cp.add_argument("--region", default="us-east-1")
    cp.add_argument("--json", dest="json_out", default=None)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            return cmd_plan(args)
        if args.cmd == "launch":
            if args.n is None:
                args.n = runs_needed(args.target, args.alpha)
            return cmd_launch(args)
        return cmd_collect(args)
    except ValueError as exc:                 # a refused dispatch, or a nonsense target
        print(str(exc), file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print("%s. `launch` writes the ledger `collect` reads." % exc, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
