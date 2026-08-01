#!/usr/bin/env python3
"""05_harnesses.py — create/update every harness from agents/*/harness.json.

Thin orchestrator over the agentcore-harness-builder skill's create_harness.py /
update_harness.py conventions: reads each config, strips `_`-prefixed comment keys,
injects the execution role from SSM, creates the harness (or updates it if it
already exists), waits READY, and publishes ids to SSM /llmops/harness/<name>.

Also sets the observability env var the ops console requires on EVERY harness:
OTEL_TRACES_SAMPLER=always_on (without it, evaluations/insights sit at zero).

Usage:
  python deploy/05_harnesses.py --region us-east-1 --dry-run
  python deploy/05_harnesses.py --region us-east-1                       # all seven
  python deploy/05_harnesses.py --region us-east-1 --agent data-prep     # one
  python deploy/05_harnesses.py --region us-east-1 --agent data-prep --prod  # harness.prod.json
"""
import argparse
import json
import pathlib
import secrets
import sys
import time

import boto3

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


def load_config(agent, prod):
    fname = "harness.prod.json" if prod else "harness.json"
    path = REPO / "agents" / agent / fname
    if not path.exists():
        raise FileNotFoundError(f"{path} (run with/without --prod?)")
    return strip_comments(json.loads(path.read_text()))


def ensure_env(cfg):
    env = cfg.setdefault("environmentVariables", {})
    env.setdefault("OTEL_TRACES_SAMPLER", "always_on")
    return cfg


def existing_harness(ctl, name):
    """harnessId is `<name>-<10char suffix>`, so match on the name prefix/field."""
    for h in ctl.list_harnesses().get("harnesses", []):
        if h.get("name") == name or h.get("harnessName") == name \
                or h.get("harnessId", "").rsplit("-", 1)[0] == name:
            return h
    return None


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
        update = {k: v for k, v in cfg.items()
                  if k in ("model", "systemPrompt", "tools", "skills", "allowedTools",
                           "maxIterations", "maxTokens", "timeoutSeconds", "truncation",
                           "environment", "environmentVariables")}
        ctl.update_harness(harnessId=harness_id, clientToken=secrets.token_hex(20), **update)
        action = "updated"
    else:
        resp = ctl.create_harness(harnessName=name, executionRoleArn=role_arn,
                                  clientToken=secrets.token_hex(20), **cfg)
        harness_id = resp["harness"]["harnessId"]
        action = "created"

    h = wait_ready(ctl, harness_id)
    if tags:
        ctl.tag_resource(resourceArn=h["arn"], tags=tags)
    out = {"harness": name, "harness_id": harness_id, "action": action,
           "status": h["status"]}
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
    if not args.dry_run:
        role_arn = ssm.get_parameter(Name="/llmops/iam/harness_execution_arn")["Parameter"]["Value"]

    results = []
    for agent in agents:
        cfg = load_config(agent, args.prod)
        res = create_or_update(ctl, cfg, role_arn, args.dry_run, dat)
        results.append(res)
        if not args.dry_run:
            ssm.put_parameter(Name=f"/llmops/harness/{agent}", Value=res["harness_id"],
                              Type="String", Overwrite=True)

    print(json.dumps({"results": results, "prod": args.prod, "dry_run": args.dry_run}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
