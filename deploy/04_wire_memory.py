#!/usr/bin/env python3
"""04_wire_memory.py — create the SHARED BYO AgentCore Memory and wire it to harnesses.

One Memory (`llmops-shared-memory`, SEMANTIC + EPISODIC) is shared by EVERY harness
this repo defines — that is the mechanism by which run-N learnings ("lr 2e-4
overfit on this dataset", "grid prompts need explicit size hints") reach run-N+1
across every stage.

Wraps the agentcore-harness-builder skill's wire_memory.py three-step wiring
(CreateMemory → UpdateHarness(optionalValue) → IAM inline grant) but:
  - creates the memory ONCE and attaches it to EVERY harness passed via --harness
  - uses only SEMANTIC + EPISODIC strategies (facts + what-happened; we skip
    USER_PREFERENCE/SUMMARIZATION — there is no human user in the loop)
  - actorId per-agent partitions the namespaces by agent, and an actorId already
    live is NEVER changed by a redeploy (see `resolve_actor_id`)
  - publishes the memory id/arn to SSM /llmops/memory/*

Usage:
  python deploy/04_wire_memory.py --region us-east-1 --dry-run
  python deploy/04_wire_memory.py --region us-east-1                       # every harness
  python deploy/04_wire_memory.py --region us-east-1 --harness llmops_data_prep
  python deploy/04_wire_memory.py --region us-east-1 --harness llmops_finops \\
      --repartition llmops_finops     # abandons that harness's memory; prints the count
"""
import argparse
import json
import pathlib
import secrets
import sys
import time

import boto3

REPO = pathlib.Path(__file__).resolve().parent.parent
MEMORY_NAME = "llmops_shared_memory"


def harness_names():
    """Every harness this repo defines, read from the configs that name them.

    This was a hand-written list of the five pipeline workers, and the two harnesses it
    omitted -- llmops_finops and llmops_orchestrator -- were therefore the two the
    retrieval-threshold fix never reached: measured live 2026-08-13, both still sat at
    the pre-fix semantic setting (topK 10 / relevanceScore 0.2) while all five listed
    ones carried 5 / 0.6. Both prompts are BUILT on memory ("estimate accuracy improves
    only if each reconciliation's finding survives into the next estimate"; "your memory
    is shared with the specialists"), so the omission was invisible: nothing failed, the
    channel just stayed as loose as it had been when it injected another run's
    post-mortem as a bare fact.

    05_harnesses.py already carries a comment naming this exact failure mode ("a config
    on disk that no script names is a harness that silently never exists") -- the lesson
    was written in one script and not applied in its sibling. So the list is derived from
    the same producer 05 deploys from, and an eighth agent config is wired by existing.
    """
    names = []
    for cfg in sorted((REPO / "agents").glob("*/harness.json")):
        name = json.loads(cfg.read_text()).get("harnessName")
        if not name:
            raise SystemExit(f"{cfg} has no harnessName -- cannot wire memory blind")
        names.append(name)
    if not names:
        raise SystemExit("no agents/*/harness.json found -- refusing to wire nothing")
    return names

STRATEGIES = [
    {"semanticMemoryStrategy": {
        "name": "llmops_facts",
        "namespaces": ["/users/{actorId}/facts"]}},
    # reflection namespace must equal or prefix the episodic namespace (live-verified)
    {"episodicMemoryStrategy": {
        "name": "llmops_episodes",
        "namespaces": ["/episodes/{actorId}/{sessionId}"],
        "reflectionConfiguration": {"namespaces": ["/episodes/{actorId}"]}}},
]


def ensure_memory(ctl, dry):
    for m in ctl.list_memories().get("memories", []):
        if m.get("id", "").startswith(MEMORY_NAME) or m.get("name") == MEMORY_NAME:
            return m["id"], m["arn"], False
    if dry:
        return "MEMORY-DRYRUN", "arn:aws:bedrock-agentcore:<REGION>:<ACCOUNT_ID>:memory/DRYRUN", True
    resp = ctl.create_memory(
        name=MEMORY_NAME,
        description="Shared cross-run memory for all llmops-agentic-system harnesses",
        memoryStrategies=STRATEGIES,
        eventExpiryDuration=90,
        clientToken=secrets.token_hex(20),
    )
    mem = resp["memory"]
    # wait ACTIVE
    for _ in range(60):
        cur = ctl.get_memory(memoryId=mem["id"])["memory"]
        if cur["status"] == "ACTIVE":
            break
        if cur["status"] == "FAILED":
            raise RuntimeError(f"memory FAILED: {cur.get('failureReason')}")
        time.sleep(5)
    return mem["id"], mem["arn"], True


