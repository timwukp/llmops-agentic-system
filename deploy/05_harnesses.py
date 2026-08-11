#!/usr/bin/env python3
"""05_harnesses.py — create/update every harness from agents/*/harness.json.

Thin orchestrator over the agentcore-harness-builder skill's create_harness.py /
update_harness.py conventions: reads each config, strips `_`-prefixed comment keys,
resolves <ACCOUNT_ID>/<REGION>/<DATA_BUCKET> placeholders, injects the execution role
from SSM, creates the harness (or updates it if it already exists), waits READY, and
publishes ids to SSM /llmops/harness/<name>.

The placeholders exist because an s3 skill source is a single URI that embeds the bucket
name, and this account's bucket name embeds the account id -- which may not appear in a
file of this public repo. See deploy/config_subst.py for why an unresolved token is a
hard error and not a warning.

Also sets the observability env var the ops console requires on EVERY harness:
OTEL_TRACES_SAMPLER=always_on (without it, evaluations/insights sit at zero).

Usage:
  python deploy/05_harnesses.py --region us-east-1 --dry-run
  python deploy/05_harnesses.py --region us-east-1                       # all seven
  python deploy/05_harnesses.py --region us-east-1 --agent data-prep     # one
  python deploy/05_harnesses.py --region us-east-1 --agent data-prep --prod  # harness.prod.json
"""
import argparse
import datetime
import json
import pathlib
import secrets
import sys
import time

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import config_subst  # noqa: E402 — deploy/ is not a package; path is set just above

REPO = pathlib.Path(__file__).resolve().parent.parent
AGENTS = ["data-prep", "finetune", "eval", "deploy", "monitor", "orchestrator",
          # The auditor. Listed here and not only in agents/ because this list is
          # what --agent validates against and what a bare run creates: a config on
          # disk that no script names is a harness that silently never exists.
          "finops"]


def strip_comments(obj):
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [strip_comments(v) for v in obj]
    return obj


def load_config(agent, prod, mapping=None):
    """Read a harness config, strip comment keys, and resolve deploy-time placeholders.

    `mapping` is None only for callers that genuinely have no account/region to resolve
    with; passing None leaves tokens in place, which the resolve step would otherwise
    reject. Every real deploy path passes one -- including --dry-run, which derives it
    from --account-id so the dry run exercises the same substitution the real run does.
    A dry run that skipped substitution would report a config nobody will ever send.
    """
    fname = "harness.prod.json" if prod else "harness.json"
    path = REPO / "agents" / agent / fname
    if not path.exists():
        raise FileNotFoundError(f"{path} (run with/without --prod?)")
    cfg = strip_comments(json.loads(path.read_text()))
    if mapping is None:
        return cfg
    return config_subst.resolve(cfg, mapping, where=str(path.relative_to(REPO)))


def ensure_env(cfg):
    env = cfg.setdefault("environmentVariables", {})
    env.setdefault("OTEL_TRACES_SAMPLER", "always_on")
    return cfg


#: Where the console reads the "spans exist from here on" cutoff.
SPANS_SINCE_PARAM = "/llmops/observability/spans_since"


def ensure_spans_since(ssm, now=None):
    """Record when always_on tracing first became true here, once, and never again.

    The console filters batch-eval and insights to runs created after this timestamp,
    because a session with no spans cannot be scored and scoring it produces a "failed
    session" that is really a missing measurement. Its value was a literal in TWO places
    (`deploy/console/deploy.sh` and `lambda_function.py`), naming the hour tracing was
    switched on in THIS account -- correct here, wrong for every other deployment, and
    wrong in the direction that hides the problem: a fresh deployment's runs are all
    NEWER than 2026-07-28, so they pass the filter, get scored, and come back as failed
    sessions with no hint that the cutoff is the reason.

    So it is written by the step that CAUSES the fact -- ensure_env sets
    OTEL_TRACES_SAMPLER=always_on right here -- rather than transcribed by a human into a
    deploy script. `Overwrite=False`: the first deploy is when spans start, and a later
    redeploy must not move the cutoff forward over runs that do have spans. An existing
    value is the answer, not a conflict, so ParameterAlreadyExists is success.
    """
    stamp = now or datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    try:
        ssm.put_parameter(Name=SPANS_SINCE_PARAM, Value=stamp, Type="String",
                          Description="UTC time OTEL_TRACES_SAMPLER=always_on was first "
                                      "deployed; the console scores no run older",
                          Overwrite=False)
        print(f"  ssm {SPANS_SINCE_PARAM} = {stamp} (first always_on deploy)")
        return stamp
    except Exception as exc:  # noqa: BLE001 — already set is the normal case, not an error
        if "ParameterAlreadyExists" not in str(type(exc).__name__) + str(exc):
            raise
        got = ssm.get_parameter(Name=SPANS_SINCE_PARAM)["Parameter"]["Value"]
        print(f"  ssm {SPANS_SINCE_PARAM} = {got} (already set; left alone)")
        return got


