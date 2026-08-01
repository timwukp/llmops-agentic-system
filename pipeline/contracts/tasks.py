"""Which harness tasks have a dispatch site, and which are knowingly without one.

Companion to ``events.EVENTS_NEEDING_A_RULE``, and it exists for the same reason: a
capability the prompts, the docs and the IAM all describe, that no code path can ever
reach, is invisible from inside the thing that declares it. An emitted event with no rule
and a declared task with no dispatch site are the same defect wearing different clothes.

This recurred three times before it earned a guard:

  * ``page_human`` (#54) — declared on the orchestrator since Phase 5, serviced only by the
    console's chat worker, while triage runs on the DRIVER. Live: the conductor was
    correctly told to page a human, tried, hit the unknown-tool branch, and the turn ended.
    Zero pages sent.
  * eval ``evaluate`` (#57) — the ``gate`` task applies thresholds to
    ``evaluation/report.json``, and nothing in the platform dispatched the task that WRITES
    that report.
  * monitor ``health`` / ``sweep`` / ``report`` (#58) — an entire harness, one of seven,
    with three declared tasks and zero dispatch sites, for the platform's whole life.

So the rule is declared here and checked offline (``test_orchestration.py``): every task
any ``agents/*/harness.json`` prompt declares must either be dispatched by the state
machine, or by a dispatch site named in ``NON_ASL_DISPATCH_SITES``, or appear in
``TASKS_WITHOUT_A_DISPATCH_SITE`` with a reason.

The allowlist is the point, not an escape hatch. "Not dispatched" and "we forgot to
dispatch it" are the same observation from the outside, and only a human writing down which
one it is can tell them apart — an empty allowlist would mean this file has nothing to say,
not that the platform is complete.

Only stdlib (Lambda-safe, no external deps).
"""
from __future__ import annotations

#: (harness_dir, task) -> repo-relative file that dispatches it, for the dispatch sites the
#: state machine does not own. ASL-dispatched tasks are NOT listed: they are read straight
#: out of ``state_machine.asl.json``, which carries stage + task + harness_id on every
#: payload and so needs no bookkeeping here. Bookkeeping that restates a fact the source
#: already states is bookkeeping that will disagree with it.
#:
#: The test verifies each named file exists and mentions BOTH the harness id and the task,
#: so an entry cannot survive the code it points at being deleted or renamed.
NON_ASL_DISPATCH_SITES: dict = {
    # Scheduled, because a cost audit is about a period, not a run: 08_triggers.py's
    # llmops-finops-daily invokes this Lambda, whose TASKS tuple gates all three.
    ("finops", "reconcile"): "orchestration/finops_reconcile/handler.py",
    ("finops", "pricing_refresh"): "orchestration/finops_reconcile/handler.py",
    ("finops", "report"): "orchestration/finops_reconcile/handler.py",
    # Scheduled, because an orphan endpoint belongs to a run that already ended: no live
    # agent is left inside it to look. See that handler's docstring.
    ("monitor", "sweep"): "orchestration/monitor_sweep/handler.py",
    # Interactive, because a consultation is a human typing: the console's task-chat worker
    # invokes the conductor directly rather than through the driver, since there is no task
    # token to settle and no state machine execution to belong to.
    ("orchestrator", "consult"): "deploy/console/lambda_function.py",
}

#: (harness_dir, task) -> why it has no dispatch site at all.
#:
#: An entry here claims the task is reachable by design some other way, or that its absence
#: is a known and accepted gap. Adding one is cheap; adding one dishonestly is how #54, #57
#: and #58 each stayed hidden for months, so each reason names the actual caller or the
#: actual gap, and says which of the two it is.
TASKS_WITHOUT_A_DISPATCH_SITE: dict = {
    ("data-prep", "verify"): (
        "BY DESIGN. Toolchain self-proof, run by hand during bring-up and kept for the next "
        "time a harness stops working ('list your mounted skills, call STS, list training "
        "jobs'). It answers a human's question about the platform, not a run's question "
        "about a model, so nothing scheduled or sequenced should call it."),
    ("data-prep", "mirror_model"): (
        "ACCEPTED GAP. Supply-chain step, invoked per model rather than per run: it pins an "
        "hf_repo at a commit SHA and is idempotent-skip once mirrored, so putting it on a "
        "run's path would re-verify hashes of unchanged bytes every run. No model has been "
        "mirrored yet and all 19 skill sources are still git, so nothing has called it -- "
        "tracked with the git->s3 source switch."),
    ("finetune", "prepare"): (
        "BY DESIGN, absorbed by `launch`: the ASL goes FinetuneLaunch -> FinetuneAnalyze "
        "with no prepare state, and the launch prompt reads curated.jsonl and writes "
        "training/config.json itself. The clause stays because `remediate` writes a REVISED "
        "training/config.json and is told to 'proceed exactly as launch' -- both share "
        "prepare's contract, so deleting it would delete the description of what they do."),
    ("eval", "evaluate"): (
        "ACCEPTED GAP, closed on a branch: `gate` applies thresholds to "
        "evaluation/report.json and nothing writes that report (#57). The EvalGenerate "
        "state exists on fix/eval-generate-dispatch (PR #33) and not on main. Kept here so "
        "the guard runs at all until that merges -- an entry that has to be REMOVED to go "
        "green is a to-do the test itself enforces."),
    ("orchestrator", "plan"): (
        "BY DESIGN, subsumed by `consult`: a run plan is what a consultation PRODUCES, and "
        "the console dispatches the conversation, not the artifact. `plan` remains declared "
        "as the contract for the plan JSON that both `consult` and conductor_tools' "
        "launch_run servicing read, so it describes a shape rather than a turn."),
    ("orchestrator", "triage"): (
        "ACCEPTED GAP, closed on a branch: an EscalatedToHuman rule on the llmops-pipeline "
        "bus dispatches it, and that bus had ZERO rules (#59). The rule exists on "
        "feat/route-escalations-to-conductor (PR #35) and not on main -- which is exactly "
        "why the driver's page_human servicing (#54) sat unreachable."),
    ("orchestrator", "report"): (
        "ACCEPTED GAP. A cross-run rollup nothing invokes: the conductor has a "
        "`write_report` tool it can call mid-turn, but no caller ever dispatches the report "
        "TASK, so the rollup only ever happens as a side-effect of somebody chatting. The "
        "shape wants what finops has -- a schedule plus an on-demand console route."),
}


def declared_tasks(prompt_text: str) -> set:
    """The tasks a harness prompt declares, read from its own ``- "name": …`` clauses.

    Derived from the prompt rather than a hand-kept list, because the prompt IS the contract
    the agent is judged against — a task added there and nowhere else is exactly the defect
    this module exists to catch, and a list maintained by hand would have to be updated by
    the same person who forgot the dispatch site.
    """
    tasks = set()
    for line in prompt_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith('- "'):
            continue
        name, _, rest = stripped[3:].partition('"')
        if rest.startswith(":") and name and name.replace("_", "").isalnum():
            tasks.add(name)
    return tasks


def prompt_text(cfg: dict) -> str:
    """A harness config's system prompt as one string.

    ``systemPrompt`` is a list of ``{"text": …}`` content blocks in every harness.json, but
    a single string is also valid to AgentCore, so accept both rather than crashing the
    guard on a config that AWS would happily take.
    """
    sp = cfg.get("systemPrompt") or cfg.get("system_prompt") or ""
    if isinstance(sp, str):
        return sp
    return "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in sp)