def strategy_ids(ctl, memory_id, dry):
    if dry:
        return {"SEMANTIC": "sem-DRYRUN", "EPISODIC": "epi-DRYRUN"}
    out = {}
    for s in ctl.get_memory(memoryId=memory_id)["memory"].get("strategies", []):
        out[s["type"]] = s["strategyId"]
    return out


def resolve_harness_id(ctl, name):
    """UpdateHarness rejects the bare name (live: ValidationException, pattern
    `[a-zA-Z][a-zA-Z0-9_]{0,39}-[a-zA-Z0-9]{10}`) — same resolution 05_harnesses.py does."""
    for h in ctl.list_harnesses().get("harnesses", []):
        if h.get("name") == name or h.get("harnessId", "").rsplit("-", 1)[0] == name:
            return h["harnessId"]
    raise SystemExit(f"harness '{name}' not found — run 05_harnesses.py first")


def check_repartition(harnesses, repartition):
    """A --repartition for a harness this run is not wiring is a no-op that reads as done."""
    unknown = sorted(set(repartition or []) - set(harnesses))
    if unknown:
        raise SystemExit(f"--repartition names harnesses not being wired: {unknown}")


def count_facts(dp, memory_id, actor_id):
    """How many semantic records live in one actor's partition.

    Read from the data plane rather than assumed, because it is the number that decides
    whether a repartition is free or destructive, and it is not derivable from anything
    in this repo.
    """
    ns = f"/users/{actor_id}/facts"
    total, kw = 0, {"memoryId": memory_id, "namespace": ns, "maxResults": 100}
    while True:
        r = dp.list_memory_records(**kw)
        total += len(r.get("memoryRecordSummaries", []))
        tok = r.get("nextToken")
        if not tok:
            return total
        kw["nextToken"] = tok


def resolve_actor_id(ctl, harness_id, harness_name, repartition):
    """The actorId already live wins over the one this script would choose.

    actorId is the PARTITION KEY of every namespace (`/users/{actorId}/facts`), so
    rewriting it does not move a memory -- it abandons one, and UpdateHarness returns
    success either way. Measured live 2026-08-13: llmops_finops and llmops_orchestrator
    were wired (by deploy/wire_memory.py, whose --actor-id took the harness ID) under
    `llmops_finops-eDJtU9PvKh` / `llmops_orchestrator-GsIqHZ4viJ`, and those two
    partitions hold 13 and 30 semantic records while every bare-name partition holds 0.
    So this script's own preferred value -- the bare name, the only spelling stable
    across a harness recreation -- would have discarded all 43 in one call, and the
    redeploy that applies the retrieval fix is exactly when it would have happened. A
    fix that silently destroys the data it exists to serve is worse than the defect.

    A repartition is therefore opt-in per harness AND announced with the count it costs.
    """
    try:
        cur = ctl.get_harness(harnessId=harness_id)["harness"]
    except Exception:  # noqa: BLE001 — never wired is the normal first-deploy case
        return harness_name, False
    live = (((cur.get("memory") or {}).get("agentCoreMemoryConfiguration") or {})
            .get("actorId"))
    if not live or live == harness_name:
        return harness_name, False
    if harness_name in (repartition or []):
        return harness_name, True
    return live, False