def existing_harness(ctl, name):
    """harnessId is `<name>-<10char suffix>`, so match on the name prefix/field."""
    for h in ctl.list_harnesses().get("harnesses", []):
        if h.get("name") == name or h.get("harnessName") == name \
                or h.get("harnessId", "").rsplit("-", 1)[0] == name:
            return h
    return None


#: The fields create_or_update actually sends on an update. Named once, here, because the
#: read-back must check exactly what was sent -- a list that drifts from the update call
#: would confirm fields nobody deployed and stay silent about one that was.
UPDATED_FIELDS = ("model", "systemPrompt", "tools", "skills", "allowedTools",
                  "maxIterations", "maxTokens", "timeoutSeconds", "truncation",
                  "environment", "environmentVariables")


def harness_config_drift(sent: dict, live: dict, fields=UPDATED_FIELDS) -> list:
    """Which of the fields we just sent did NOT land, field by field.

    CONTAINMENT, not equality, and that is a measurement rather than a preference. On a
    harness that is perfectly in sync, `environment` still differs: we send
    networkConfiguration + lifecycleConfiguration, and the service returns those plus
    agentRuntimeArn/agentRuntimeName/agentRuntimeId that it assigned. Strict equality would
    report drift on every single deploy forever, and a check that cries wolf on a correct
    deploy is a check somebody deletes. So the question asked is the one that matters: is
    every value we sent present and equal live? Keys the service added are its business.

    Only `fields` are checked, because only those are sent. Reporting on a field this script
    never deploys would blame this deploy for something another owns -- `memory` is
    04_wire_memory.py's, and naming it here would make every run of this script look broken.
    """
    drift = []
    for f in fields:
        if f not in sent:
            continue
        want, got = sent[f], live.get(f)
        if want == got:
            continue
        if isinstance(want, dict) and isinstance(got, dict):
            inner = _dict_drift(want, got, f)
            if inner:
                drift.extend(inner)
                continue
            # Every key we sent matched; the difference is keys the service added.
            continue
        drift.append({"field": f, "problem": _describe(want, got)})
    return drift


def _dict_drift(want: dict, got: dict, path: str) -> list:
    """Recursive containment check, reporting the dotted path of each key that disagrees."""
    out = []
    for k, wv in want.items():
        if k not in got:
            out.append({"field": f"{path}.{k}", "problem": "sent, but ABSENT live"})
        elif isinstance(wv, dict) and isinstance(got[k], dict):
            out.extend(_dict_drift(wv, got[k], f"{path}.{k}"))
        elif wv != got[k]:
            out.append({"field": f"{path}.{k}", "problem": _describe(wv, got[k])})
    return out


def _describe(want, got) -> str:
    """Say what differs without pasting a 6.5 KB prompt into a terminal.

    The finops prompt is 6539 characters. Dumping both sides is how a deploy log becomes
    unreadable and the one line that mattered scrolls away, so long values are reported by
    length and by the first place they diverge -- enough to tell "the new prompt did not
    land" apart from "a different prompt landed".
    """
    ws, gs = json.dumps(want, default=str), json.dumps(got, default=str)
    if len(ws) > 160 or len(gs) > 160:
        at = next((i for i in range(min(len(ws), len(gs))) if ws[i] != gs[i]),
                  min(len(ws), len(gs)))
        return (f"sent {len(ws)} chars, live {len(gs)} chars, first differ at {at}: "
                f"sent {ws[at:at + 60]!r} vs live {gs[at:at + 60]!r}")
    return f"sent {ws} != live {gs}"


