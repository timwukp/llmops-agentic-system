#!/usr/bin/env python3
"""04_wire_memory.py — create the SHARED BYO AgentCore Memory and wire it to harnesses.

One Memory (`llmops-shared-memory`, SEMANTIC + EPISODIC) is shared by all five
worker harnesses — that is the mechanism by which run-N learnings ("lr 2e-4
overfit on this dataset", "grid prompts need explicit size hints") reach run-N+1
across every stage.

Wraps the agentcore-harness-builder skill's wire_memory.py three-step wiring
(CreateMemory → UpdateHarness(optionalValue) → IAM inline grant) but:
  - creates the memory ONCE and attaches it to EVERY harness passed via --harness
  - uses only SEMANTIC + EPISODIC strategies (facts + what-happened; we skip
    USER_PREFERENCE/SUMMARIZATION — there is no human user in the loop)
  - actorId per-agent = the harness name, so namespaces partition by agent while
    retrieval can still cross-read shared facts
  - publishes the memory id/arn to SSM /llmops/memory/*

Usage:
  python deploy/04_wire_memory.py --region us-east-1 --dry-run
  python deploy/04_wire_memory.py --region us-east-1                       # all 5 harnesses
  python deploy/04_wire_memory.py --region us-east-1 --harness llmops_data_prep
"""
import argparse
import json
import secrets
import sys
import time

import boto3

MEMORY_NAME = "llmops_shared_memory"
DEFAULT_HARNESSES = ["llmops_data_prep", "llmops_finetune", "llmops_eval",
                     "llmops_deploy", "llmops_monitor"]

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


def attach_to_harness(ctl, harness_name, memory_arn, sids, dry):
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
    block = {
        "agentCoreMemoryConfiguration": {
            "arn": memory_arn,
            "actorId": harness_name,
            "messagesCount": 20,
            "retrievalConfig": retrieval,
        }
    }
    if dry:
        return {"harness": harness_name, "would_attach": block}
    ctl.update_harness(
        harnessId=resolve_harness_id(ctl, harness_name),
        memory={"optionalValue": block},
        clientToken=secrets.token_hex(20),
    )
    return {"harness": harness_name, "attached": True}


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
                    help="harness name(s); default all five")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    harnesses = args.harness or DEFAULT_HARNESSES

    ctl = boto3.client("bedrock-agentcore-control", region_name=args.region)
    iam = boto3.client("iam", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)

    mem_id, mem_arn, created = ensure_memory(ctl, args.dry_run)
    sids = strategy_ids(ctl, mem_id, args.dry_run)
    grant = grant_iam(iam, mem_arn, args.dry_run)
    attached = [attach_to_harness(ctl, h, mem_arn, sids, args.dry_run) for h in harnesses]

    if not args.dry_run:
        ssm.put_parameter(Name="/llmops/memory/id", Value=mem_id, Type="String", Overwrite=True)
        ssm.put_parameter(Name="/llmops/memory/arn", Value=mem_arn, Type="String", Overwrite=True)

    print(json.dumps({"memory_id": mem_id, "created": created,
                      "strategies": sids, "iam": grant,
                      "attached": attached, "dry_run": args.dry_run}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
