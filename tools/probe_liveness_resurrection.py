#!/usr/bin/env python3
"""Live probe: the non-run (triage) liveness loop of #37, end to end, against the
DEPLOYED bundles and the REAL stage-events table.

Why this exists as a script rather than a unit test. The non-run half of the
resurrector is the one path whose healthy state is indistinguishable from a broken
one: a triage dies maybe once a month, so `checked_liveness: 0` is the normal reading
on almost every day, and a Query against the wrong partition, a missing
`dynamodb:Query` grant, or two bundles that disagree about the string `__liveness__`
would all produce exactly that reading. The unit tests pin the logic; nothing pins
that the two SEPARATELY BUNDLED copies of this contract still agree once deployed.
So this probe downloads what Lambda is actually serving and drives it.

What it does NOT do, deliberately:
  * no AgentCore turn — the fake agentcore client raises before the first invoke, so
    the driver's real heartbeat runs and the billed part never starts;
  * no real `lambda:invoke` and no real `events:PutEvents` — both are captured;
  * no writes to the runs table — the resurrector's run-row half is handed a stub that
    returns zero items and raises on any write, because forcing STALE_MINUTES to 0
    (needed to make the probe's own fresh beat look stale) would otherwise claim and
    revive every live run in the account.

The one real mutation is a single item in the `__liveness__` partition of
EVENTS_TABLE, under a synthetic subject, deleted before exit. It is safe to leave
behind if this script is interrupted: the 15-minute sweep only revives a beat older
than STALE_MINUTES (20), and every step here rewrites `beat_at` to now.

Usage:
  python tools/probe_liveness_resurrection.py --region us-east-1
  python tools/probe_liveness_resurrection.py --region us-east-1 --from-repo

Exit code is 0 only if every check passes. `--from-repo` reads this repo's copies
instead of the deployed ones, which answers a different question: whether the code you
are about to ship holds the contract, not whether the code in production does.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from urllib.request import urlopen

import boto3

REPO = pathlib.Path(__file__).resolve().parent.parent
DRIVER_FN = "llmops-harness-driver"
RESURRECTOR_FN = "llmops-resurrector"
#: The subject is synthetic and self-describing: if it ever shows up in an escalation,
#: a human should read it as "a probe leaked", not as a run to go look for.
ABOUT = "run-PROBE-liveness-check"


def _load(name: str, path: pathlib.Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fetch_bundle(region: str, fn: str, dest: pathlib.Path) -> pathlib.Path:
    """Unzip what Lambda is serving right now. The presigned URL is never printed."""
    lam = boto3.client("lambda", region_name=region)
    url = lam.get_function(FunctionName=fn)["Code"]["Location"]
    with urlopen(url) as resp:  # noqa: S310 — URL comes from the Lambda API
        blob = resp.read()
    out = dest / fn
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(out)
    return out / "handler.py"


class _Boom:
    def invoke_harness(self, **kw):
        raise RuntimeError("PROBE-STOP: before any agent turn")


class _Recorder:
    """Absorbs every other client call, so only the beat reaches AWS for real."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def f(**kw):
            self.calls.append((name, kw))
        return f


class _FakeEvents:
    def __init__(self):
        self.entries = []

    def put_events(self, Entries):  # noqa: N803 — boto3's own casing
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


