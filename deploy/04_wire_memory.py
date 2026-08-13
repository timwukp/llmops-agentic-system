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
  - every attach reports the records held under the OTHER spelling of actorId, so a
    partition an earlier deploy walked away from stops looking like an empty one
    (see `stranded_partitions` — 63 records are in that state right now)
  - and once per run, every actor on the memory that no harness points at, enumerated
    from ListActors rather than from this repo's two naming conventions, because 9 of
    those records sit under actorIds this repo never had (see `unreachable_actors`)
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


def resolve_harness_id(ctl, name, required=True):
    """UpdateHarness rejects the bare name (live: ValidationException, pattern
    `[a-zA-Z][a-zA-Z0-9_]{0,39}-[a-zA-Z0-9]{10}`) — same resolution 05_harnesses.py does.

    `required=False` returns None instead of exiting, for the memory-level sweep: a harness
    this repo defines but has not created yet must not abort a report about OTHER harnesses'
    records.
    """
    for h in ctl.list_harnesses().get("harnesses", []):
        if h.get("name") == name or h.get("harnessId", "").rsplit("-", 1)[0] == name:
            return h["harnessId"]
    if not required:
        return None
    raise SystemExit(f"harness '{name}' not found — run 05_harnesses.py first")


def check_repartition(harnesses, repartition):
    """A --repartition for a harness this run is not wiring is a no-op that reads as done."""
    unknown = sorted(set(repartition or []) - set(harnesses))
    if unknown:
        raise SystemExit(f"--repartition names harnesses not being wired: {unknown}")


def count_records(dp, memory_id, namespace):
    """How many memory records live in one namespace, every page of it.

    Read from the data plane rather than assumed, because it is the number that decides
    whether a repartition is free or destructive, and it is not derivable from anything
    in this repo.
    """
    total, kw = 0, {"memoryId": memory_id, "namespace": namespace, "maxResults": 100}
    while True:
        r = dp.list_memory_records(**kw)
        total += len(r.get("memoryRecordSummaries", []))
        tok = r.get("nextToken")
        if not tok:
            return total
        kw["nextToken"] = tok


def count_facts(dp, memory_id, actor_id):
    """The semantic partition of one actor."""
    return count_records(dp, memory_id, f"/users/{actor_id}/facts")


def stranded_partitions(dp, memory_id, harness_id, harness_name, actor_id):
    """Records this harness holds under a spelling of actorId it is NOT being wired with.

    Keeping a live actorId (see `resolve_actor_id`) protects a partition from THIS
    deploy; it does nothing about a partition an EARLIER deploy already walked away from,
    and that is not hypothetical. Measured live 2026-08-13, memory
    llmops_shared_memory-hbEZ9K8d57: all seven harnesses hold semantic records under
    their FULL harness ID -- 2 / 25 / 16 / 11 / 9 for the five pipeline workers, 13 and
    30 for finops and the orchestrator, 106 in total -- while every bare-name semantic
    partition holds 0. The five workers' live actorId is the bare name, so 63 of those
    106 are already unreachable by the agents that wrote them, and llmops_monitor's
    newest orphaned record is dated 2026-08-08: the move happened days ago, silently,
    by exactly the mechanism this finding is about. The two that were spared were spared
    only because the hand-written list omitted them.

    Nothing in the control plane can report this -- UpdateHarness succeeded, and a
    harness wired to the wrong partition looks identical to one whose memory is simply
    empty. So the count is read on every attach, not only when --repartition is asked
    for, and BOTH candidate spellings are checked rather than the one this script would
    have chosen: an actorId that is neither (a hand-set value, a rename) otherwise hides
    a partition behind an assumption about which spelling went stale.

    There is no API that moves a record between namespaces, so this is a report, not a
    repair. What it buys is that "the memory is empty" and "the memory is 25 records away
    from here" stop looking the same in a deploy log.
    """
    found = {}
    for other in [s for s in (harness_name, harness_id) if s != actor_id]:
        # /episodes/{actorId} is the episodic strategy's reflection namespace, and it is
        # a real namespace holding real records (live-verified) -- the episodic channel
        # strands the same way the semantic one does.
        for ns in (f"/users/{other}/facts", f"/episodes/{other}"):
            n = count_records(dp, memory_id, ns)
            if n:
                found[ns] = n
    return found


def reachable_actor_ids(ctl, harnesses, repartition):
    """The actorId each harness will be reachable at AFTER this run.

    Read for EVERY harness this repo defines, not only the ones this invocation wires:
    with `--harness llmops_eval`, a set built from the wired list alone would call the
    other six harnesses' own partitions unreachable, and a sweep that cries wolf on six
    healthy partitions is a sweep nobody reads.
    """
    out = {}
    for name in harnesses:
        harness_id = resolve_harness_id(ctl, name, required=False)
        if harness_id is None:
            out[name] = name  # not created yet; the bare name is what it will be given
            continue
        out[name] = resolve_actor_id(ctl, harness_id, name, repartition)[0]
    return out