def attach_to_harness(ctl, harness_name, memory_arn, sids, dry, dp=None,
                      memory_id=None, repartition=None):
    """UpdateHarness with the BYO memory block (wrapped in optionalValue)."""
    retrieval = {}
    if "SEMANTIC" in sids:
        # Semantic facts are the CROSS-RUN channel, and 0.2 was no threshold at all:
        # live, every finetune invocation received exactly topK=10 "facts", including a
        # post-mortem of a DIFFERENT run's MissingStageComplete and hyperparameters from
        # an unrelated dataset -- another run's conclusions injected invisibly as truth.
        # 0.6/5 keeps the run-N -> run-N+1 learning channel open but makes it earn
        # relevance; the episodic namespace below stays at 0.2 because {sessionId}
        # scopes it to the agent's OWN session.
        retrieval["/users/{actorId}/facts"] = {
            "strategyId": sids["SEMANTIC"], "topK": 5, "relevanceScore": 0.6}
    if "EPISODIC" in sids:
        retrieval["/episodes/{actorId}/{sessionId}"] = {
            "strategyId": sids["EPISODIC"], "topK": 10, "relevanceScore": 0.2}
    harness_id = resolve_harness_id(ctl, harness_name)
    actor_id, repartitioned = resolve_actor_id(ctl, harness_id, harness_name, repartition)
    abandoning = None
    if repartitioned:
        if dp is None or memory_id is None:
            raise SystemExit(
                f"--repartition {harness_name} needs the data plane to say what it costs")
        abandoning = count_facts(dp, memory_id, ctl.get_harness(harnessId=harness_id)
                                 ["harness"]["memory"]["agentCoreMemoryConfiguration"]
                                 ["actorId"])
    block = {
        "agentCoreMemoryConfiguration": {
            "arn": memory_arn,
            "actorId": actor_id,
            "messagesCount": 20,
            "retrievalConfig": retrieval,
        }
    }
    out = {"harness": harness_name, "actorId": actor_id}
    if actor_id != harness_name:
        out["kept_live_actor_id"] = True
    if repartitioned:
        out["repartitioned_from_records"] = abandoning
    if dry:
        out["would_attach"] = block
        return out
    ctl.update_harness(
        harnessId=harness_id,
        memory={"optionalValue": block},
        clientToken=secrets.token_hex(20),
    )
    out["attached"] = True
    return out


def grant_iam(iam, memory_arn, dry):
    """Per-Memory data-plane grant on the harness execution role (skill 3rd step)."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "SharedMemoryDataPlane",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:CreateEvent", "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:ListEvents", "bedrock-agentcore:DeleteEvent",
                "bedrock-agentcore:RetrieveMemoryRecords",
                "bedrock-agentcore:ListMemoryRecords", "bedrock-agentcore:GetMemoryRecord",
            ],
            "Resource": memory_arn,
        }],
    }
    if dry:
        return {"would_grant": "llmops-harness-execution/SharedMemoryAccess"}
    iam.put_role_policy(RoleName="llmops-harness-execution",
                       PolicyName="SharedMemoryAccess",
                       PolicyDocument=json.dumps(policy))
    return {"granted": "SharedMemoryAccess"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--harness", action="append",
                    help="harness name(s); default every agents/*/harness.json")
    ap.add_argument("--repartition", action="append", metavar="HARNESS",
                    help="rewrite this harness's live actorId to the bare name, "
                         "ABANDONING the memory records under the old one")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    harnesses = args.harness or harness_names()
    check_repartition(harnesses, args.repartition)

    ctl = boto3.client("bedrock-agentcore-control", region_name=args.region)
    dp = boto3.client("bedrock-agentcore", region_name=args.region)
    iam = boto3.client("iam", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)

    mem_id, mem_arn, created = ensure_memory(ctl, args.dry_run)
    sids = strategy_ids(ctl, mem_id, args.dry_run)
    grant = grant_iam(iam, mem_arn, args.dry_run)
    attached = [attach_to_harness(ctl, h, mem_arn, sids, args.dry_run, dp=dp,
                                  memory_id=mem_id, repartition=args.repartition)
                for h in harnesses]

    if not args.dry_run:
        ssm.put_parameter(Name="/llmops/memory/id", Value=mem_id, Type="String", Overwrite=True)
        ssm.put_parameter(Name="/llmops/memory/arn", Value=mem_arn, Type="String", Overwrite=True)

    print(json.dumps({"memory_id": mem_id, "created": created,
                      "strategies": sids, "iam": grant,
                      "attached": attached, "dry_run": args.dry_run}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