class _FakeLambda:
    def __init__(self):
        self.invokes = []

    def invoke(self, **kw):
        self.invokes.append(kw)
        return {"StatusCode": 202}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--from-repo", action="store_true",
                    help="probe this checkout instead of the deployed bundles")
    args = ap.parse_args()

    lam = boto3.client("lambda", region_name=args.region)
    env = lam.get_function_configuration(
        FunctionName=RESURRECTOR_FN)["Environment"]["Variables"]
    runs_table, events_table = env["RUNS_TABLE"], env["EVENTS_TABLE"]

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="liveness-probe-"))
    try:
        if args.from_repo:
            drv_path = REPO / "orchestration/harness_driver/handler.py"
            res_path = REPO / "orchestration/resurrector/handler.py"
            source = "this checkout"
        else:
            drv_path = _fetch_bundle(args.region, DRIVER_FN, tmp)
            res_path = _fetch_bundle(args.region, RESURRECTOR_FN, tmp)
            source = "the deployed bundles"
        print(f"probing {source}; EVENTS_TABLE={events_table}")

        os.environ.update({
            "AWS_REGION": args.region, "RUNS_TABLE": runs_table,
            "EVENTS_TABLE": events_table, "EVENT_BUS": env.get("EVENT_BUS", "llmops-events"),
            "DRIVER_FN": env.get("DRIVER_FN", DRIVER_FN),
            "DATA_BUCKET": "probe-bucket-not-read",
            "LLMOPS_SNS_TOPIC": "probe", "START_FN": "probe",
            "ACTUALS_TABLE": env.get("ACTUALS_TABLE", "probe"),
        })
        drv = _load("probe_driver", drv_path)
        res = _load("probe_resurrector", res_path)
        return _run(args, drv, res, runs_table, events_table)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(args, drv, res, runs_table, events_table) -> int:  # noqa: C901 — a checklist
    ddb = boto3.resource("dynamodb", region_name=args.region)
    real_events = ddb.Table(events_table)
    subject = "triage-" + ABOUT
    key = {"run_id": drv.LIVENESS_PK, "sk": "beat#" + subject}

    def item():
        return real_events.get_item(Key=key).get("Item")

    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail else ""))

    check("the two separately-bundled copies agree on the partition name",
          drv.LIVENESS_PK == res.LIVENESS_PK == "__liveness__",
          f"{drv.LIVENESS_PK!r} vs {res.LIVENESS_PK!r}")
    # A leftover from an interrupted run is cleared rather than asserted away: the check
    # that matters is CLEANUP at the end, and a stale item must not be able to make the
    # fresh-beat assertion below pass for the wrong reason.
    if item() is not None:
        real_events.delete_item(Key=key)
        print("NOTE cleared a leftover probe item from an earlier run")
    check("the partition holds no probe item before the probe", item() is None)

    def _clear_ghost_row():
        """A runs row for the synthetic subject can only be debris, and it INVERTS the
        probe: with the row present the conditional beat succeeds, so the handoff into
        __liveness__ never runs and every check below fails for the wrong reason."""
        if "Item" in ddb.Table(runs_table).get_item(Key={"run_id": subject}):
            ddb.Table(runs_table).delete_item(Key={"run_id": subject})
            print("NOTE cleared a leftover probe row from the runs table")

    _clear_ghost_row()

    # 1. the beat -------------------------------------------------------------------
    # Built by the deployed function every real emitter's payload goes through, so the
    # probe cannot pass on an event shape production never sends: a hand-written event
    # omitting manifest_uri died in _run_stage before the heartbeat and would have
    # reported "the beat never wrote" for entirely the wrong reason.
    event = drv.triage_event_from_bus(
        {"detail": {"run_id": ABOUT, "stage": "finetune", "reason": "liveness probe",
                    "iteration": 0, "emitted_at": "1970-01-01T00:00:00+00:00"}},
        os.environ["DATA_BUCKET"])
    check("triage_event_from_bus mints the subject the resurrector keys on",
          event["run_id"] == subject, event["run_id"])
    clients = {"ddb": ddb, "agentcore": _Boom(), "sfn": _Recorder(),
               "events": _FakeEvents(), "lambda": _FakeLambda(),
               "s3": _Recorder(), "sns": _Recorder(), "kms": _Recorder()}
    try:
        drv.handler(event, clients=clients)
        check("the driver stopped before the agent turn (no billed turn)", False,
              "no exception raised")
    except Exception as exc:  # noqa: BLE001 — the stop IS the assertion
        check("the driver stopped before the agent turn (no billed turn)",
              "PROBE-STOP" in str(exc), f"{type(exc).__name__}")

    # A ghost row is not just a failed check, it is the exact hazard the design forbids
    # (a runs row carrying driver_beat_at is what the resurrector sweeps for), so it is
    # DELETED as well as reported. Measured need: a mutation run that stripped the
    # ConditionExpression left such a row behind, and on the next probe its mere
    # existence made the conditional beat SUCCEED -- so _beat_liveness never ran and the
    # probe reported "the beat never wrote" for entirely the wrong reason. Hence also
    # _clear_ghost_row() before the beat, above.
    ghost = ddb.Table(runs_table).get_item(Key={"run_id": subject}).get("Item")
    check("no ghost run row was minted in the runs table", ghost is None,
          "" if ghost is None else f"driver_beat_at={ghost.get('driver_beat_at')}")
    if ghost is not None:
        ddb.Table(runs_table).delete_item(Key={"run_id": subject})
        print("NOTE deleted the ghost run row this probe must never produce")

    beat = item()
    check("the REJECTED runs-table beat wrote a __liveness__ item instead",
          beat is not None)
    if beat is None:
        return _verdict(results)
    payload = json.loads(str(beat.get("payload") or "{}"))
    check("the stamped payload carries params.escalation "
          "(a revival without it triages blind)",
          payload.get("params", {}).get("escalation", {}).get("run_id") == ABOUT,
          json.dumps(payload.get("params", {}))[:80])

    # 2. the sweep ------------------------------------------------------------------
    class _RunsStub:
        def scan(self, **kw):
            return {"Items": []}

        def update_item(self, **kw):
            raise AssertionError("the run-row half must not write during this probe")

    class _HybridDDB:
        def Table(self, name):  # noqa: N802 — mirrors boto3
            return _RunsStub() if name == runs_table else real_events

    def sweep(stale_minutes):
        os.environ["STALE_MINUTES"] = str(stale_minutes)
        fake_lambda, fake_events = _FakeLambda(), _FakeEvents()
        out = res.handler({}, clients={"ddb": _HybridDDB(), "lambda": fake_lambda,
                                       "events": fake_events})
        return out, fake_lambda, fake_events

    out, fake_lambda, _ = sweep(20)
    check("a FRESH beat is not resurrected (age < STALE_MINUTES)",
          not fake_lambda.invokes, json.dumps(out)[:100])
    check("the sweep reports how many liveness beats it read",
          out.get("checked_liveness", 0) >= 1, json.dumps(out)[:120])

    out, fake_lambda, fake_events = sweep(0)
    revived = [json.loads(i["Payload"].decode()) for i in fake_lambda.invokes]
    check("a STALE beat is resurrected with the stamped payload",
          len(revived) == 1
          and revived[0].get("params", {}).get("escalation", {}).get("run_id") == ABOUT,
          str([r.get("run_id") for r in revived]))
    check("the revival is announced as DriverResurrected",
          any(e["DetailType"] == res.ev.DRIVER_RESURRECTED for e in fake_events.entries),
          str([e["DetailType"] for e in fake_events.entries]))
    check("the claim incremented resurrections on the real item",
          int((item() or {}).get("resurrections") or 0) == 1,
          str((item() or {}).get("resurrections")))

    # 3. cap exhaustion -------------------------------------------------------------
    real_events.update_item(Key=key, UpdateExpression="SET resurrections = :n",
                            ExpressionAttributeValues={
                                ":n": res.RESURRECTIONS_MAX_DEFAULT})
    out, fake_lambda, fake_events = sweep(0)
    escalations = [json.loads(e["Detail"]) for e in fake_events.entries
                   if e["DetailType"] == res.ev.ESCALATED_TO_HUMAN]
    check("a cap-exhausted triage escalates against the run it was ABOUT, not itself",
          len(escalations) == 1 and escalations[0]["run_id"] == ABOUT
          and escalations[0].get("dead_triage") == subject,
          json.dumps([e.get("run_id") for e in escalations]))
    check("no driver invoke on the cap path", not fake_lambda.invokes)
    check("the cap-exhausted item is DELETED "
          "(or every 15-minute sweep re-escalates it forever)", item() is None)

    # 4. the terminal delete --------------------------------------------------------
    def rebeat():
        real_events.update_item(
            Key=key, UpdateExpression="SET beat_at = :t, payload = :p",
            ExpressionAttributeValues={":t": datetime.now(timezone.utc).isoformat(),
                                       ":p": json.dumps({"params": event["params"]})})

    rebeat()
    drv._settle_liveness({"ddb": ddb}, event, {"status": "escalated"})
    check("_settle_liveness deletes the item on a terminal return", item() is None)
    rebeat()
    drv._settle_liveness({"ddb": ddb}, event,
                         {"status": "self_reinvoked_between_turns"})
    check("a between-turns handoff does NOT delete it (that is the live case)",
          item() is not None)

    real_events.delete_item(Key=key)
    check("CLEANUP: the partition is empty again", item() is None)
    return _verdict(results)


def _verdict(results) -> int:
    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