def confirm_harness_landed(ctl, harness_id: str, sent: dict, attempts: int = 4,
                           sleep=time.sleep) -> dict:
    """Read the config back, and refuse to call the deploy done until it matches.

    `update_harness` returning 200 says the call was accepted. `wait_ready` then says the
    harness is READY. Neither says the live config is the one in this tree, and READY is the
    more dangerous of the two, because a harness serving a stale prompt is READY the whole
    time -- which is exactly what happened: on 2026-08-03 the live llmops_finops prompt still
    quoted the orphan endpoint at `~$18/day` while main had said `$36.36/day` since #41
    merged. Every surface reported healthy: status READY, version 5, `list_harnesses` clean.
    The falsified number was found by a human dumping the live prompt and grepping it.

    This is the same defect deploy/07_lambdas.py had for the state machine definition (#80),
    and the same one `update_function_configuration` had for `Role` before that: three
    resources, one belief, that a call which returned is a change that landed.

    Polls, because a version publish is not instantaneous and a guard that cries wolf gets
    deleted -- the same reasoning as the ASL read-back, and the same reason `warm()` below
    needs two consecutive fast turns instead of one.
    """
    last = [{"problem": "never read"}]
    for i in range(attempts):
        try:
            live = ctl.get_harness(harnessId=harness_id)["harness"]
        except Exception as exc:  # noqa: BLE001 — cannot confirm; must not claim confirmed
            return {"config_confirmed": False,
                    "read_back_unreachable": f"{type(exc).__name__}: {exc}"}
        last = harness_config_drift(sent, live)
        if not last:
            return {"config_confirmed": True,
                    "harness_version": live.get("harnessVersion")}
        if i < attempts - 1:
            sleep(2 ** i)  # 1,2,4s — for a version publish settling, not a cure for drift
    raise SystemExit(
        f"{harness_id}: update_harness succeeded and the harness is READY, but the LIVE "
        f"config still disagrees with this tree after {attempts} reads — "
        f"{json.dumps(last, indent=2)}\n"
        "Every session runs the live config, not the one in this tree. Do not record this "
        "deploy as done: READY is not 'serving what you sent', and a stale prompt is READY "
        "the entire time it is wrong.")