def unreachable_actors(dp, memory_id, reachable):
    """Every actor holding records on this memory that no harness will be pointing at.

    `stranded_partitions` asks a narrower question -- it compares the two spellings THIS
    repo can produce (bare name, full harness id). That candidate list cannot contain a
    spelling this repo never had, and the memory has some: measured live 2026-08-13,
    `monitor` holds 3 semantic records and `monitor-agent` holds 6, actorIds that appear in
    no file in this repo (they came from deploy/wire_memory.py's free-form --actor-id). So
    9 records were invisible to a check whose own docstring claimed to cover "an actorId
    that is neither". The list is therefore derived from ListActors -- the data plane's own
    enumeration of who has written -- and the two checks answer different questions: the
    per-harness one says WHICH harness lost a partition, this one says whether anything at
    all on the memory is orphaned. Their counts overlap; they are not additive.

    Same reporting contract as `stranded_partitions`: no API moves a record between
    namespaces, so this is a report. What it buys is that an actorId typo, a rename, or a
    hand-set --actor-id stops being indistinguishable from an empty memory.
    """
    found, tok = {}, None
    while True:
        kw = {"memoryId": memory_id, "maxResults": 100}
        if tok:
            kw["nextToken"] = tok
        resp = dp.list_actors(**kw)
        for summary in resp.get("actorSummaries", []):
            actor = summary.get("actorId")
            if not actor or actor in reachable:
                continue
            counts = {ns: count_records(dp, memory_id, ns)
                      for ns in (f"/users/{actor}/facts", f"/episodes/{actor}")}
            counts = {ns: n for ns, n in counts.items() if n}
            if counts:
                found[actor] = counts
        tok = resp.get("nextToken")
        if not tok:
            return found


def resolve_actor_id(ctl, harness_id, harness_name, repartition):
    """The actorId already live wins over the one this script would choose.

    actorId is the PARTITION KEY of every namespace (`/users/{actorId}/facts`), so
    rewriting it does not move a memory -- it abandons one, and UpdateHarness returns
    success either way. Measured live 2026-08-13: llmops_finops and llmops_orchestrator
    were wired (by deploy/wire_memory.py, whose --actor-id took the harness ID) under
    `llmops_finops-eDJtU9PvKh` / `llmops_orchestrator-GsIqHZ4viJ`, and those two
    partitions hold 13 and 30 semantic records while every bare-name partition holds 0.
    So this script's own preferred value -- the bare name, the only spelling stable
    across a harness recreation -- would have discarded those 43 in one call, and the
    redeploy that applies the retrieval fix is exactly when it would have happened. A
    fix that silently destroys the data it exists to serve is worse than the defect.

    A repartition is therefore opt-in per harness AND announced with the count it costs.

    43 is what THIS guard still protects, not the size of the problem: the other five
    harnesses hold 63 more semantic records under their full harness IDs and have already
    been moved to the bare name by an earlier run of this script. Keeping a live actorId
    cannot bring those back -- see `stranded_partitions`, which reports them.
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
    if dp is not None and memory_id is not None:
        stranded = stranded_partitions(dp, memory_id, harness_id, harness_name, actor_id)
        if stranded:
            out["stranded"] = stranded
            print(f"WARNING {harness_name}: {sum(stranded.values())} memory records are "
                  f"NOT reachable with actorId={actor_id}: {stranded}", file=sys.stderr)
    else:
        # An unchecked partition must not print as an empty one; that equivalence is the
        # whole reason 63 records went missing without a single failed call.
        out["stranded_check"] = "SKIPPED: no data plane -- unknown is not zero"
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

    # The memory-level half of the same question: not "did THIS harness lose a partition"
    # but "is anything on this memory orphaned at all", derived from ListActors so a
    # spelling this repo never produced cannot hide.
    if mem_id == "MEMORY-DRYRUN":
        sweep = {"unreachable_check": "SKIPPED: no memory yet -- unknown is not zero"}
    else:
        reachable = set(reachable_actor_ids(ctl, harness_names(), args.repartition).values())
        orphans = unreachable_actors(dp, mem_id, reachable)
        sweep = {"unreachable_actors": orphans,
                 "unreachable_records": sum(sum(v.values()) for v in orphans.values())}
        if orphans:
            print(f"WARNING {sweep['unreachable_records']} memory records on {mem_id} are "
                  f"held by actorIds no harness points at: "
                  f"{json.dumps(orphans, sort_keys=True)}", file=sys.stderr)

    if not args.dry_run:
        ssm.put_parameter(Name="/llmops/memory/id", Value=mem_id, Type="String", Overwrite=True)
        ssm.put_parameter(Name="/llmops/memory/arn", Value=mem_arn, Type="String", Overwrite=True)

    print(json.dumps({"memory_id": mem_id, "created": created,
                      "strategies": sids, "iam": grant,
                      "attached": attached, "dry_run": args.dry_run, **sweep}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