def wait_ready(ctl, harness_id, timeout=300):
    for _ in range(timeout // 5):
        h = ctl.get_harness(harnessId=harness_id)["harness"]
        if h["status"] == "READY":
            return h
        if h["status"] in ("FAILED", "DELETING"):
            raise RuntimeError(f"{harness_id}: {h['status']} — {h.get('failureReason')}")
        time.sleep(5)
    raise TimeoutError(f"{harness_id} not READY after {timeout}s")


# A turn faster than this came off a warm harness. Measured on a scratch harness with a
# trivial prompt: cold turns land at 38-46s and warm ones at 3.5-6s, so anything in
# between is a gap with no observations in it.
WARM_FAST_S = 15.0
# Consecutive fast turns required before declaring the harness warm. TWO, because ONE is
# ambiguous and that ambiguity refuted the first version of this fix: five trials in a row
# saw a fast FIRST turn (4.45/3.92/3.77/3.83/4.13s) and the next real turn was still cold
# (36.98/37.02/38.29/39.0/46.51s). A lone fast turn cannot tell "already warm" apart from
# "the previous version is still answering while routing moves to the new one".
WARM_CONSEC_FAST = 2
# Cap on throwaway turns. A cold harness needs ~4 (two cold, then two fast); the cap exists
# so a harness that never gets fast (a broken skill, a model outage) costs a bounded amount
# instead of looping through a deploy budget.
WARM_MAX_TURNS = 6


def warm(dat, arn, harness_id):
    """Spend the cold-start turns HERE so a customer does not spend them.

    MEASURED on a scratch harness, 8 consecutive turns, fresh session id each, no config
    change between them -- time to first token:

        44.83  3.86  3.89  4.27  4.10  4.27  3.97  4.60

    Then a NO-OP UpdateHarness (identical model, only a new version) and it came straight
    back: 38.94, 34.89, then 4.39, 4.19. Reusing one session id changed nothing
    (4.09/4.31/3.48/3.98), so this is not per-session state -- publishing a version is
    what costs it. READY does NOT mean warm, and this script used to believe it did:
    every deploy handed the next speaker a ~40s turn, and the next speaker is a customer.

    It takes TWO turns, not one. The first attempt at this fix sent a single throwaway
    turn, paid 37.59s, and the next real turn STILL took 44.96s. Timing turns until they
    went fast showed the actual shape, twice, on two independent versions:

        version 19:  44.03  45.62   5.22  5.55
        version 20:  45.27  43.36   5.89  4.38

    The turns must also be SEQUENTIAL. Firing the two concurrently looked attractive
    (46s wall instead of 84s) but did not work: both raced the same uninitialized slot,
    and the turn after them was cold too -- 46.1 / 38.06 concurrent, then 45.99, then
    5.9. So 3 cold turns instead of 2.

    Rather than hardcode a count, loop until turns come back under WARM_FAST_S, capped at
    WARM_MAX_TURNS: the count is what the platform does today, not a contract it owes us.

    But stopping at ONE fast turn is WRONG, and shipping that was the second wrong version
    of this fix. Paired trials -- publish, warm, then time a real turn -- refuted it five
    times out of five:

        warm_turns [4.45] -> real turn 36.98s      warm_turns [3.83] -> 39.00s
        warm_turns [3.92] -> real turn 37.02s      warm_turns [4.13] -> 46.51s
        warm_turns [3.77] -> real turn 38.29s

    Every one reported warmed=True after a single ~4s turn and then handed the customer a
    ~40s turn anyway. The fast first turn was the OLD version still answering while routing
    moved across; one sample cannot tell that apart from a genuinely warm harness. Requiring
    WARM_CONSEC_FAST consecutive fast turns separates them, and it holds where the earlier
    rule broke:

        K=1: [37.46 37.27 4.06]        -> customer turn 3.97s
        K=2: [45.38 37.11 5.68 3.95]   -> customer turn 7.91s

    Failure is deliberately non-fatal. The harness is live either way; reporting a
    successful deploy as failed because a throwaway turn timed out would send someone
    hunting a deploy bug that does not exist.

    A fast turn only counts if TEXT actually arrived. One cold turn in five came back with
    an empty stream and no error at all (headers, then nothing) -- and an empty stream
    returns immediately, so timing alone would have scored that turn as "warm" and stopped
    the loop having warmed nothing. Elapsed seconds cannot tell a warm reply from a
    reply that never happened.
    """
    t0 = time.time()
    turns = []
    note = None
    consec = 0
    for i in range(WARM_MAX_TURNS):
        t1 = time.time()
        chars = 0
        try:
            r = dat.invoke_harness(
                harnessArn=arn,
                # runtimeSessionId has a 33-char minimum, hence the padding. A fresh id
                # per turn because session reuse buys no warmth (measured above) and a
                # reused one would leave conversation state behind on the harness.
                runtimeSessionId=f"deploy-warm-{i}-{harness_id}-0000000000000000"[:64],
                messages=[{"role": "user", "content": [{"text": "Reply with: ok"}]}])
            for ev in r.get("stream", []):
                chars += len(((ev.get("contentBlockDelta") or {}).get("delta")
                              or {}).get("text", ""))
        except Exception as e:
            note = f"{type(e).__name__}: {str(e)[:160]}"
            turns.append(round(time.time() - t1, 2))
            break
        turns.append(round(time.time() - t1, 2))
        if chars == 0:
            # A text-less turn is not evidence of anything, in either direction: it
            # returns instantly (so it looks fast) but nothing was served (so it warmed
            # nothing). Break the streak rather than count it.
            note = f"turn {i} streamed no text"
            consec = 0
            continue
        consec = consec + 1 if turns[-1] < WARM_FAST_S else 0
        if consec >= WARM_CONSEC_FAST:
            note = None
            break
    return {"warmed": consec >= WARM_CONSEC_FAST and note is None,
            "warm_turns": turns, "seconds": round(time.time() - t0, 2),
            **({"note": note} if note else {})}


def create_or_update(ctl, cfg, role_arn, dry, dat=None):
    name = cfg.pop("harnessName")
    tags = cfg.pop("tags", None)
    cfg = ensure_env(cfg)
    exists = None if dry else existing_harness(ctl, name)

    if dry:
        return {"harness": name, "action": "would create/update",
                "model": cfg["model"]["bedrockModelConfig"]["modelId"],
                "skills": len(cfg.get("skills", [])), "tools": len(cfg.get("tools", []))}

    if exists:
        # UpdateHarness: memory/environmentArtifact/authorizerConfiguration wrap in
        # optionalValue; everything else passes directly. We never send memory here
        # (04_wire_memory.py owns it).
        harness_id = exists["harnessId"]
        wait_ready(ctl, harness_id)  # can't update while CREATING/UPDATING
        sent = {k: v for k, v in cfg.items() if k in UPDATED_FIELDS}
        ctl.update_harness(harnessId=harness_id, clientToken=secrets.token_hex(20), **sent)
        action = "updated"
    else:
        resp = ctl.create_harness(harnessName=name, executionRoleArn=role_arn,
                                  clientToken=secrets.token_hex(20), **cfg)
        harness_id = resp["harness"]["harnessId"]
        sent = cfg
        action = "created"

    h = wait_ready(ctl, harness_id)
    if tags:
        ctl.tag_resource(resourceArn=h["arn"], tags=tags)
    out = {"harness": name, "harness_id": harness_id, "action": action,
           "status": h["status"]}
    # READY says the harness answers; it does not say what it answers WITH. Confirmed before
    # warming, because warming a harness that is serving a stale prompt spends real turns
    # making the wrong config fast to reach.
    out.update(confirm_harness_landed(ctl, harness_id, sent))
    # READY is not warm. See warm() for the measurement; without this the first real
    # turn after any deploy pays ~35s, and the person who pays it is a customer.
    if dat is not None:
        out.update(warm(dat, h["arn"], harness_id))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--agent", action="append", choices=AGENTS)
    ap.add_argument("--prod", action="store_true", help="use harness.prod.json variants")
    ap.add_argument("--account-id", help="for offline --dry-run placeholder resolution")
    ap.add_argument("--bucket", help="override the data bucket in <DATA_BUCKET>")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    agents = args.agent or AGENTS

    ctl = boto3.client("bedrock-agentcore-control", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)
    # The DATA plane, for the post-deploy warm-up turn. Long read timeout because that
    # first turn is the slow one by definition.
    dat = None if args.dry_run else boto3.client(
        "bedrock-agentcore", region_name=args.region,
        config=boto3.session.Config(read_timeout=300, retries={"max_attempts": 1}))

    role_arn = None
    account_id, bucket = args.account_id, args.bucket
    if not args.dry_run:
        role_arn = ssm.get_parameter(Name="/llmops/iam/harness_execution_arn")["Parameter"]["Value"]
        account_id = account_id or boto3.client(
            "sts", region_name=args.region).get_caller_identity()["Account"]
        # Prefer the bucket 03_storage.py actually PUBLISHED over one derived from the
        # account id. The derived name is right for this account and would be wrong for a
        # deploy that passed 01_iam.py --bucket; a skill URI pointing at a bucket that
        # does not exist fails at session start, not here.
        if not bucket:
            try:
                bucket = ssm.get_parameter(
                    Name="/llmops/storage/bucket")["Parameter"]["Value"]
            except ssm.exceptions.ParameterNotFound:
                pass  # falls back to the derived default in mapping_for
    mapping = None
    if account_id:
        mapping = config_subst.mapping_for(account_id, args.region, bucket)
    elif any(config_subst.unresolved(load_config(a, args.prod)) for a in agents):
        raise SystemExit(
            "a config carries placeholders but no --account-id was given for this "
            "dry run; pass --account-id to resolve them offline")

    results = []
    for agent in agents:
        cfg = load_config(agent, args.prod, mapping)
        res = create_or_update(ctl, cfg, role_arn, args.dry_run, dat)
        results.append(res)
        if not args.dry_run:
            ssm.put_parameter(Name=f"/llmops/harness/{agent}", Value=res["harness_id"],
                              Type="String", Overwrite=True)

    if not args.dry_run:
        ensure_spans_since(ssm)

    print(json.dumps({"results": results, "prod": args.prod, "dry_run": args.dry_run}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
