"""Negative controls for #58: break one thing, confirm the matching guard fails, restore.

A guard nobody has ever seen fail is a guard nobody has tested. Every test in this repo's
suite asserts something about a file, and a test that reads the wrong file, greps a pattern
that no longer exists, or asserts a tautology passes exactly as loudly as one that works.
The only way to know a guard guards is to break what it guards and watch it go red.

Run it: ``.venv/bin/python tests/negative_controls/monitor_dispatch.py``

It is deliberately NOT a pytest module. Each case edits a tracked source file in place and
restores it in a ``finally``; collecting that alongside the suite it mutates would let a
crash mid-case leave the working tree broken and the next run's results meaningless.

Three lessons are baked in, all learned by this harness reporting a false result:

  * **Assert the mutation applied.** A ``str.replace`` whose pattern has drifted is a no-op,
    and a no-op case prints "the guard caught nothing" -- which reads identically to a guard
    that does not work. Four cases here were silently no-ops on the first run.
  * **Turn bytecode caching off.** CPython validates a ``.pyc`` against
    ``(source mtime in WHOLE SECONDS, source size)``. The ``sweep_id`` mutation and the
    ``TASKS`` mutation of ``monitor_sweep/handler.py`` both produce 7505 bytes; run in the
    same second, the second one imported the FIRST one's bytecode. The source on disk had
    changed, the assertion that it changed passed, and the code under test was someone
    else's -- so ``test_the_sweep_lambda_refuses_the_run_scoped_monitor_tasks`` was reported
    as an uncaught break for a full debugging round while being perfectly sound. Verifying
    the patch applied is necessary and not sufficient: the interpreter has its own opinion
    about what the source is.
  * **A ``finally`` does not run when the process is signalled.** The restore has always been
    inside a ``finally``, and a mutation still leaked to disk: killing this runner at a
    two-minute tool timeout left ``m52``'s edit to ``deploy/03_storage.py`` (``NO JOB SCANS``
    -> ``no llmops job for``) in the working tree, noticed afterwards only by ``git status``.
    SIGTERM's default disposition terminates the process outright -- no unwinding, no
    ``finally``, no ``atexit``. A full run takes ~3 minutes, so being killed partway is the
    ordinary case rather than the exceptional one, and the leak is silent in the worst way:
    the next run mutates an already-mutated file, so every result after it is meaningless
    while still printing PASS. Hence the two defences below -- handlers that turn a signal
    into an ordinary exception so the existing ``finally`` fires, and a journal on disk that
    survives even ``SIGKILL``, which no handler is allowed to intercept.
"""
import importlib.util, json, os, pathlib, re, shutil, signal, subprocess, sys

REPO = pathlib.Path(__file__).resolve().parents[2]

#: pytest's exit codes. 1 == tests failed, which is the ONLY outcome that proves a guard
#: fired. 4 (usage/collection error) and 5 (nothing collected) are also non-zero, so a case
#: naming a test that does not exist would otherwise be scored as a catch -- and a typo in a
#: test id is the single likeliest defect in a file that is nothing but test ids.
PYTEST_TESTS_FAILED = 1


def run(test):
    """Run one test with bytecode caching OFF.

    Without PYTHONDONTWRITEBYTECODE the harness silently lies. CPython validates a .pyc on
    (source mtime in WHOLE SECONDS, source size) -- so two mutations of the same file that
    happen in the same second and produce the same byte count are indistinguishable, and the
    second one imports the FIRST one's bytecode. That is exactly what happened here: the
    sweep_id mutation and the TASKS mutation both yield 7505 bytes, ran within one second of
    each other, and the TASKS case reported "guard did not catch it" while running the
    sweep_id code. Asserting the source changed is not enough; the interpreter has its own
    idea of what the source is.
    """
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run(['.venv/bin/python', '-m', 'pytest', test, '-q', '--no-header', '-x'],
                       cwd=REPO, capture_output=True, text=True, env=env)
    return r.returncode, (r.stdout + r.stderr).strip().splitlines()[-1]

CASES = []

def case(name, path, mutate, tests):
    CASES.append((name, path, mutate, tests))

# 1. Delete MonitorHealth from the ASL -> the placement guard + the doc state count.
def m1(t):
    d = json.loads(t)
    del d["States"]["MonitorHealth"]
    d["States"]["SmokeTest"]["Next"] = "Teardown"
    return json.dumps(d, indent=2)
case("ASL: MonitorHealth deleted", "orchestration/state_machine.asl.json", m1,
     ["tests/test_orchestration.py::TestStateMachine::test_monitor_health_reads_metrics_while_the_endpoint_still_exists",
      "tests/test_docs_claims.py::test_the_documented_happy_path_state_count_matches_the_state_machine"])

# 2. MonitorReport moved BEFORE Teardown -> ordering guard.
def m2(t):
    d = json.loads(t)
    d["States"]["MonitorHealth"]["Next"] = "MonitorReport"
    d["States"]["MonitorReport"]["Next"] = "Teardown"
    d["States"]["Teardown"]["Next"] = "Complete"
    d["States"]["Teardown"].pop("End", None)
    return json.dumps(d, indent=2)
case("ASL: MonitorReport before Teardown", "orchestration/state_machine.asl.json", m2,
     ["tests/test_orchestration.py::TestStateMachine::test_monitor_report_runs_after_teardown_on_the_finished_manifest"])

# 3. MonitorHealth Catch pointed at Fail -> a metric read failure strands the endpoint.
def m3(t):
    d = json.loads(t)
    for c in d["States"]["MonitorHealth"]["Catch"]:
        c["Next"] = "Fail"
    return json.dumps(d, indent=2)
case("ASL: MonitorHealth Catch -> Fail", "orchestration/state_machine.asl.json", m3,
     ["tests/test_orchestration.py::TestStateMachine::test_teardown_always_follows_smoke_even_on_failure"])

# 4. ASL dispatches a task no prompt declares.
def m4(t):
    d = json.loads(t)
    d["States"]["MonitorHealth"]["Parameters"]["Payload"]["task"] = "healthcheck"
    return json.dumps(d, indent=2)
case("ASL: dispatches undeclared task 'healthcheck'", "orchestration/state_machine.asl.json", m4,
     ["tests/test_orchestration.py::test_the_state_machine_only_dispatches_tasks_the_prompts_declare"])

# 5. A prompt declares a task with no dispatch site and no allowlist entry.
def m5(t):
    d = json.loads(t)
    p = d["systemPrompt"][0]["text"]
    d["systemPrompt"][0]["text"] = p.replace(
        '- "sweep":', '- "forecast": predict next month spend.\n- "sweep":', 1)
    return json.dumps(d, indent=2, ensure_ascii=False)
case("prompt: undispatchable task 'forecast'", "agents/monitor/harness.json", m5,
     ["tests/test_orchestration.py::test_every_task_a_prompt_declares_can_actually_be_dispatched"])

# 6. Harness role loses ListTags -> the prompt-driven action guard. This is the case the
#    abbreviated 'list-tags' spelling made invisible: before the prompt fix it PASSED here.
def m6(t):
    d = json.loads(t)
    for st in d["permissionsPolicy"]["Statement"]:
        if st.get("Sid") == "SageMakerList":
            st["Action"] = [a for a in st["Action"] if a != "sagemaker:ListTags"]
    return json.dumps(d, indent=2)
case("IAM: sagemaker:ListTags removed", "deploy/iam/harness_execution_role.json", m6,
     ["tests/test_orchestration.py::test_every_aws_api_a_prompt_tells_an_agent_to_call_is_in_the_harness_role",
      "tests/test_orchestration.py::test_the_sweep_can_read_tags_of_endpoints_nobody_claimed"])

# 7. ListEndpoints/ListTags scoped to llmops-* -> the untagged orphan becomes invisible.
def m7(t):
    d = json.loads(t)
    for st in d["permissionsPolicy"]["Statement"]:
        if st.get("Sid") == "SageMakerList":
            st["Resource"] = ["arn:aws:sagemaker:*:*:endpoint/llmops-*"]
    return json.dumps(d, indent=2)
case("IAM: enumeration scoped to llmops-*", "deploy/iam/harness_execution_role.json", m7,
     ["tests/test_orchestration.py::test_the_sweep_can_read_tags_of_endpoints_nobody_claimed"])

# 8. Sweep schedule given a flexible window -> can drift past midnight UTC.
def m8(t):
    # Keyed off the sweep's own cron line so the finops schedule above it is untouched --
    # both carry Mode OFF, and a blind replace would break the wrong one.
    old = ('ScheduleExpression="cron(0 8 * * ? *)",  # 08:00 UTC daily\n'
           '        ScheduleExpressionTimezone="UTC",\n'
           '        FlexibleTimeWindow={"Mode": "OFF"},')
    assert old in t
    return t.replace(old, old.replace('{"Mode": "OFF"}',
        '{"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 60}'), 1)
case("triggers: sweep window FLEXIBLE", "deploy/08_triggers.py", m8,
     ["tests/test_orchestration.py::test_the_sweep_schedule_cannot_drift_into_the_wrong_day"])

# 9. Sweep writes into the runs table.
def m9(t):
    return t.replace('EVENTS_TABLE', 'RUNS_TABLE')
case("sweep: bookkeeping to RUNS_TABLE", "orchestration/monitor_sweep/handler.py", m9,
     ["tests/test_orchestration.py::test_a_sweep_row_never_lands_in_the_runs_table"])

# 10. Bookkeeping failure propagates -> a PutItem error loses an already-invoked sweep.
def m10(t):
    # Replace the whole try/except with a bare call rather than injecting `raise` after the
    # `except` line: that line carries a trailing em-dash comment, so a prefix-anchored
    # insert split it and the module stopped PARSING. A SyntaxError makes the guard's own
    # test UNCOLLECTABLE -- a non-zero pytest exit that looks exactly like the guard firing.
    old = ('    try:\n'
           '        record_outcome(c["ddb"], run_id, outcome)\n'
           '    except Exception as exc:  # noqa: BLE001'
           ' — a bookkeeping failure must not lose the sweep\n'
           '        print(f"[monitor-sweep] could not record outcome for {run_id}: {exc}")\n')
    assert old in t, "the sweep's bookkeeping try/except has moved; re-anchor this mutation"
    return t.replace(old, '    record_outcome(c["ddb"], run_id, outcome)\n', 1)
case("sweep: bookkeeping failure re-raised", "orchestration/monitor_sweep/handler.py", m10,
     ["tests/test_orchestration.py::test_a_bookkeeping_failure_does_not_lose_the_sweep"])

# 11. Sweep run_id from a uuid -> re-running a day is no longer idempotent.
def m11(t):
    old = 'return f"sweep-{today.isoformat()}"'
    assert old in t
    return t.replace(old, 'return f"sweep-{today.isoformat()}-{id(today) % 9973}"', 1)
case("sweep: non-deterministic sweep id", "orchestration/monitor_sweep/handler.py", m11,
     ["tests/test_orchestration.py::test_the_sweep_id_is_derived_from_the_date_so_re_running_a_day_is_idempotent"])

# 12. Sweep Lambda accepts the run-scoped tasks it must refuse.
def m12(t):
    return t.replace('TASKS = ("sweep",)', 'TASKS = ("sweep", "health", "report")')
case("sweep: accepts run-scoped monitor tasks", "orchestration/monitor_sweep/handler.py", m12,
     ["tests/test_orchestration.py::test_the_sweep_lambda_refuses_the_run_scoped_monitor_tasks"])

# 13. Scheduler role loses the sweep function ARN.
def m13(t):
    old = '            f"arn:aws:lambda:{region}:{account}:function:llmops-monitor-sweep",\n'
    assert old in t, "the scheduler role's resource list no longer names the sweep fn"
    return t.replace(old, '', 1)
case("triggers: scheduler role missing sweep fn", "deploy/08_triggers.py", m13,
     ["tests/test_orchestration.py::test_the_scheduler_role_may_invoke_every_function_this_deploy_schedules"])

# 14. A schedule the deployer creates, absent from the cost posture.
def m14(t):
    return t.replace("- the daily **08:00 UTC monitor sweep**", "- the daily 08:00 UTC MONITORSWEEPREDACTED")
case("PROJECT_STATE: sweep schedule unlisted", "PROJECT_STATE.md", m14,
     ["tests/test_docs_claims.py::test_every_schedule_the_deployer_creates_is_named_in_the_cost_posture"])

# 15. Lambda count drifts from the deployer.
def m15(t):
    old = "| Lambdas ×7 |"
    assert old in t, "PROJECT_STATE's Lambda count row has moved; re-anchor this mutation"
    return t.replace(old, "| Lambdas ×5 |", 1)
case("PROJECT_STATE: Lambda count drifted", "PROJECT_STATE.md", m15,
     ["tests/test_docs_claims.py::test_the_documented_state_and_lambda_counts_match_the_deployers"])

# 16. Drift emitter loosened to bool() -> "unknown" reports drift.
def m16(t):
    return t.replace('metrics.get("drift_detected") is True', 'bool(metrics.get("drift_detected"))')
case("driver: drift bool() instead of is True", "orchestration/harness_driver/handler.py", m16,
     ["tests/test_orchestration.py::test_only_a_literal_true_announces_drift"])

# 17. DescribeEndpoint pushed back into the scoped lifecycle statement -> the sweep can no
#     longer read the instance type of the untagged endpoint it flags, which is exactly the
#     gap the first live sweep filed against itself.
def m17(t):
    d = json.loads(t)
    stmts = d["permissionsPolicy"]["Statement"]
    read = next(st for st in stmts if st.get("Sid") == "SageMakerDescribeReadOnly")
    read["Resource"] = ["arn:aws:sagemaker:*:*:endpoint/llmops-*"]
    return json.dumps(d, indent=2)
case("IAM: Describe re-scoped to llmops-*", "deploy/iam/harness_execution_role.json", m17,
     ["tests/test_orchestration.py::test_the_sweep_can_characterise_an_orphan_it_may_not_touch"])

# 18. The read gap "fixed" by widening the LIFECYCLE statement instead -> DeleteEndpoint
#     account-wide for an agent whose prompt forbids deleting anything. This is the tempting
#     wrong fix, so the guard has to reject it as loudly as it rejects the gap itself.
def m18(t):
    d = json.loads(t)
    for st in d["permissionsPolicy"]["Statement"]:
        if st.get("Sid") == "SageMakerLifecycleScoped":
            st["Resource"] = "*"
    return json.dumps(d, indent=2)
case("IAM: lifecycle widened to '*'", "deploy/iam/harness_execution_role.json", m18,
     ["tests/test_orchestration.py::test_the_sweep_can_characterise_an_orphan_it_may_not_touch",
      "tests/test_orchestration.py::test_the_sweep_can_read_tags_of_endpoints_nobody_claimed"])

# 19. The IAM grant kept, the instruction removed -> the permission exists and nothing uses
#     it. The live sweep never took an AccessDenied on describe-endpoint; it never called it.
#     A guard that only reads the role file scores this half-fix as done.
def m19(t):
    d = json.loads(t)
    p = d["systemPrompt"][0]["text"]
    old = ("For every endpoint you flag, read its real instance type and count "
           "('aws sagemaker describe-endpoint' then 'aws sagemaker describe-endpoint-config' "
           "on the config it names) before you price it: ")
    assert old in p, "the sweep clause's describe instruction has moved; re-anchor this"
    d["systemPrompt"][0]["text"] = p.replace(old, "When pricing a flagged endpoint: ", 1)
    return json.dumps(d, indent=2, ensure_ascii=False)
case("prompt: describe instruction dropped", "agents/monitor/harness.json", m19,
     ["tests/test_orchestration.py::test_the_sweep_can_characterise_an_orphan_it_may_not_touch"])

# 20. The driver trusts the agent's echoed stage/task again -> a sweep files its findings
#     under task "" (what both live sweeps actually did), and the console's (stage, task)
#     derivation widens an empty task to any task of the stage.
def m20(t):
    old = ('    _record_stage_event(c["ddb"], run_id, stage, "stage_complete",\n'
           '                        _stamp_dispatch(norm, stage, task))\n')
    assert old in t, "the stage_complete event write has moved; re-anchor this mutation"
    return t.replace(
        old, '    _record_stage_event(c["ddb"], run_id, stage, "stage_complete", norm)\n', 1)
case("driver: event row trusts the agent's echoed task",
     "orchestration/harness_driver/handler.py", m20,
     ["tests/test_orchestration.py::test_the_event_row_records_the_task_that_was_dispatched_not_the_one_echoed"])

# ── #42: the s3 skill-source migration ────────────────────────────────────────────
# 21. mounted_skills() reverted to collecting git only -- the shape it had before the
#     switch. This is the case that matters most, because the bug it models is INVISIBLE:
#     with every source now s3, a git-only sync mirrors nothing, and the coverage guard
#     compares 0 mounts against 0 git sources and PASSES. The mirror would quietly stop
#     syncing, every config would still read healthy, and the next skill edit would reach
#     no agent. So the guard must fail on a sync that collects nothing, not just on one
#     that collects the wrong thing.
def m21(t):
    old = ('            elif isinstance(s3src, dict) and s3src.get("uri"):')
    assert old in t, "mounted_skills' s3 branch has moved; re-anchor this mutation"
    # neuter the s3 branch without changing the line count or the surrounding logic
    return t.replace(old, '            elif False:', 1)
case("storage: mounted_skills collects git sources only", "deploy/03_storage.py", m21,
     ["tests/test_orchestration.py::test_the_sync_covers_every_skill_the_configs_mount",
      "tests/test_orchestration.py::test_the_sync_still_covers_a_skill_once_its_source_is_s3"])

# 22. A literal bucket name written into a skill URI. It resolves, it works in THIS
#     account, and it passes the redaction scan whenever the name carries no digits --
#     which is why this needs its own guard rather than leaning on the account-id check:
#     a config pinned to one account's bucket is not deployable anywhere else, and the
#     failure appears at session start in the account that does not have that bucket.
def m22(t):
    d = json.loads(t)
    d["skills"][0]["s3"]["uri"] = "s3://my-skills-bucket/skills/llmops/llm-observability"
    return json.dumps(d, indent=2, ensure_ascii=False)
case("config: literal bucket in a skill URI", "agents/monitor/harness.json", m22,
     ["tests/test_orchestration.py::test_every_s3_skill_uri_uses_the_bucket_placeholder"])

# 23. An unknown token in a skill URI -- the `<DATABUCKET>` typo. Nothing else in the
#     chain rejects it: the JSON is valid, validate_config.py accepts the s3 shape, and
#     UpdateHarness accepts it, mints a version and reports READY. resolve() is the only
#     thing standing between the typo and a harness that cannot start a session.
def m23(t):
    d = json.loads(t)
    d["skills"][0]["s3"]["uri"] = "s3://<DATABUCKET>/skills/llmops/llm-observability"
    return json.dumps(d, indent=2, ensure_ascii=False)
case("config: unknown placeholder token in a skill URI", "agents/monitor/harness.json", m23,
     ["tests/test_orchestration.py::test_every_placeholder_a_config_uses_has_a_value"])

# 24. resolve() downgraded from raising to warning -- "substitute and carry on", the
#     tempting shape, since a warning still prints. It ships the config: the token reaches
#     AWS, the harness reports READY, and every session dies at start. A guard that only
#     checks the substitution happened would score this as fine.
def m24(t):
    old = "        raise SystemExit("
    assert old in t, "resolve()'s raise has moved; re-anchor this mutation"
    return t.replace(old, "        print(", 1)
case("config_subst: resolve() warns instead of raising", "deploy/config_subst.py", m24,
     ["tests/test_orchestration.py::test_resolve_refuses_a_config_with_a_token_left_in_it",
      "tests/test_orchestration.py::test_resolve_reports_every_unresolved_token_not_just_the_first"])

# 25. The deployer stops resolving -- load_config returns the raw config. The placeholder
#     then travels all the way into the UpdateHarness payload. This is the wiring case:
#     config_subst.py can be perfect and unreferenced.
def m25(t):
    old = "    return config_subst.resolve(cfg, mapping, where=str(path.relative_to(REPO)))"
    assert old in t, "load_config's resolve call has moved; re-anchor this mutation"
    return t.replace(old, "    return cfg", 1)
case("deployer: 05_harnesses stops resolving placeholders", "deploy/05_harnesses.py", m25,
     ["tests/test_orchestration.py::test_the_deployer_resolves_placeholders_before_sending_a_config"])

# 26. substitute() walks dict values only. `skills` is a LIST of dicts, so every flat
#     field resolves, `unresolved()` on the result still finds the URI's token, and the
#     deploy fails with a confusing message -- or, if the check were also list-blind,
#     ships the token. Recursion through lists is not incidental to this file.
def m26(t):
    old = "    if isinstance(obj, list):\n        return [substitute(x, mapping) for x in obj]\n"
    assert old in t, "substitute()'s list branch has moved; re-anchor this mutation"
    return t.replace(old, "", 1)
case("config_subst: substitute() does not recurse into lists", "deploy/config_subst.py", m26,
     ["tests/test_orchestration.py::test_substitution_reaches_a_skill_uri_nested_in_a_list"])

# 27. The doc-claim guard reads the git total instead of the total. It then passes on the
#     current tree by accident (19 != 0 is the only comparison that fires) while every doc
#     could say any number at all once the git count hits zero.
def m27(t):
    old = ('    wrong = [f"{name} says {n}, configs have {total} skill sources"\n'
           '             for name, n, _ in claims if n != total]')
    assert old in t, "the count comparison has moved; re-anchor this mutation"
    return t.replace(old, ('    wrong = [f"{name} says {n}, configs have {git_n} git sources"\n'
                           '             for name, n, _ in claims if n != git_n]'), 1)
case("docs guard: count compared against the git total", "tests/test_docs_claims.py", m27,
     ["tests/test_docs_claims.py::test_the_skill_source_claims_match_the_harness_configs"])

# 28. One doc left saying the sources are `git`. The count is still 19 and still correct,
#     so the count check cannot see it -- and the sentence tells the reader the migration
#     never happened, which is precisely the failure this whole guard exists to prevent.
def m28(t):
    old = "are `s3` today; none are `git`"
    assert old in t, "AGENTS.md's kind claim has moved; re-anchor this mutation"
    return t.replace(old, "are `git` today; none are `s3`", 1)
case("docs: a doc still calls the 19 sources git", "AGENTS.md", m28,
     ["tests/test_docs_claims.py::test_the_skill_source_claims_match_the_harness_configs"])

# 29. #32's mount guard read the git path only. After the switch every skill path became
#     "", so `want in {""}` was False and it failed -- but it would fail identically if the
#     mount had actually been deleted. Restoring the git-only read models a guard that can
#     no longer tell an intact mount from a missing one.
def m29(t):
    old = ('            out = set()\n'
           '            for s in h.get("skills") or []:\n'
           '                if "git" in s:\n'
           '                    out.add(s["git"].get("path", ""))\n'
           '                elif isinstance(s.get("s3"), dict):\n'
           '                    rest = s["s3"].get("uri", "").split("://", 1)[-1]\n'
           '                    out.add(rest.split("/", 1)[1].rstrip("/") if "/" in rest else "")\n'
           '            return out\n')
    assert old in t, "skill_paths' source-agnostic read has moved; re-anchor this mutation"
    return t.replace(
        old, '            return {s.get("git", {}).get("path", "") '
             'for s in (h.get("skills") or [])}\n', 1)
case("mount guard: skill_paths reads git paths only", "tests/test_orchestration.py", m29,
     ["tests/test_orchestration.py::TestConductorDispatch::test_the_agent_that_asks_about_data_has_the_skill_that_knows_how"])

# ── #61: the live bus vs the bytes about to ship ───────────────────────────────
# The driver was deployed WITHOUT triage_event_from_bus while llmops-escalation-triage was
# ENABLED and targeting it, so every escalation arrived as a raw EventBridge envelope and
# died on KeyError: 'run_id'. Every offline guard stayed green, because they compare this
# tree's declarations against this tree's deployer -- and the branch that overwrote the
# driver carried neither the declaration, nor the rule, nor the translator, which is
# perfectly self-consistent. These controls break each half of the new deploy-time check.

# 30. The regression itself: delete the translator from the driver.
def m30(t):
    old = "def triage_event_from_bus(record: dict, bucket: str) -> dict:"
    assert old in t, "the translator's signature has moved; re-anchor this mutation"
    return t.replace(old, "def _unused_translator(record: dict, bucket: str) -> dict:", 1)
case("driver: the bus envelope translator is gone", "orchestration/harness_driver/handler.py",
     m30,
     # Both of these read handler.py from disk, which is what makes them controllable from
     # here. This mutation renames only the DEFINITION and leaves the call site, so the
     # source still CONTAINS the name -- and the check's first version grepped for the bare
     # name and passed, on a handler that would raise NameError on the first escalation.
     # That is why live_bus_translator_gap looks for `def <name>(`; the shape is also
     # asserted directly by test_a_call_site_without_a_definition_does_not_satisfy_the_check,
     # which builds the mutated source in-process rather than from this file.
     ["tests/test_orchestration.py::test_the_real_driver_source_can_read_the_real_live_rule",
      "tests/test_orchestration.py::test_every_declared_translator_exists_in_the_handler_it_names"])

# 31. The entry-point dispatch, without which the translator exists and is never called --
#     the function would be present in the zip and the channel still dead. A grep for the
#     name alone cannot tell these apart, which is why there is a behavioural test too.
def m31(t):
    old = '    if event.get("detail-type") == ev.ESCALATED_TO_HUMAN and "detail" in event:'
    assert old in t, "the bus-delivery branch has moved; re-anchor this mutation"
    return t.replace(old, '    if False:', 1)
case("driver: the entry point stops recognising a bus delivery",
     "orchestration/harness_driver/handler.py", m31,
     ["tests/test_orchestration.py::test_the_driver_recognises_a_bus_delivery_at_its_entry_point"])

# 32. The check warns instead of refusing. The deploy then reports success while the
#     channel is dead -- the same reason config_subst.resolve() raises: an unresolved token
#     and an unreadable envelope are both accepted by the API and both fail out of sight.
def m32(t):
    old = "        if blocking:\n            raise SystemExit("
    assert old in t, "the refusal has moved; re-anchor this mutation"
    return t.replace(old, "        if blocking:\n            print(", 1)
case("deployer: the bus/translator check warns instead of refusing", "deploy/07_lambdas.py",
     m32,
     ["tests/test_orchestration.py::test_the_deploy_refuses_rather_than_warns_when_the_translator_is_missing"])

# 33. An unreachable bus reported as clean. This is the failure mode the guard was built to
#     prevent, reintroduced inside the guard: [] would make "no rules disagree" and "I could
#     not look" identical -- exactly the confusion #59 was about.
def m33(t):
    old = '        return [{"unchecked": f"{type(exc).__name__}: {exc}"}]'
    assert old in t, "the unreachable-bus branch has moved; re-anchor this mutation"
    return t.replace(old, "        return []", 1)
case("deployer: an unreachable bus is reported as clean", "deploy/07_lambdas.py", m33,
     ["tests/test_orchestration.py::test_an_unreachable_bus_is_reported_as_unchecked_not_as_clean"])

# 34. The check stops being scoped to the function being deployed, so every Lambda deploy is
#     gated on every rule on the bus. A guard that blocks correct deploys is one people
#     route around, which costs more than it saves.
def m34(t):
    old = '        mine = [t for t in targets if t.get("Arn", "").endswith(f":function:{fn}")]'
    assert old in t, "the target scoping has moved; re-anchor this mutation"
    return t.replace(old, "        mine = targets", 1)
case("deployer: the check is no longer scoped to the deployed function",
     "deploy/07_lambdas.py", m34,
     ["tests/test_orchestration.py::test_a_rule_targeting_a_different_function_is_not_this_deploys_problem"])

# 35. A DISABLED rule blocks the deploy. It cannot invoke anything, so this is a false
#     positive -- and false positives are how a deploy-time gate gets deleted.
def m35(t):
    old = '        if rule.get("State") != "ENABLED":\n            continue'
    assert old in t, "the ENABLED filter has moved; re-anchor this mutation"
    return t.replace(old, "        if False:\n            continue", 1)
case("deployer: a disabled rule blocks the deploy", "deploy/07_lambdas.py", m35,
     ["tests/test_orchestration.py::test_a_disabled_rule_delivers_nothing_and_blocks_nothing"])

# 36. The InputTransformer alternative is ignored. EventBridge has already reshaped the
#     event, so demanding the Python function blocks a correct deploy.
def m36(t):
    old = '            if any(t.get("InputTransformer") or t.get("Input") for t in mine):'
    assert old in t, "the InputTransformer branch has moved; re-anchor this mutation"
    return t.replace(old, "            if False:", 1)
case("deployer: an InputTransformer no longer substitutes for a translator",
     "deploy/07_lambdas.py", m36,
     ["tests/test_orchestration.py::test_an_input_transformer_on_the_rule_removes_the_need_for_a_translator"])

# 37. The driver is no longer marked as bus-delivered, so the check never runs for the one
#     function it exists to protect. This is how the guard dies quietly: nothing fails, and
#     the next branch to drop the translator ships exactly as this one did.
def m37(t):
    old = '        "bus_delivered": "llmops-pipeline",'
    assert old in t, "the driver's bus_delivered marker has moved; re-anchor this mutation"
    return t.replace(old, "", 1)
case("deployer: the driver is no longer checked against the bus at all",
     "deploy/07_lambdas.py", m37,
     ["tests/test_orchestration.py::test_only_the_bus_delivered_lambda_is_checked_against_the_bus"])

# 38. A live rule for a detail-type nothing declares a translator for is passed silently.
#     That is how the NEXT version of this defect arrives -- a new rule at the driver for an
#     event no branch handles, which is the same crash by a different route.
def m38(t):
    old = ('                gaps.append({"rule": rule["Name"], "detail_type": detail_type,\n'
           '                             "problem": "no translator declared in BUS_DELIVERY_TRANSLATORS"})\n'
           '                continue')
    assert old in t, "the undeclared-detail-type branch has moved; re-anchor this mutation"
    return t.replace(old, "                continue", 1)
case("deployer: an undeclared detail-type on a live rule passes silently",
     "deploy/07_lambdas.py", m38,
     ["tests/test_orchestration.py::test_a_live_rule_for_an_undeclared_detail_type_is_itself_reported"])

# 39. The two contracts drift apart: an event declared to need a rule to the driver, with no
#     translator declared for it. That is the half-built channel #59 found, mirrored.
def m39(t):
    old = "BUS_DELIVERY_TRANSLATORS: dict = {\n    ESCALATED_TO_HUMAN: \"triage_event_from_bus\",\n}"
    assert old in t, "BUS_DELIVERY_TRANSLATORS has moved; re-anchor this mutation"
    return t.replace(old, "BUS_DELIVERY_TRANSLATORS: dict = {}", 1)
case("contracts: a ruled event has no declared translator", "pipeline/contracts/events.py",
     m39,
     ["tests/test_orchestration.py::test_the_translator_declaration_covers_every_event_that_needs_a_rule"])

# ── #62: the escalate path must never MINT a run row ─────────────────────────────────
#
# 40. The exact regression: drop the ConditionExpression and update_item is an upsert
#     again, minting {run_id, status} for every non-run escalation. This is the state the
#     live sweep-2026-08-01 row was written from.
def m40(t):
    old = '            ConditionExpression="attribute_exists(run_id)",\n'
    assert old in t, "the escalate row-write condition has moved; re-anchor this mutation"
    return t.replace(old, "", 1)
case("driver: escalate writes the run row unconditionally (upsert mints a phantom run)",
     "orchestration/harness_driver/handler.py", m40,
     ["tests/test_orchestration.py::TestDriver::test_an_escalation_by_something_that_is_not_a_run_mints_no_run_row",
      "tests/test_orchestration.py::TestDriver::test_the_row_write_is_gated_on_the_row_existing_not_on_a_stage_allowlist"])

# 41. Absorb EVERY failure, not just the rejected condition. Worse than the original bug:
#     a run that really escalated keeps status=running and becomes the zombie MarkRunDone
#     and MarkRunFailed both exist to prevent.
def m41(t):
    old = ("        if _is_condition_failure(exc):\n"
           "            return False\n"
           "        raise")
    assert old in t, "the escalate error discrimination has moved; re-anchor this mutation"
    return t.replace(old, "        return False", 1)
case("driver: a throttle on the row write is read as 'this was not a run'",
     "orchestration/harness_driver/handler.py", m41,
     ["tests/test_orchestration.py::TestDriver::test_a_throttle_on_the_row_write_is_not_read_as_this_was_not_a_run"])

# 42. Match the rejection by message text instead of by botocore error code. Passes on
#     the real exception and ALSO on any unrelated error whose message contains the word
#     -- the same over-broad absorption as 41, arriving by a route that looks careful.
def m42(t):
    old = '    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")\n    return code == "ConditionalCheckFailedException"'
    assert old in t, "_is_condition_failure has moved; re-anchor this mutation"
    return t.replace(old, '    return "ConditionalCheckFailedException" in str(exc)', 1)
case("driver: the rejected condition is matched by message text, not error code",
     "orchestration/harness_driver/handler.py", m42,
     ["tests/test_orchestration.py::TestDriver::test_the_condition_is_matched_by_error_code_not_by_message_text"])

# 43. Suppress the whole escalation for a non-run instead of just the row write. A sweep
#     that cannot finish would then reach nobody -- fixing a phantom row by losing the
#     alert, which is the more expensive half of the pair.
def m43(t):
    old = "    run_row = _mark_run_escalated(c[\"ddb\"], run_id)"
    assert old in t, "the escalate row-write call site has moved; re-anchor this mutation"
    return t.replace(old, "    run_row = _mark_run_escalated(c[\"ddb\"], run_id)\n"
                          "    if not run_row:\n        return {\"escalated\": True}", 1)
case("driver: a non-run escalation is swallowed along with its row",
     "orchestration/harness_driver/handler.py", m43,
     ["tests/test_orchestration.py::TestDriver::test_an_escalation_by_something_that_is_not_a_run_mints_no_run_row"])

# 44. The real-run path stops closing out. The condition must gate CREATION only; a guard
#     that also blocks the legitimate update would leave every escalated run at 'running'.
def m44(t):
    old = 'ConditionExpression="attribute_exists(run_id)",'
    assert old in t, "the escalate row-write condition has moved; re-anchor this mutation"
    return t.replace(old, 'ConditionExpression="attribute_not_exists(run_id)",', 1)
case("driver: the condition is inverted, so a real run never reaches status=escalated",
     "orchestration/harness_driver/handler.py", m44,
     ["tests/test_orchestration.py::TestDriver::test_an_escalation_updates_the_run_row_of_a_real_run"])

# 45. The test DOUBLE goes back to dropping writes to an absent key. This is the co-defect
#     -- with this reverted the fix's own tests cannot fail, because the phantom write
#     evaporates in the fake. A double more forgiving than production hides exactly the
#     bugs production will have, so it needs a control of its own.
def m45(t):
    old = ("        if target is None:\n"
           "            # update_item is an UPSERT")
    assert old in t, "the fake's upsert branch has moved; re-anchor this mutation"
    return t.replace(old, "        if target is None:\n            return {}\n"
                          "        if False:\n            # update_item is an UPSERT", 1)
case("test double: the fake drops update_item writes to an absent key",
     "tests/test_orchestration.py", m45,
     ["tests/test_orchestration.py::TestDriver::test_the_fake_table_upserts_like_dynamodb_does"])

# 46. The fake stops resolving ExpressionAttributeNames, so `SET #s = :v` lands under a
#     literal "#s" and every read-back of a status it wrote is empty. That was latent here
#     until a test read one back.
def m46(t):
    old = "                    target[names.get(lhs, lhs)] = vals[rhs]"
    assert old in t, "the fake's name resolution has moved; re-anchor this mutation"
    return t.replace(old, "                    target[lhs] = vals[rhs]", 1)
case("test double: ExpressionAttributeNames are not resolved on a SET",
     "tests/test_orchestration.py", m46,
     ["tests/test_orchestration.py::TestDriver::test_an_escalation_updates_the_run_row_of_a_real_run"])

# 47. The escalation stops being recorded in the timeline -- the state handle_escalate was
#     actually in before this fix, which the runs.status write was standing in for. With the
#     row write now correctly declined for a non-run, this leaves a sweep's escalation with
#     no durable trace in EITHER table.
def m47(t):
    old = ('    try:\n'
           '        _record_stage_event(c["ddb"], run_id, event["stage"], "escalated",\n'
           '                            {"reason": args.get("reason", ""),\n'
           '                             "task": event.get("task", ""), "run_row": run_row})\n'
           '    except Exception as exc:  # noqa: BLE001 — never withhold an escalation for a log\n'
           '        print(f"[driver] could not record the escalation of {run_id}: {exc}")\n')
    assert old in t, "the escalate stage-event write has moved; re-anchor this mutation"
    return t.replace(old, "", 1)
case("driver: an escalation leaves no stage event, so the timeline never shows it",
     "orchestration/harness_driver/handler.py", m47,
     ["tests/test_orchestration.py::TestDriver::test_an_escalation_is_recorded_in_the_timeline_whichever_path_it_took"])

# 48. Bookkeeping is allowed to withhold the alert: the stage-event write is no longer
#     wrapped, so a failing events table takes the SNS page and the bus event down with it.
#     An escalation nobody hears is the failure this handler exists to prevent.
def m48(t):
    old = ('    except Exception as exc:  # noqa: BLE001 — never withhold an escalation for a log\n'
           '        print(f"[driver] could not record the escalation of {run_id}: {exc}")')
    assert old in t, "the escalate stage-event guard has moved; re-anchor this mutation"
    return t.replace(old, '    except Exception:\n        raise', 1)
case("driver: a failed timeline write takes the escalation alert down with it",
     "orchestration/harness_driver/handler.py", m48,
     ["tests/test_orchestration.py::TestDriver::test_a_failed_timeline_write_never_withholds_the_escalation"])

# ── the escalation channels must be independent ───────────────────────────────────────
#
# 49. SNS goes back to being unwrapped and first, which is where it was: a failed publish
#     then takes the bus event, the stage event and the token settle with it. When that was
#     found, SNS was also the channel with a KNOWN-ZERO audience (llmops-escalations had no
#     subscribers), which made the one channel reaching nobody the gate on the two that
#     worked. It has a confirmed subscriber now (measured 2026-08-10) and the mutation is
#     unchanged in force: the reason to wrap the publish is that a notification must not
#     withhold a state transition, and a live channel still fails on a throttle.
def m49(t):
    old = ('    try:\n'
           '        c["sns"].publish(TopicArn=os.environ["LLMOPS_SNS_TOPIC"],\n'
           '                         Subject=f"[llmops] escalation: {run_id}/{event[\'stage\']}",\n'
           '                         Message=json.dumps(args, indent=2, default=str))\n'
           '    except Exception as exc:  # noqa: BLE001 — one dead channel must not close the rest\n'
           '        print(f"[driver] SNS publish failed for the escalation of {run_id}: {exc}")\n')
    assert old in t, "the escalate SNS publish guard has moved; re-anchor this mutation"
    return t.replace(old,
                     '    c["sns"].publish(TopicArn=os.environ["LLMOPS_SNS_TOPIC"],\n'
                     '                     Subject=f"[llmops] escalation: {run_id}/{event[\'stage\']}",\n'
                     '                     Message=json.dumps(args, indent=2, default=str))\n', 1)
case("driver: a dead SNS topic takes the whole escalation down with it",
     "orchestration/harness_driver/handler.py", m49,
     ["tests/test_orchestration.py::TestDriver::test_a_dead_sns_topic_does_not_take_the_whole_escalation_with_it",
      "tests/test_orchestration.py::TestDriver::test_the_finops_audit_path_survives_a_dead_topic_too"])

# 50. The bus emit is unwrapped again, so a failed PutEvents skips the task-token settle and
#     parks a live token on a run that has already escalated -- freed only by the stage's own
#     timeout, hours later. #52's zombie, re-entered through the notification path.
def m50(t):
    old = ('    try:\n'
           '        ev.emit_event(os.environ["EVENT_BUS"], ev.ESCALATED_TO_HUMAN,\n')
    assert old in t, "the escalate bus-emit guard has moved; re-anchor this mutation"
    marker = '    except Exception as exc:  # noqa: BLE001 — see below: the token must still settle'
    assert marker in t, "the escalate bus-emit except clause has moved; re-anchor this mutation"
    head, _, tail = t.partition(marker)
    # drop the try/except wrapper: dedent the emit and delete the handler block
    head = head.replace(old, '    ev.emit_event(os.environ["EVENT_BUS"], ev.ESCALATED_TO_HUMAN,\n', 1)
    head = head.replace('                      {"run_id": run_id, "stage": event["stage"],\n'
                        '                       "reason": args.get("reason", "")}, client=c["events"])\n',
                        '                  {"run_id": run_id, "stage": event["stage"],\n'
                        '                   "reason": args.get("reason", "")}, client=c["events"])\n', 1)
    rest = tail.partition('    if event.get("task_token"):')[2]
    return head + '    if event.get("task_token"):' + rest
case("driver: a failed bus emit skips the task-token settle, parking it until the timeout",
     "orchestration/harness_driver/handler.py", m50,
     ["tests/test_orchestration.py::TestDriver::test_a_failed_bus_emit_still_settles_the_task_token"])


# ── the readiness checklist must be derived from the prompt, not restated ─────────────
#
# 51. The readiness panel drops readiness_report_uri again -- the pointer to the Data
#     Readiness Report, which is where the audit's PII scan lands. The panel then shows a
#     complete 8/8 while the customer has no link to the artifact that examined their data,
#     and "PII disposition: redacted" is a claim in the plan with nothing behind it.
def m51(t):
    old = ('    ("readiness_report_uri", "Data Readiness Report",\n'
           '     "the audit that actually examined the data, including its PII scan — without this "\n'
           '     "link the PII answer above is a claim in the plan with nothing behind it"),\n')
    assert old in t, "the readiness_report_uri row has moved; re-anchor this mutation"
    return t.replace(old, "", 1)
case("console: the readiness panel omits the report that carries the PII scan",
     "deploy/console/lambda_function.py", m51,
     ["tests/test_console_tasks.py::test_readiness_names_every_field_the_consult_protocol_asks_for"])

# 52. The derivation itself is replaced by a hand-copied set -- the shape the guard was in
#     when the omission got through. A restated checklist agrees with the console and with
#     itself, so it cannot detect the drift it exists to detect. This mutation is aimed at
#     _prompt_data_block_keys, which is what BOTH readiness guards read, so it also proves
#     control 51's catch depends on the derivation rather than on a coincidence.
def m52(t):
    old = "    m = re.search(r'a \"data\" block \\{(.*?)\\}; and for any', prompt)"
    assert old in t, "the data-block prompt parse has moved; re-anchor this mutation"
    return t.replace(
        old,
        "    m = None\n"
        "    if True:\n"
        "        return {'source_uri', 'verification_method', 'datasheet.license',\n"
        "                'datasheet.pii_disposition', 'datasheet.consent',\n"
        "                'customer_eval_uri', 'decontamination'}", 1)
case("test: the readiness guard restates the checklist instead of deriving it",
     "tests/test_console_tasks.py", m52,
     ["tests/test_console_tasks.py::test_the_readiness_guard_is_derived_from_the_prompt"])



# ── nothing scans the customer's data, and the deploy must say so ─────────────────────
#
# 53. The gap goes quiet: the no-job branch reports a bland status instead of naming it.
#     This is the exact prior state -- Macie ENABLED plus a COMPLETE job over 25 unrelated
#     buckets read as coverage everywhere anyone looked, and the deploy output is the only
#     place the absence is visible.
def m53(t):
    old = '        res["coverage"] = (\n            f"NO JOB SCANS {CUSTOMER_DATA_PREFIX}/ -- '
    assert old in t, "the no-coverage report has moved; re-anchor this mutation"
    return t.replace(
        old,
        '        res["coverage"] = (\n            f"no llmops job for {CUSTOMER_DATA_PREFIX}/ -- ', 1)
case("storage: the deploy stops naming the missing PII scan",
     "deploy/03_storage.py", m53,
     ["tests/test_console_tasks.py::test_the_deploy_reports_the_gap_loudly_when_nothing_scans",
      "tests/test_console_tasks.py::test_a_gap_is_reported_even_though_the_session_says_enabled"])

# 54. Coverage is decided by the bucket list alone, ignoring scoping. A job that names our
#     bucket but includes only runs/ then counts as scanning customer-data/ -- reported
#     coverage of a prefix nothing reads.
def m54(t):
    old = "    includes = ((defn.get(\"scoping\") or {}).get(\"includes\") or {}).get(\"and\") or []"
    assert old in t, "the includes-scoping read has moved; re-anchor this mutation"
    head, _, tail = t.partition(old)
    rest = tail.partition("    return True")[2]
    return head + "    return True" + rest

case("storage: coverage ignores scoping, so a runs/-only job counts as scanning customer data",
     "deploy/03_storage.py", m54,
     ["tests/test_console_tasks.py::test_naming_the_bucket_is_not_enough_if_the_prefix_is_scoped_out"])

# 55. A bucketCriteria job is treated as covering us rather than as undecidable. A tag-
#     matching job nobody has verified then silences the gap warning entirely.
def m55(t):
    old = "        return None  # cannot be decided from the definition alone"
    assert old in t, "the undecidable branch has moved; re-anchor this mutation"
    return t.replace(old, "        return True", 1)
case("storage: an unverified criteria job is counted as PII coverage",
     "deploy/03_storage.py", m55,
     ["tests/test_console_tasks.py::test_a_criteria_based_job_is_undecidable_not_covered_and_not_uncovered",
      "tests/test_console_tasks.py::test_an_undecidable_job_is_never_counted_as_coverage"])

# 56. The created job loses its prefix scoping, so it pays to read runs/, finops/ and
#     models-mirror/ as well -- our own artifacts, billed per GB, none of it customer data.
def m56(t):
    old = ('            "scoping": {"includes": {"and": [{"simpleScopeTerm": {\n'
           '                "comparator": "STARTS_WITH", "key": "OBJECT_KEY",\n'
           '                "values": [f"{CUSTOMER_DATA_PREFIX}/"]}}]}},\n')
    assert old in t, "the created job's scoping has moved; re-anchor this mutation"
    return t.replace(old, "", 1)
case("storage: the new scan job reads the whole bucket instead of customer-data/",
     "deploy/03_storage.py", m56,
     ["tests/test_console_tasks.py::test_the_created_job_is_scoped_to_customer_data_only"])

# 57. Idempotency-by-name is dropped, so every deploy with the flag creates ANOTHER paid
#     scheduled scanner (create_classification_job takes a clientToken; a fresh one does
#     not collide, it duplicates).
def m57(t):
    old = "    if ours:\n"
    assert old in t, "the existing-job branch has moved; re-anchor this mutation"
    return t.replace(old, "    if ours and False:\n", 1)
case("storage: a second Macie scanner is created on every deploy",
     "deploy/03_storage.py", m57,
     ["tests/test_console_tasks.py::test_the_job_is_idempotent_by_name_because_create_is_not",
      "tests/test_console_tasks.py::test_a_wrongly_scoped_job_of_ours_says_so_instead_of_claiming_an_update"])

# 58. The opt-in becomes opt-out: the deploy starts recurring billable classification work
#     without anyone asking for it.
def m58(t):
    old = "    if not enable:\n"
    assert old in t, "the enable gate has moved; re-anchor this mutation"
    return t.replace(old, "    if False:\n", 1)
case("storage: a plain deploy silently starts a billable recurring scan",
     "deploy/03_storage.py", m58,
     ["tests/test_console_tasks.py::test_nothing_is_created_without_the_flag_or_in_a_dry_run"])


# 59. The macie2 read grant is removed from the harness role -- the live pre-fix state
#     (simulate_principal_policy returned implicitDeny for every macie2 read). The scan then
#     runs, costs money per GB, and the audit that writes the report the readiness panel
#     links cannot see a single finding.
def m59(t):
    old = '          "macie2:ListFindings",\n          "macie2:GetFindings",\n'
    assert old in t, "the macie2 read grant has moved; re-anchor this mutation"
    return t.replace(old, "", 1)
case("iam: the audit agent loses its read access to Macie findings",
     "deploy/iam/harness_execution_role.json", m59,
     ["tests/test_console_tasks.py::test_the_audit_agent_can_actually_read_macie"])

# 60. The grant widens to let the agent start its own classification job -- billable work an
#     agent launches mid-turn, and the same statement shape would let it disable the session
#     it is judged by.
def m60(t):
    old = '          "macie2:GetMacieSession",\n'
    assert old in t, "the macie2 action list has moved; re-anchor this mutation"
    return t.replace(old, old + '          "macie2:CreateClassificationJob",\n', 1)
case("iam: the agent can start its own billable Macie scan",
     "deploy/iam/harness_execution_role.json", m60,
     ["tests/test_console_tasks.py::test_the_audit_agent_cannot_start_or_stop_a_scan"])

# 61. The audit prompt drops the "no job covers this data" sentence, so a run with zero
#     classification coverage produces a report whose PII line reads exactly like one backed
#     by a real scan. This is the trap the whole task exists to close.
def m61(t):
    old = ("If NO job covers the prefix, write exactly that: 'no Macie classification job "
           "covers this data; the PII finding below is a heuristic regex scan only'. ")
    assert old in t, "the no-coverage instruction has moved; re-anchor this mutation"
    return t.replace(old, "", 1)
case("prompt: the audit stops disclosing that nothing classified the data",
     "agents/data-prep/harness.json", m61,
     ["tests/test_console_tasks.py::test_the_audit_prompt_refuses_to_read_enabled_as_coverage"])

# 62. The orchestrator prompt goes back to naming two of its four mounted skills -- the
#     exact live state before this fix. The mount stays intact, so the older
#     "has the skill that knows how" guard still passes; only a guard derived from the
#     mount list can see it.
def m62(t):
    old = ("Your mounted skills (llm-agent-orchestration, ml-solution-design, "
           "llm-cost-optimization, llm-data-preparation) are your methodology")
    assert old in t, "the mounted-skills sentence has moved; re-anchor this mutation"
    return t.replace(old, "Your mounted skills (llm-agent-orchestration, "
                          "ml-solution-design) are your methodology", 1)
case("prompt: the orchestrator is told to consult 2 of its 4 mounted skills",
     "agents/orchestrator/harness.json", m62,
     ["tests/test_orchestration.py::TestConductorDispatch"
      "::test_every_mounted_skill_is_named_in_the_prompt_that_must_consult_it"])

# 63. A skill is named in the prompt but never mounted -- the mirror image of 62, and the
#     failure that ships an agent instructed to consult a file that does not exist.
def m63(t):
    old = '        "uri": "s3://<DATA_BUCKET>/skills/llmops/llm-data-preparation"\n'
    assert old in t, "the data-prep skill mount has moved; re-anchor this mutation"
    return t.replace(old, '        "uri": "s3://<DATA_BUCKET>/skills/llmops/llm-nonexistent"\n', 1)
case("prompt: the orchestrator is told to consult a skill nothing mounts",
     "agents/orchestrator/harness.json", m63,
     ["tests/test_orchestration.py::TestConductorDispatch"
      "::test_every_mounted_skill_is_named_in_the_prompt_that_must_consult_it"])

# 64. The message text is capped BEFORE the DynamoDB/S3 split -- the live state, in which
#     one assistant reply sat at exactly 8000 characters in both copies and the
#     "full-text audit copy" was a truncated copy of a truncated record.
def m64(t):
    old = "    trimmed = [{**m, \"text\": str(m.get(\"text\", \"\"))[:MSG_TEXT_MAX]} for m in msgs]"
    assert old in t, "the DDB trim has moved; re-anchor this mutation"
    return t.replace(old, "    trimmed = msgs\n"
                          "    msgs = [{**m, \"text\": str(m.get(\"text\", \"\"))[:MSG_TEXT_MAX]}\n"
                          "            for m in msgs]", 1)
case("tasks: the message is truncated before the audit copy is written",
     "deploy/console/lambda_function.py", m64,
     ["tests/test_console_tasks.py"
      "::test_the_audit_copy_keeps_the_full_text_the_ddb_record_has_to_cap"])

# 65. The audit copy goes back to read-modify-write of one transcript.jsonl, with the
#     read's failure treated as "no file yet" -- so a single transient S3 error replaces
#     the whole history with the newest lines.
def m65(t):
    old = '    key = f"tasks/{task_id}/transcript/{_now_iso()}-{secrets.token_hex(4)}.jsonl"'
    assert old in t, "the transcript key has moved; re-anchor this mutation"
    return t.replace(old,
                     '    key = f"tasks/{task_id}/transcript/one.jsonl"\n'
                     '    try:\n'
                     '        old_body = s3.get_object(Bucket=b, Key=key)["Body"].read()\n'
                     '    except Exception:\n'
                     '        old_body = b""', 1).replace(
        "    s3.put_object(Bucket=b, Key=key, Body=lines,",
        "    s3.put_object(Bucket=b, Key=key, Body=old_body + lines,", 1)
case("tasks: a failed transcript read is treated as 'no file yet' and erases history",
     "deploy/console/lambda_function.py", m65,
     ["tests/test_console_tasks.py"
      "::test_a_failed_read_can_never_erase_the_audit_log"])

# 66. The audit write goes back to being unwrapped, so one S3 failure skips the
#     _task_event and the _enqueue_task_turn that follow it -- stranding a KMS-signed
#     acceptance at 'accepting' with no worker, escapable only by the 20-minute hatch.
def m66(t):
    old = "    _safe_transcript_append(task_id, msgs)"
    assert old in t, "the wrapped audit call has moved; re-anchor this mutation"
    return t.replace(old, "    _transcript_append(task_id, msgs)", 1)
case("tasks: a failed audit write strands the signed acceptance it should not gate",
     "deploy/console/lambda_function.py", m66,
     ["tests/test_console_tasks.py"
      "::test_a_failed_audit_write_does_not_strand_a_signed_acceptance"])


def m67(t):
    """Restore the name collision the #33 merge produced.

    Both branches wrote a guard for "how many harness-task states does the happy path
    have", and both named the helper _happy_path_harness_states. git auto-merged the two
    bodies without complaint -- no conflict marker, no failing test -- and Python kept
    only the second, so the first test silently ran the WRONG derivation. A shadowed
    helper is the quietest defect a clean merge can introduce, and the two walks are
    independent (StartAt + stage/task vs PipelineModeChoice + harness_id), so the cross
    -check is worth a control: this mutation renames the second back onto the first.
    """
    old = "def _happy_path_harness_state_count() -> int:"
    assert old in t, "the renamed helper has moved; re-anchor this mutation"
    return (t.replace(old, "def _happy_path_harness_states() -> int:", 1)
             .replace("n = _happy_path_harness_state_count()",
                      "n = _happy_path_harness_states()", 1))
case("docs: the two happy-path derivations collide under one name and one shadows the other",
     "tests/test_docs_claims.py", m67,
     ["tests/test_docs_claims.py::test_the_documented_spine_matches_the_state_machine"])


def m68(t):
    """Put the falsified $18/day figure back into cost_model.py.

    Every prose mention of the Whisper orphan said $18/day, in six files, for as long as
    the finding existed. The number was the FIRST SWEEP'S GUESS -- that sweep could not
    call DescribeEndpoint and said so in its own report -- and it was half the truth:
    describe_endpoint_config returns ml.g5.2xlarge x1, and Cost Explorer billed $36.36 on
    seven consecutive days. A cost control that understates by 2x is one an owner can
    dismiss on the merits, so the arithmetic is now asserted against the hourly rate this
    module documents rather than restated as a string. This mutation restores the stale
    figure; the guard must notice.
    """
    old = "endpoint ($36.36/day) that have nothing to do"
    assert old in t, "the corrected daily figure has moved; re-anchor this mutation"
    return t.replace(old, "endpoint ($18/day) that have nothing to do", 1)


case("finops: the Whisper orphan's daily cost is restated from the sweep's guess, not derived",
     "pipeline/contracts/cost_model.py", m68,
     ["tests/test_cost_model.py::test_the_whisper_orphans_daily_figure_matches_its_instance_and_hourly_rate"])


def m69(t):
    """Delete the half of the budget-filter sentence that says which control DOES cover
    non-Bedrock spend.

    `bedrock-monthly-dev` is filtered to `Service: ["Amazon Bedrock"]`. Saying only that
    the filter protects our $1000 headroom is true and reassuring and leaves the reader
    believing the account has a spend guardrail over it. It does not: a budget scoped to
    one service is blind to waste in another, which is exactly why nothing at the account
    level ever flagged a $1106/month endpoint sitting InService for 843 days. The whole-
    account monitor sweep is what found it. Drop the second half and the paragraph
    misleads by omission -- the failure mode that is hardest to catch by reading, because
    every sentence left on the page is still true.
    """
    anchor = "- It ALSO meant"
    assert anchor in t, (
        "the second half of the budget-filter sentence has moved or been reworded; "
        "re-anchor this mutation rather than letting it crash -- a control that raises "
        "is a control that never tested its guard")
    start = t.index(anchor)
    end = t.index("\n\n", start)
    return t[:start] + t[end:]


case("finops: COST.md states the budget's Bedrock filter without saying what it is blind to",
     "docs/COST.md", m69,
     ["tests/test_cost_model.py::test_the_orphans_monthly_figure_is_derived_and_the_budget_filter_is_stated_both_ways"])


def m70(t):
    """Put the stale Lambda count back in the English README.

    The count guard existed and passed for 21 merged PRs while both READMEs said 5 and the
    deployer deployed 6, because the guard read PROJECT_STATE.md only. That is the shape of
    the drift: the one file a count guard watches is the one file that gets corrected, since
    it is the only file the failure names. This mutation restores the stale digit in a file
    the guard did not used to read.
    """
    old = "state machine + 7 Lambdas"
    assert old in t, "the README's Lambda line has moved; re-anchor this mutation"
    return t.replace(old, "state machine + 5 Lambdas", 1)


case("docs: the English README states 5 Lambdas again while the deployer deploys 7",
     "README.md", m70,
     ["tests/test_docs_claims.py::test_the_documented_state_and_lambda_counts_match_the_deployers"])


def m71(t):
    """Drop monitor-sweep from the README's Lambda list but leave the digit at 6.

    This is the more interesting half, and the half a digit-only guard cannot see: the
    stale README did not just miscount, it named four functions for a fleet of six. A
    reader who trusts the list goes looking for the sweep's code and concludes it does not
    exist. So the names beside the number are asserted too, and this mutation breaks only
    the names.
    """
    old = "(driver / start / resume / resurrector / webhook / finops / monitor-sweep)"
    assert old in t, "the README's Lambda name list has moved; re-anchor this mutation"
    return t.replace(old, "(driver / start / resume / resurrector / webhook / finops)", 1)


case("docs: the README's Lambda list omits monitor-sweep while the count still reads 7",
     "README.md", m71,
     ["tests/test_docs_claims.py::test_the_documented_state_and_lambda_counts_match_the_deployers"])


def m72(t):
    """Turn the negative-control claim back into an adjective with no number in it.

    "Every guard was mutation-checked" is the exact sentence that stood while nothing
    counted the controls: a control deleted, or a guard added with no control at all, left
    it reading true. An adjective cannot go stale, which is why it is worthless as evidence.
    This mutation removes the count and the guard must refuse the sentence that remains.
    """
    start = t.index("reverted one at a time and the test confirmed to fail")
    end = t.index("\n", start)
    line = t[start:end]
    assert "negative controls" in line, "the count has moved out of this sentence"
    return t[:start] + "reverted one at a time and the test confirmed to fail. A test that" + t[end:]


case("docs: TEST_RESULTS' mutation-check claim loses its count and becomes an adjective",
     "docs/TEST_RESULTS.md", m72,
     ["tests/test_docs_claims.py::test_the_documented_negative_control_count_matches_the_runner"])


def m73(t):
    """Delete the shell suite's row from the evidence file.

    This is not hypothetical: `test_capacity_race_guard.sh` ran in CI on every push for
    days while both TEST_RESULTS variants reported the pytest total alone, so the evidence
    understated what was verified. The pytest-derived count guard is structurally unable to
    notice -- a shell suite is not a pytest test -- so a second suite needs a second
    derivation, and this mutation removes what that derivation checks.
    """
    start = t.index("| Shell suite")
    end = t.index("\n", start) + 1
    return t[:start] + t[end:]


case("docs: the shell suite CI runs on every push vanishes from the evidence file",
     "docs/TEST_RESULTS.md", m73,
     ["tests/test_docs_claims.py::test_the_shell_suite_is_documented_with_its_assertion_count"])


def m74(t):
    """Leave VERSION at the release the CHANGELOG has moved past.

    This is what actually happened: the 1.1.0 entry was written, 21 PRs merged after it, and
    both VERSION and PROJECT_STATE's current phase went on naming 1.1.0 for two days. Nothing
    failed, because a version string is only checkable against another version string. The
    mutation reverts VERSION alone -- the CHANGELOG's newest entry and PROJECT_STATE stay at
    the new release -- so the guard has to catch a disagreement, not just an old number.
    """
    assert t.strip() != "1.1.0", "VERSION is already 1.1.0; this mutation would be a no-op"
    return "1.1.0\n"


case("release: VERSION names the release the CHANGELOG has already moved past",
     "VERSION", m74,
     ["tests/test_docs_claims.py::test_the_version_file_and_the_changelog_agree_on_the_current_release"])


def m75(t):
    """Put the falsified figure back in the zh-TW twin, in the zh unit spelling.

    This is a real escape, not a hypothetical. When m68's guard was written it checked two
    files by name -- docs/COST.md and CHANGELOG.md -- so the correction landed in English
    and `docs/COST.zh-TW.md` kept saying `$18/天` with the guard green for as long as it
    existed. Two independent evasions in one: a per-file allowlist is satisfied by the
    files it happens to name, and `$18/天` is not the string `$18/day`. The guard now walks
    every *.md in both unit spellings, which is the only shape that cannot be escaped by
    adding a file or translating a unit.
    """
    old = "Whisper endpoint（約 $36.36/天）"
    assert old in t, (
        "the corrected zh-TW daily figure has moved or been reworded; re-anchor this "
        "mutation rather than letting it no-op")
    return t.replace(old, "Whisper endpoint（約 $18/天）", 1)


case("finops: the falsified $18 figure survives in the zh-TW twin, in the zh unit",
     "docs/COST.zh-TW.md", m75,
     ["tests/test_cost_model.py::test_the_whisper_orphans_daily_figure_matches_its_instance_and_hourly_rate"])


def m76(t):
    """Restate the orphan's idle lifetime as one of the three wrong numbers it had.

    838 in the snapshot and the CHANGELOG, 842 in the IAM comment, 843 in fact. Three
    files, three digits, and nothing that could disagree with anything -- a standalone
    number has nothing to be checked against. The endpoint is deleted, so the interval is
    fixed forever: a wrong value here is permanent, not merely stale. The guard derives it
    from the snapshot's own creation and deletion dates.
    """
    old = "InService for 843 days"
    assert old in t, (
        "the CHANGELOG's derived day count has moved or been reworded; re-anchor this "
        "mutation rather than letting it no-op")
    return t.replace(old, "InService for 838 days", 1)


case("finops: the orphan's idle lifetime is restated as one of its three wrong values",
     "CHANGELOG.md", m76,
     ["tests/test_cost_model.py::test_the_orphans_idle_lifetime_is_derived_from_its_own_two_dates"])


def m77(t):
    """Lower the console's fallback limit and leave cost_model's alone.

    This is the exact shape of the defect the pairing test was written for: two copies of
    one number in two files, and until 2026-08-02 nothing compared them. The console's copy
    is the one that decides what an undeployed or env-var-less invocation enforces, so a
    silent disagreement means the tests all check $20,000 while the running code checks
    something else.
    """
    old = 'os.environ.get("APPROVAL_LIMIT_USD", "20000")'
    assert old in t, (
        "the console's fallback limit literal has moved or been reworded; re-anchor this "
        "mutation rather than letting it no-op")
    return t.replace(old, 'os.environ.get("APPROVAL_LIMIT_USD", "2000")', 1)


case("budget: the console's fallback limit disagrees with cost_model",
     "deploy/console/lambda_function.py", m77,
     ["tests/test_console_cost.py::test_the_consoles_fallback_limits_equal_the_canonical_ones"])


def m78(t):
    """Retype the limits in deploy.sh instead of reading them out of cost_model.

    The pre-2026-08-02 state was worse than this -- the script set NEITHER var, so the live
    function reported `APPROVAL_LIMIT_USD: null` and fell back to the console literal, which
    happened to agree. Nothing was wrong and nothing could have told us when it stopped
    agreeing. Hardcoding is that hazard with an extra copy: the deploy would keep shipping
    2000 while every test in the repo asserted 20000.
    """
    old = 'LIMITS=$("$PY_FOR_BUILD" -c "import sys; sys.path.insert(0,\'$BUILD\'); import cost_model as c; \\\n  print(f\'{c.DEFAULT_SINGLE_RUN_LIMIT_USD:.0f} {c.DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD:.0f}\')")'
    assert old in t, (
        "the deploy script's limit derivation has moved or been reworded; re-anchor this "
        "mutation rather than letting it no-op")
    return t.replace(old, 'LIMITS="2000 2000"', 1)


case("budget: deploy.sh hardcodes the limits instead of deriving them",
     "deploy/console/deploy.sh", m78,
     ["tests/test_console_cost.py::test_the_deploy_script_sets_both_limits_and_reads_them_from_cost_model"])


def m79(t):
    """Put the old $2,000 reference back in the module that owns the arithmetic.

    The number was raised by the platform owner's instruction, and the test carries it in
    its NAME as well as its body for this reason: a diff that edits the constant and the
    assert together still leaves a test called `..._are_the_20000_dollars_asked_for`
    checking something else, which does not read as fine to anyone reviewing it.
    """
    old = "DEFAULT_SINGLE_RUN_LIMIT_USD = 20_000.0"
    assert old in t, (
        "the canonical single-run reference has moved or been reworded; re-anchor this "
        "mutation rather than letting it no-op")
    return t.replace(old, "DEFAULT_SINGLE_RUN_LIMIT_USD = 2_000.0", 1)


case("budget: the canonical single-run reference silently reverts to $2,000",
     "pipeline/contracts/cost_model.py", m79,
     ["tests/test_cost_model.py::test_default_limits_are_the_20000_dollars_asked_for",
      "tests/test_console_cost.py::test_the_consoles_fallback_limits_equal_the_canonical_ones"])


def m80(t):
    """Freeze the straddling plan back to the literal 2,000,000 rows it used at $2,000.

    Not a hypothetical: this IS what raising the reference did, and it is the reason the
    plan is derived now. 2M rows price at $1,268 expected / $3,804 worst case -- both under
    $20,000 -- so every budget test downstream would stop engaging the budget check at all
    and pass while testing nothing. A fixture that no longer triggers the check it exists to
    trigger is the failure mode its own docstring names, so the straddle relation is
    asserted on every use rather than assumed.
    """
    old = 'return {"sample_count": int(0.5 * limit / per_sample), "max_iterations": 3}'
    assert old in t, (
        "the straddling plan's derivation has moved or been reworded; re-anchor this "
        "mutation rather than letting it no-op")
    return t.replace(old, 'return {"sample_count": 2_000_000, "max_iterations": 3}', 1)


case("budget: the straddling fixture stops straddling and tests nothing",
     "tests/test_console_cost.py", m80,
     ["tests/test_console_cost.py::test_the_budget_check_reads_worst_case_not_expected",
      "tests/test_console_cost.py::test_an_over_budget_launch_says_so_in_its_own_response",
      "tests/test_console_cost.py::test_lowering_the_limit_re_gates_an_already_clean_estimate"])


def m81(t):
    """Put the falsified $18/day back into the auditor's own system prompt.

    m68 covers `cost_model.py`; the sweep that corrected the figure edited three files and
    the guard was anchored to exactly those three, so the finops prompt kept stating the
    stale rate inside the very rule about not publishing assumed numbers as measured ones.
    A prompt is the worst place for it: the agent re-reads it on every invocation, and
    nothing in the repo compared it to the arithmetic. This mutation restores that state.
    """
    old = "carried a JumpStart Whisper endpoint at $36.36/day"
    assert old in t, "the corrected prompt sentence has moved; re-anchor this mutation"
    return t.replace(old, "carried a JumpStart Whisper endpoint at ~$18/day", 1)


case("finops: the auditor's prompt states the orphan rate the sweep guessed, not the one measured",
     "agents/finops/harness.json", m81,
     ["tests/test_cost_model.py::test_no_harness_prompt_states_the_falsified_orphan_rate"])


def m82(t):
    """Delete the measured rate from the prompt rather than falsifying it.

    An absence-only guard passes on an empty page. Removing the excluded-spend example
    leaves the attribute-by-resource rule abstract -- and that rule exists because a
    service-level rollup billed hundreds of dollars of somebody else's Canvas and Whisper
    spend to this project. The guard has to require the correction to be PRESENT, not
    merely require the wrong number to be gone.
    """
    old = " at $36.36/day -- ml.g5.2xlarge x1 at $1.515/hr x 24 h"
    assert old in t, "the measured-rate clause has moved; re-anchor this mutation"
    return t.replace(old, "", 1)


case("finops: the prompt drops the orphan's measured rate instead of correcting it",
     "agents/finops/harness.json", m82,
     ["tests/test_cost_model.py::test_no_harness_prompt_states_the_falsified_orphan_rate"])


def m83(t):
    """Take the signal handlers back off, leaving only the ``finally``.

    This is the state the runner shipped in, and it read as safe: the restore WAS in a
    ``finally``. SIGTERM's default disposition terminates without unwinding, so the mutation
    survived on disk -- ``m52``'s edit to `deploy/03_storage.py` was found afterwards by
    `git status`. Note this mutation edits the runner's own source: harmless, because the
    running process already holds its bytecode and the pytest subprocess reads from disk.
    """
    # Assembled from fragments rather than written whole: this case mutates the file it
    # lives IN, so a verbatim anchor would occur twice -- here and in the code it targets --
    # and `replace(..., 1)` would rewrite this literal while leaving the handlers installed.
    # It did exactly that, and the run scored the guard as having a hole it did not have.
    old = "for _sig in (signal.SIG" + "TERM, signal.SIGINT, signal.SIGHUP):"
    assert t.count(old) == 1, (
        f"expected exactly one handler-installation loop, found {t.count(old)}; a "
        "self-mutating case that matches its own anchor tests nothing")
    return t.replace(old, "for _sig in ():", 1)


case("controls: the runner drops its signal handlers and leaks the mutation on SIGTERM",
     "tests/negative_controls/monitor_dispatch.py", m83,
     ["tests/test_docs_claims.py::test_the_control_runner_restores_its_mutation_even_when_signalled"])


def m84(t):
    """Journal AFTER mutating instead of before.

    An ordering bug that no test of the happy path can see: both writes happen, the restore
    works, and every run passes. It only bites in the window between them -- crash there and
    the file is mutated with nothing on disk that knows what it held. The guard compares
    line numbers rather than trusting that the code reads in the order it executes.
    """
    journal = '    JOURNAL.write_text(json.dumps({"path": rel, "text": orig, "case": name}))\n'
    mutate_line = "    p.write_text(new)\n"
    assert journal + mutate_line in t, (
        "the journal/mutate ordering has moved; re-anchor this mutation")
    return t.replace(journal + mutate_line, mutate_line + journal, 1)


case("controls: the recovery journal is written after the mutation, not before",
     "tests/negative_controls/monitor_dispatch.py", m84,
     ["tests/test_docs_claims.py::test_the_control_runner_restores_its_mutation_even_when_signalled"])


def m85(t):
    """Keep the journal but never read it -- a record with no repair.

    The subtler half of the fix. Writing the pristine text is worthless on its own: SIGKILL
    cannot be handled, so the ONLY thing that undoes that leak is the next run reading the
    journal before it trusts the tree. Delete the call and the file still gets written, still
    gets cleaned up on the normal path, and still looks like a recovery mechanism.
    """
    # Anchored on the invocation, which now sits inside the `if __name__ == "__main__"` block
    # that keeps an import side-effect-free; the def and the reference inside this docstring
    # are quoted or at a different indent. Same self-mutation hazard as m83: an unanchored
    # "_restore_from_journal()" matches three places in this file. The column moved from 0 to
    # 4 when the mutating loop was guarded, which killed this anchor -- caught in one second
    # by test_every_negative_control_still_matches_the_code_it_mutates, which is the guard
    # that exists because four anchors had died this way unnoticed.
    old = "\n    _restore_from_journal()\n"
    assert t.count(old) == 1, (
        f"expected exactly one recovery call at the runner's top level, found {t.count(old)}; "
        "this case mutates its own file, so an anchor that matches itself would test nothing")
    return t.replace(old, "\n", 1)


case("controls: the runner journals the original but never restores from it",
     "tests/negative_controls/monitor_dispatch.py", m85,
     ["tests/test_docs_claims.py::test_the_control_runner_restores_its_mutation_even_when_signalled"])


def m86(t):
    """Give Teardown the same 24-hour ceiling the long-work states were raised to.

    The plausible mistake, and the reason the raise was scoped to six states rather than
    applied with sed: Teardown is what DELETES the endpoint. A wedged Teardown at 86400
    holds an ml.g5.2xlarge InService for a full day at $1.515/hr -- the shape of the
    843-day orphan this project already paid for once. Nothing about the state's own
    definition objects: 86400 is a legal TimeoutSeconds and the machine deploys fine.
    """
    old = '"ResultPath": "$.teardown",\n      "TimeoutSeconds": 3600,'
    assert t.count(old) == 1, f"Teardown's timeout has moved; found {t.count(old)}"
    return t.replace(old, '"ResultPath": "$.teardown",\n      "TimeoutSeconds": 86400,', 1)


case("ASL: Teardown inherits the 24-hour ceiling meant for long-work states",
     "orchestration/state_machine.asl.json", m86,
     ["tests/test_orchestration.py::TestStateMachine::test_a_stage_that_deletes_the_endpoint_keeps_a_short_timeout"])


def m87(t):
    """Leave DataPrepGenerate at the 7200 that cut the generation run off mid-work.

    The reverse direction, and the one a merge conflict resolves wrongly by default: an
    older branch's 7200 wins and every surface still reads healthy, because 7200 is a
    perfectly valid timeout. The only thing that says it is wrong is the owner's decision,
    so the guard has to hold that decision rather than trust the file.
    """
    old = '"ResultPath": "$.data_prep_generate",\n      "TimeoutSeconds": 86400,'
    assert t.count(old) == 1, f"DataPrepGenerate's timeout has moved; found {t.count(old)}"
    return t.replace(
        old, '"ResultPath": "$.data_prep_generate",\n      "TimeoutSeconds": 7200,', 1)


case("ASL: DataPrepGenerate reverts to the 7200s that cut a run off mid-work",
     "orchestration/state_machine.asl.json", m87,
     ["tests/test_orchestration.py::TestStateMachine::test_a_stage_that_deletes_the_endpoint_keeps_a_short_timeout"])


def m88(t):
    """Add a new timed state without classifying it into either timeout bucket.

    The hole a two-list guard has if it only checks the states it names: a state added
    later is in neither list, so neither loop examines it and the guard passes while an
    unreviewed ceiling ships. Modelled on the real hazard -- this clones MonitorReport
    under a new name with a 24-hour timeout, which is exactly how a cleanup-adjacent
    state would acquire one.
    """
    old = '    "MonitorReport": {'
    assert t.count(old) == 1, "MonitorReport's key has moved; re-anchor this mutation"
    clone = ('    "ArchiveArtifacts": {\n'
             '      "Type": "Task",\n'
             '      "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",\n'
             '      "Parameters": {\n'
             '        "FunctionName": "${HarnessDriverArn}",\n'
             '        "Payload": {\n'
             '          "run_id.$": "$.run_id",\n'
             '          "manifest_uri.$": "$.manifest_uri",\n'
             '          "stage": "monitor",\n'
             '          "task": "report",\n'
             '          "harness_id": "llmops_monitor",\n'
             '          "task_token.$": "$$.Task.Token",\n'
             '          "iteration.$": "$.iteration"\n'
             '        }\n'
             '      },\n'
             '      "ResultPath": "$.archive",\n'
             '      "TimeoutSeconds": 86400,\n'
             '      "Next": "Complete"\n'
             '    },\n')
    return t.replace(old, clone + old, 1)


case("ASL: a new timed state ships unclassified by the timeout policy",
     "orchestration/state_machine.asl.json", m88,
     ["tests/test_orchestration.py::TestStateMachine::test_a_stage_that_deletes_the_endpoint_keeps_a_short_timeout"])


def m89(t):
    """Put HeartbeatSeconds back on FinetuneLaunch with nothing sending heartbeats.

    The state this shipped in for weeks, and it read as MORE careful than a bare timeout:
    a liveness interval alongside a ceiling. Nothing calls SendTaskHeartbeat, so the first
    heartbeat never arrives and the state dies at 18000s while its TimeoutSeconds says
    86400 -- a deadline no surface reports, hidden behind a field whose name promises
    monitoring. The console even rendered it as a "heartbeat" row.
    """
    old = '"ResultPath": "$.training",\n      "TimeoutSeconds": 86400,'
    assert t.count(old) == 1, f"FinetuneLaunch's timeout has moved; found {t.count(old)}"
    return t.replace(
        old,
        '"ResultPath": "$.training",\n      "TimeoutSeconds": 86400,'
        '\n      "HeartbeatSeconds": 18000,', 1)


case("ASL: a heartbeat interval returns with nothing to send heartbeats",
     "orchestration/state_machine.asl.json", m89,
     ["tests/test_orchestration.py::TestStateMachine::test_a_heartbeat_interval_requires_something_to_send_heartbeats"])


def m90(t):
    """Go back to trusting update_state_machine's 200 -- the state the deployer shipped in.

    This is the whole defect, restored: the call succeeds, the deployer reports
    action="updated", and nobody ever compares the live definition to the ASL. It ran that
    way while the live machine was missing EvalGenerate for a day.
    """
    old = "    landed = confirm_state_machine_landed(sfn, sm_arn, asl, sleep=sleep)\n"
    assert t.count(old) == 1, f"the read-back call has moved; found {t.count(old)}"
    return t.replace(old, "    landed = {}\n", 1)


case("deploy: the ASL deploy stops reading the definition back",
     "deploy/07_lambdas.py", m90,
     ["tests/test_finops.py::test_the_asl_deploy_path_actually_calls_the_read_back"])


def m91(t):
    """Report clean when the definitions differ but the walk localises nothing.

    The tempting simplification -- if the per-state loop found nothing to say, there is
    nothing wrong. It turns the one case this function cannot explain into a pass, and it
    passes every test that feeds it a difference the walk CAN localise.
    """
    old = ('        drift.append({"problem": "definitions differ in a way this check '
           'cannot localise"})')
    assert t.count(old) == 1, f"the unexplained-difference branch has moved; {t.count(old)}"
    return t.replace(old, "        pass", 1)


case("deploy: an unexplained definition difference is reported as clean",
     "deploy/07_lambdas.py", m91,
     ["tests/test_finops.py::test_an_unexplained_difference_is_never_reported_as_clean"])


def m92(t):
    """Read the definition back exactly once, with no allowance for eventual consistency.

    The opposite failure, and the reason the retry loop exists: UpdateStateMachine is
    eventually consistent, so a single read fails on deploys that were fine. A check that
    cries wolf is a check that gets deleted -- and then the next undeployed state is
    invisible again.
    """
    old = "        if i < attempts - 1:\n            sleep(2 ** i)"
    assert t.count(old) == 1, f"the retry backoff has moved; found {t.count(old)}"
    return t.replace(old, "        if False:\n            sleep(2 ** i)", 1)


case("deploy: the definition read-back gives up before eventual consistency settles",
     "deploy/07_lambdas.py", m92,
     ["tests/test_finops.py::test_eventual_consistency_is_waited_out_rather_than_reported_as_drift"])


def m93(t):
    """Claim confirmed when the read-back could not run at all.

    No credentials, throttling, a deleted machine: all mean "unknown", and the one answer
    that must never come out is "confirmed". The dry-run path already follows this rule for
    an unreachable ASL validator; this is the same rule on the verify path.
    """
    old = ('            return {"definition_confirmed": False,\n'
           '                    "read_back_unreachable": f"{type(exc).__name__}: {exc}"}')
    assert t.count(old) == 1, f"the unreachable-read branch has moved; found {t.count(old)}"
    return t.replace(
        old,
        '            return {"definition_confirmed": True,\n'
        '                    "read_back_unreachable": f"{type(exc).__name__}: {exc}"}', 1)


case("deploy: an unreachable read-back is reported as a confirmed deploy",
     "deploy/07_lambdas.py", m93,
     ["tests/test_finops.py::test_a_read_back_that_cannot_run_says_so_instead_of_claiming_confirmed"])


def m94(t):
    """Stop reading the harness config back after the update (#81).

    The state this restores is the one that let the live llmops_finops prompt quote a
    falsified $18/day for two days while status read READY -- and llmops_data_prep sit 932
    characters behind main, with the Macie paragraph from #63 never deployed.
    """
    old = "    out.update(confirm_harness_landed(ctl, harness_id, sent))"
    assert t.count(old) == 1, f"the harness read-back call has moved; found {t.count(old)}"
    return t.replace(old, "    out.update({})", 1)


case("deploy: the harness deploy stops reading its config back",
     "deploy/05_harnesses.py", m94,
     ["tests/test_finops.py::test_the_harness_deploy_path_actually_calls_the_read_back"])


def m95(t):
    """Compare by equality instead of containment.

    This is the plausible-looking version of the check, and it is exactly why the guard
    needs a control: it reports drift on EVERY correct deploy, because the service adds
    agentRuntimeArn/Name/Id to the environment it returns. A check that fails a correct
    deploy is one somebody switches off, taking the real check with it.
    """
    old = """        if isinstance(want, dict) and isinstance(got, dict):
            inner = _dict_drift(want, got, f)
            if inner:
                drift.extend(inner)
                continue
            # Every key we sent matched; the difference is keys the service added.
            continue"""
    assert t.count(old) == 1, f"the containment branch has moved; found {t.count(old)}"
    return t.replace(old, "        pass", 1)


case("deploy: the config read-back demands equality the service never returns",
     "deploy/05_harnesses.py", m95,
     ["tests/test_finops.py::test_the_service_adding_its_own_keys_is_not_reported_as_drift"])


def m96(t):
    """Warm the harness before confirming what it serves.

    Six model turns spent making a stale prompt fast to reach, and a reassuring "warmed"
    line printed above the failure.
    """
    old = "    out.update(confirm_harness_landed(ctl, harness_id, sent))\n"
    assert t.count(old) == 1, f"the read-back call has moved; found {t.count(old)}"
    t = t.replace(old, "", 1)
    tail = '        out.update(warm(dat, h["arn"], harness_id))\n'
    assert t.count(tail) == 1, f"the warm call has moved; found {t.count(tail)}"
    return t.replace(tail, tail + old, 1)


case("deploy: the harness is warmed before its config is confirmed",
     "deploy/05_harnesses.py", m96,
     ["tests/test_finops.py::test_the_config_is_confirmed_before_any_turn_is_spent_warming_it"])


def m97(t):
    """Let the update payload name its own field list again.

    Two lists that agree today and drift later: a field added to the update call but not to
    the read-back can fail to land silently, which is this defect reintroduced one field at
    a time.
    """
    old = "if k in UPDATED_FIELDS}"
    assert t.count(old) == 1, f"the shared field list has moved; found {t.count(old)}"
    return t.replace(
        old,
        'if k in ("model", "systemPrompt", "tools", "skills", "allowedTools",\n'
        '                           "maxIterations", "maxTokens", "timeoutSeconds",\n'
        '                           "truncation", "environment", "environmentVariables")}', 1)


case("deploy: the update payload stops deriving from the checked field list",
     "deploy/05_harnesses.py", m97,
     ["tests/test_finops.py::test_the_read_back_checks_exactly_the_fields_the_update_sends"])


def m98(t):
    """Claim confirmed when the harness read-back could not run at all."""
    old = ('            return {"config_confirmed": False,\n'
           '                    "read_back_unreachable": f"{type(exc).__name__}: {exc}"}')
    assert t.count(old) == 1, f"the unreachable-read branch has moved; found {t.count(old)}"
    return t.replace(
        old,
        '            return {"config_confirmed": True,\n'
        '                    "read_back_unreachable": f"{type(exc).__name__}: {exc}"}', 1)


case("deploy: an unreachable harness read-back is reported as confirmed",
     "deploy/05_harnesses.py", m98,
     ["tests/test_finops.py::"
      "test_a_harness_read_back_that_cannot_run_says_so_instead_of_claiming_confirmed"])


def m99(t):
    """Stop reporting a name seen a second time.

    The detector goes silent while still returning a list, so both its claims read true on a
    clean tree. Its first version aimed this control at the repo-wide sweep, which passes
    whether the detection works or not -- the tree had no duplicates -- and the control went
    UNCAUGHT. That is the signal that the test named the wrong subject: the fix was to make
    the detection checkable on input that has the defect, not to weaken the mutation.
    """
    old = '                dupes.append(f"{node.name} (line {node.lineno})")'
    assert t.count(old) == 1, f"the duplicate report has moved; found {t.count(old)}"
    return t.replace(old, "                pass", 1)


case("docs: a shadowed duplicate test name is not reported",
     "tests/test_docs_claims.py", m99,
     ["tests/test_docs_claims.py::test_a_test_name_defined_twice_in_one_module_is_reported"])


def m100(t):
    """Ship the limits without the mode, exactly as they shipped until 2026-08-03.

    Two numbers labelled as limits and no word on whether either is enforced. The route
    keeps working, the page keeps rendering, and the reader concludes the platform stops an
    over-budget run -- which in the deployed advisory default it does not.
    """
    old = ('                       "budget_mode": BUDGET_MODE,\n'
           '                       "enforced": BUDGET_MODE == "blocking"}}')
    assert t.count(old) == 1, f"the limits payload has moved; found {t.count(old)}"
    return t.replace(old, '                       }}', 1)


case("console: the limits ship without saying whether they are enforced",
     "deploy/console/lambda_function.py", m100,
     ["tests/test_console_cost.py::test_the_limits_say_whether_they_are_enforced"])


def m101(t):
    """Hardcode enforcement to False -- the shape that keeps the advisory test green forever.

    This is the mutation the first version of this guard would NOT have caught: a test that
    only checks the advisory default is satisfied by a constant, and every blocking
    deployment then reports its real gate as advisory. Aimed at the blocking tests, because
    their subject is the flag TRACKING the mode rather than matching today's value.
    """
    old = '                       "enforced": BUDGET_MODE == "blocking"}}'
    assert t.count(old) == 1, f"the enforced flag has moved; found {t.count(old)}"
    return t.replace(old, '                       "enforced": False}}', 1)


case("console: enforcement is hardcoded rather than derived from the mode",
     "deploy/console/lambda_function.py", m101,
     ["tests/test_console_cost.py::"
      "test_the_limits_report_enforcement_when_the_gate_really_blocks",
      "tests/test_console_cost.py::test_enforced_is_true_and_the_run_really_is_held"])


def m102(t):
    """Put the numbers back on the card with the mode stripped off.

    The API stays honest and the page still reads as a stop sign -- which is the actual
    user-visible defect, and the reason the card is asserted separately from the payload.
    """
    old = 'const enf = lim.enforced === true;'
    assert t.count(old) == 1, f"the card's mode read has moved; found {t.count(old)}"
    t = t.replace(old, 'const enf = false;', 1)
    old2 = ('      +(enf ? "ENFORCED &mdash; an over-budget run is held for an approver"\n'
            '            : "ADVISORY &mdash; an over-budget run is reported, then launched '
            'anyway")+"</span>";')
    assert t.count(old2) == 1, f"the card's mode text has moved; found {t.count(old2)}"
    return t.replace(old2, '      +""+"</span>";', 1)


case("console: the cost card shows the limits with no word on enforcement",
     "deploy/console/frontend.html", m102,
     ["tests/test_console_cost.py::test_the_cost_card_renders_the_mode_next_to_the_numbers"])


def m103(t):
    """Strip the mode from the OVERVIEW limits only -- the state that actually shipped.

    m100 breaks the estimates payload, which the first three guards all read. This breaks
    the second payload and leaves the first intact, so it reproduces the live defect
    exactly: /api/cost-estimates answered with budget_mode while /api/cost-overview
    answered with two bare numbers, and every test named after the estimates endpoint
    stayed green through the deploy. It is aimed at the source-derived guard as well as
    the endpoint-named one, because the source-derived guard is the only one that would
    have caught this before it shipped.
    """
    old = ('        "limits": {"single_usd": APPROVAL_LIMIT_USD,\n'
           '                   "cumulative_usd": CUMULATIVE_LIMIT_USD,\n'
           '                   "budget_mode": BUDGET_MODE,\n'
           '                   "enforced": BUDGET_MODE == "blocking"},')
    assert t.count(old) == 1, f"the overview limits payload has moved; found {t.count(old)}"
    return t.replace(old, '        "limits": {"single_usd": APPROVAL_LIMIT_USD,\n'
                          '                   "cumulative_usd": CUMULATIVE_LIMIT_USD},', 1)


case("console: one limits payload states its mode and the other does not",
     "deploy/console/lambda_function.py", m103,
     ["tests/test_console_cost.py::"
      "test_the_overview_limits_also_say_whether_they_are_enforced",
      "tests/test_console_cost.py::test_no_limits_payload_anywhere_omits_the_mode"])


def m104(t):
    """Put the readiness docstring's count back to "six" -- the state that was on main.

    Not a hypothetical: DATA_READINESS_FIELDS grew to nine to match the consult prompt and
    this sentence stayed at six, and every readiness test passed either way because they
    all measure the tuple. The count in the tuple was guarded; the count in the prose was
    not, so the prose is what drifted.
    """
    old = "which of the nine data questions"
    assert t.count(old) == 1, f"the readiness count sentence has moved; found {t.count(old)}"
    return t.replace(old, "which of the six data questions", 1)


case("console: the readiness docstring miscounts the data questions",
     "deploy/console/lambda_function.py", m104,
     ["tests/test_console_tasks.py::"
      "test_the_readiness_docstring_states_the_real_number_of_questions"])


def m105(t):
    """Shrink the field tuple and leave the prose at nine -- drift from the other side.

    m104 breaks the sentence; this breaks the list. A guard that hardcoded "nine" instead
    of deriving it from DATA_READINESS_FIELDS would catch m104 and sail past this one,
    which is the difference between checking the claim and restating it.
    """
    old = ('    ("decontamination", "Decontamination",\n'
           '     "training on the held-out set inflates every score that follows"),\n')
    assert t.count(old) == 1, f"the decontamination field has moved; found {t.count(old)}"
    return t.replace(old, "", 1)


case("console: the readiness field list shrinks and the prose count does not follow",
     "deploy/console/lambda_function.py", m105,
     ["tests/test_console_tasks.py::"
      "test_the_readiness_docstring_states_the_real_number_of_questions"])


#: The fleet count a first-time reader meets on line 7 of both READMEs. Three cases, because
#: this claim can rot in three ways and only the first is the one people think of.
#:
#: The fourth way -- the FLEET grows while the prose stands still, which is how the Lambda
#: count, the ASL state count and CASE_STUDY's "six" all actually broke -- cannot be encoded
#: here: it needs a NEW agents/*/harness.json, and this runner mutates the text of one
#: existing file and journals that one path for recovery. Widening it to create files would
#: mean widening the journal's restore contract, which is not a change to make from inside a
#: docs PR. It was verified by hand instead: an 8th harness dropped into agents/ turned the
#: guard red with "README.md tells its first-time reader '7' agents; agents/ holds 8 harness
#: configs" -- the direction a guard hardcoding 7 would have sailed straight past.
#:
#: The node id is repeated verbatim in all three cases rather than hoisted into a shared
#: constant: test_every_negative_control_case_names_a_guard reads these registrations with
#: `ast`, so args[3] must be a list LITERAL -- a variable parses as ast.Name and the case
#: scores as naming no test at all. That guard caught this exact shortcut here.
def m106(t):
    """Drift the EN README's spelled-out count. The plain prose-rot direction."""
    old = "Seven AI agents — a conductor"
    assert t.count(old) == 1, f"the EN fleet sentence has moved; found {t.count(old)}"
    return t.replace(old, "Six AI agents — a conductor", 1)


case("readme: the count a first-time reader sees drifts from the fleet (EN)",
     "README.md", m106,
     ["tests/test_docs_claims.py::"
      "test_the_agent_count_readers_see_first_matches_the_fleet"])


def m107(t):
    """Drift the zh-TW twin. Both languages state the claim, so both must be guarded --
    a bilingual repo where only the English half is checked has an unguarded half."""
    old = "**7 個 agent 自己揣著 pager**"
    assert t.count(old) == 1, f"the zh-TW fleet phrase has moved; found {t.count(old)}"
    return t.replace(old, "**6 個 agent 自己揣著 pager**", 1)


case("readme: the count a first-time reader sees drifts from the fleet (zh-TW)",
     "README.zh-TW.md", m107,
     ["tests/test_docs_claims.py::"
      "test_the_agent_count_readers_see_first_matches_the_fleet"])


def m108(t):
    """Delete the paragraph that scopes CASE_STUDY's "six" to the v1 fleet.

    This is the case that keeps the carve-out from being a hole. The guard lets a document
    state a smaller PAST count -- but only where it says so. Strip the scoping paragraph and
    the bare "six agents" must go red, otherwise the exemption would silently bless any
    stale number that happens to sit in a file on the allowed list.
    """
    old = """**Six, throughout this document, is the v1 fleet.** The FinOps auditor
(`llmops_finops`) was added after Phase 6, making seven today — so the READMEs
say seven and this record says six, and each is right about its own moment.
Renumbering it would put this document in conflict with the evidence it cites
(`VERIFICATION_phase5.md`: "All six harnesses currently run Opus 5") and would
claim the auditor took part in a build it was not present for.

"""
    assert t.count(old) == 1, f"the v1-scope paragraph has moved; found {t.count(old)}"
    return t.replace(old, "", 1)


case("case study: the v1 scoping vanishes and a stale count is left bare",
     "docs/CASE_STUDY.md", m108,
     ["tests/test_docs_claims.py::"
      "test_the_agent_count_readers_see_first_matches_the_fleet"])


#: The Introduction tab. Four cases, one per failure DIRECTION, because every one of them
#: produces a page that loads -- which is why none of them would be found by looking.
def m109(t):
    """Make the audio route serve raw bytes instead of base64.

    The single most consequential line in the feature and the one with no visible symptom:
    API Gateway payload format 2.0 sends `body` as UTF-8 unless `isBase64Encoded` is set,
    so every clip arrives corrupted under a 200 status. The page's own degradation then
    hides it -- audio.onerror falls through to browser speech, so the narration still
    plays, in a robot voice, in all five languages, with nothing logged anywhere.
    """
    old = ('return {"statusCode": 200, "headers": headers,\n'
           '            "body": base64.b64encode(data).decode("ascii"), "isBase64Encoded": True}')
    assert t.count(old) == 1, f"the audio envelope has moved; found {t.count(old)}"
    return t.replace(old, 'return {"statusCode": 200, "headers": headers,\n'
                          '            "body": data.decode("latin-1")}', 1)


case("intro: the audio route drops isBase64Encoded and serves corrupted MP3s",
     "deploy/console/lambda_function.py", m109,
     ["tests/test_intro_bundle.py::test_every_clip_the_page_will_request_is_served"])


def m110(t):
    """Replace the allowlist membership test with a path-shaped check.

    This is the traversal the route is built to be immune to, written the way it is
    tempting to write it: validate the SHAPE of the segments, then join them. It looks
    careful. `%2F` is already decoded by API Gateway by the time the handler sees the
    path, so `en%2F..%2F..%2Flambda_function.py.mp3` arrives as real separators and the
    scene segment carries a `..` that this check has no opinion about.
    """
    old = "    if (lang, scene) not in INTRO_CLIPS:"
    assert t.count(old) == 1, f"the allowlist test has moved; found {t.count(old)}"
    return t.replace(old, "    if not lang or not scene:", 1)


case("intro: the audio route validates the path instead of allowlisting the clip",
     "deploy/console/lambda_function.py", m110,
     ["tests/test_intro_bundle.py::"
      "test_the_audio_route_cannot_be_walked_out_of_its_directory"])


def m111(t):
    """Drop the `csp_upload=False` on the intro page route.

    The direction that costs money rather than correctness: `_csp()` resolves the S3
    upload origin through `data_bucket()`, which does NOT cache a failed resolve -- so the
    default landing tab would hit Parameter Store on every request for an origin the page
    never fetches, and would break when SSM is throttled.
    """
    old = 'return _resp(200, INTRO_HTML, "text/html; charset=utf-8", csp_upload=False)'
    assert t.count(old) == 1, f"the intro page route has moved; found {t.count(old)}"
    return t.replace(old, 'return _resp(200, INTRO_HTML, "text/html; charset=utf-8")', 1)


case("intro: the default landing tab reaches for SSM on every request",
     "deploy/console/lambda_function.py", m111,
     ["tests/test_intro_bundle.py::test_the_intro_routes_touch_no_aws_service",
      "tests/test_intro_bundle.py::"
      "test_dropping_the_upload_origin_is_scoped_to_the_intro_routes"])


def m112(t):
    """Make `csp_upload=False` the default for every response.

    The opposite direction, and the one a fix-in-the-wrong-place produces: satisfying the
    intro's no-SSM requirement by changing the DEFAULT strips the S3 origin from
    `connect-src` on all 30 other routes, so the dataset upload is blocked by our own
    header. That failure reads as a broken S3 permission -- it cost hours the first time,
    which is why the guard pins both directions rather than just the intro's.
    """
    old = 'def _resp(code, body, ctype="application/json", cookies=None, csp_upload=True):'
    assert t.count(old) == 1, f"the _resp signature has moved; found {t.count(old)}"
    return t.replace(old, 'def _resp(code, body, ctype="application/json", '
                          'cookies=None, csp_upload=False):', 1)


case("intro: the CSP opt-out leaks to every other route and blocks the upload",
     "deploy/console/lambda_function.py", m112,
     ["tests/test_intro_bundle.py::"
      "test_dropping_the_upload_origin_is_scoped_to_the_intro_routes"])


def m113(t):
    """Delete deploy.sh's audio copy: the page ships, the narration does not.

    The bundle is where this feature is most likely to break, because nothing about the
    RUNNING system notices. The handler's cold-start walk finds no clips, `INTRO_CLIPS` is
    empty -- a state the code treats as legitimate, by design, so the tab loads, the
    scenes advance, and every language falls back to browser speech. The deploy log says
    nothing. A viewer is the detector.
    """
    old = 'cp -R "$SCRIPT_DIR/intro/audio/." "$BUILD/intro_audio/"\n'
    assert t.count(old) == 1, f"the audio copy line has moved; found {t.count(old)}"
    return t.replace(old, "", 1)


case("intro: deploy.sh stops bundling the narration and nothing errors",
     "deploy/console/deploy.sh", m113,
     ["tests/test_intro_bundle.py::test_deploy_sh_bundles_what_the_handler_reads"])


def m114(t):
    """Stop scanning binaries at all, instead of only dropping the entropy-prone rule.

    This is the mutation that matters most, because it is the shape a well-meaning
    "simplification" of the real fix takes. The commit hook was blocking on an MP3, the
    diagnosis was "binaries are not text", and the one-line version of that conclusion is an
    early `return []`. Every clip then scans clean, the hook goes green, the suite goes green
    -- and this repo's own account id could ship inside a bundled asset with nothing looking.

    The distinction the fix rests on: binaries skip ONLY the generic any-12-digits heuristic
    (measured 0 signal, 1 false hit across 11.4 MB) and keep every high-signal rule plus the
    literal account id.
    """
    old = "    findings = []\n    binary = is_binary(blob)\n"
    assert t.count(old) == 1, f"scan_blob's preamble has moved; found {t.count(old)}"
    return t.replace(old, "    findings = []\n    binary = is_binary(blob)\n"
                          "    if binary:\n        return findings\n", 1)


case("redaction: binaries are skipped entirely, not just the entropy rule",
     "tests/redaction_scan.py", m114,
     ["tests/test_redaction_scan.py::"
      "test_a_real_secret_is_caught_even_inside_a_binary"])


def m115(t):
    """Drop the own-account rule, keeping only the structural patterns.

    The subtle half of m114. `AKIA…` and `arn:aws:…` are structural and would still fire, so
    a reviewer skimming for "do the high-signal rules run on binaries?" sees yes. But the
    generic 12-digit rule is text-only, so the digest check is the ONLY thing catching this
    account's bare id in a binary -- delete it and the single most important string in the
    repo's threat model is unguarded in exactly the files nobody reads.

    Retargeted when REAL_ACCOUNT_IDS became REAL_ACCOUNT_DIGESTS: the loop this deletes used to
    iterate literal ids and now hashes candidates. A control whose anchor has moved does not
    fail quietly -- the `count(old) == 1` assertion below turns it into a loud error -- which is
    the reason the anchor is asserted rather than assumed.
    """
    old = "    own_account = []\n"
    assert t.count(old) == 1, f"the account-id loop has moved; found {t.count(old)}"
    i = t.index(old)
    end = t.index("    # The generic heuristic is text-only", i)
    return t[:i] + t[end:]


case("redaction: a bare real account id in a binary stops being a finding",
     "tests/redaction_scan.py", m115,
     ["tests/test_redaction_scan.py::"
      "test_a_real_secret_is_caught_even_inside_a_binary"])


def m116(t):
    """Apply the entropy-prone generic rule to binaries too -- the pre-fix behaviour.

    Restores the defect exactly: `if not binary` becomes unconditional, so 1 of 35 narration
    clips blocks the commit again. Worth a control of its own because the failure is
    intermittent by nature -- re-synthesise the audio and a different subset trips -- and an
    intermittent gate is the kind people route around with --no-verify, at which point it
    guards nothing.
    """
    old = "    if not binary:\n        for m in GENERIC_ACCOUNT_ID.finditer(blob):\n"
    assert t.count(old) == 1, f"the text-only guard has moved; found {t.count(old)}"
    return t.replace(old, "    if True:\n        for m in GENERIC_ACCOUNT_ID.finditer(blob):\n", 1)


case("redaction: compressed-audio entropy blocks the commit again",
     "tests/redaction_scan.py", m116,
     ["tests/test_redaction_scan.py::"
      "test_the_byte_run_that_blocked_the_commit_is_not_a_finding",
      "tests/test_redaction_scan.py::test_every_committed_narration_clip_scans_clean"])


def m117(t):
    """Put CI back on an extension allowlist, leaving 44 tracked files unscanned.

    The half of the defect that was never visible: an allowlist of `*.py *.json *.md *.yml
    *.yaml *.sh *.svg` opened 113 of 157 tracked files and never looked at frontend.html,
    page.template.html, test_intro_player.js or any extensionless file. Those are TEXT -- a
    real account id in one of them passes CI green. The mutation reverts the workflow to a
    self-contained grep so the two scanners can drift apart again.
    """
    old = "        run: python3 tests/redaction_scan.py --tracked\n"
    assert t.count(old) == 1, f"the CI scan step has moved; found {t.count(old)}"
    return t.replace(old, "        run: grep -rn AKIA --include='*.py' . || true\n", 1)


case("redaction: CI drifts back to its own rule list and its own blind spot",
     ".github/workflows/redaction-check.yml", m117,
     ["tests/test_redaction_scan.py::test_both_callers_fail_on_the_scanner_being_absent",
      "tests/test_redaction_scan.py::test_neither_caller_kept_its_own_copy_of_the_rules"])


def m118(t):
    """"Simplify" the allowlist back into bare 12-digit literals.

    These three accounts are published by AWS, so the instinct is that spelling them out is
    harmless -- and for THIS repo's scanner it is, since redaction_scan.py is in
    SELF_REFERENTIAL. The cost lands somewhere else entirely: a session-level pre-PR hook
    scans the branch diff with its own pattern list and no such exemption, and it blocked the
    PR that introduced this module on exactly these bytes. Every further scanner that ever
    reads this file would need the same per-file exemption taught to it, one at a time -- which
    is the drift the module exists to end.

    So "no credential-shaped literal in either file" has to be enforced, not left as a
    convention the next editor has no way to see.

    The mutation is derived from `rs.ALLOWED` rather than typed out, for two reasons. This file
    obeys the same rule -- a quoted tuple here would make the control's own module a finding,
    which the gate caught on the first draft, and a guard that cannot be committed guards
    nothing. It also means the mutation cannot go stale: add a fourth allowlisted account and
    this still respells all of them.
    """
    spec = importlib.util.spec_from_file_location(
        "redaction_scan", REPO / "tests" / "redaction_scan.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    literal = ", ".join(f'b"{a.decode()}"' for a in rs.ALLOWED)
    old = 'ALLOWED = (b"6833" + b"13688378", b"7631" + b"04351884", b"1234" + b"56789012")'
    assert t.count(old) == 1, f"the ALLOWED tuple has moved; found {t.count(old)}"
    return t.replace(old, f"ALLOWED = ({literal})", 1)


case("redaction: the allowlist is respelled as bare 12-digit literals",
     "tests/redaction_scan.py", m118,
     ["tests/test_redaction_scan.py"
      "::test_no_credential_shaped_literal_survives_in_either_file"])


def m119(t):
    """Quote the blocking byte run as a literal instead of rebuilding it from bytes([...]).

    The other direction of the same property, in the test file rather than the scanner. This
    one matters because the literal is genuinely tempting: quoting the twelve digits directly
    reads far better than a `bytes([0x33] * 10 + ...)` constant, and the value is not a secret
    at all -- it is a coincidental run of digits inside an MPEG frame.

    It still must not be written down. `BLOCKING_RUN` is asserted to be the identical twelve
    bytes, so nothing is lost by constructing it, and the file stops needing an exemption from
    every scanner that reads it.

    Note that THIS file obeys the same rule, which is why the replacement text below is built
    rather than quoted. The mutation is byte-for-byte what it would be if spelled out; writing
    it as a literal would have made this control's own module a finding, and the gate caught
    exactly that on the first draft. A guard that cannot be committed guards nothing.
    """
    run = bytes([0x33] * 10 + [0x31, 0x39]).decode()
    old = "BLOCKING_RUN = bytes([0x33] * 10 + [0x31, 0x39])"
    assert t.count(old) == 1, f"BLOCKING_RUN has moved; found {t.count(old)}"
    return t.replace(old, f'BLOCKING_RUN = b"{run}"', 1)


case("redaction: the blocking byte run is quoted instead of reconstructed",
     "tests/test_redaction_scan.py", m119,
     ["tests/test_redaction_scan.py"
      "::test_no_credential_shaped_literal_survives_in_either_file"])


#: The README's walkthrough player. The film is no longer committed to this repo -- it is served
#: by the `user-attachments` upload GitHub renders as a real <video> element -- so what these
#: cases break is the PRESENTATION, and that is now the whole of the promise: if the URL stops
#: being promoted to a player, the walkthrough is unreachable from the repo page and there is no
#: second path behind it.
#:
#: A block of history used to sit here listing nine deliberately-broken mp4s (4 KB truncation,
#: `-an` at full length, a 3-second audio track, scale=640:360, setsar=59/32, no +faststart,
#: -t 240) built with ffmpeg and driven past the container reader BY HAND -- by hand because this
#: runner mutates the text of one tracked file and journals that path for recovery, so it cannot
#: swap a 10 MB binary. Those directions are gone with the artifact: there is no committed mp4
#: left to damage. Two of the four defects they found are kept, because they are about how a
#: guard is written rather than about mp4s:
#:   - the audio-stream and frame-size assertions sat behind skipif(ffprobe), and CI has no
#:     ffmpeg, so a FULL-LENGTH SILENT film passed the whole module (7 passed, 3 skipped) on the
#:     one machine that gates merges. A guard that can only fail on a laptop gates nothing.
#:   - presence anywhere in a section is not a claim. m131's ancestor PASSED its guard because a
#:     paragraph ABOVE the link happened to mention the same number, and the sentence ABOUT a
#:     number is not the sentence a reader acts on. Hence the section slice, and hence the anchor.
#:
#: One direction here CANNOT be driven by this runner, and was driven by hand instead:
#: test_no_video_file_is_committed_to_this_repo reads `git ls-files`, so no edit to the text of an
#: already-tracked file can trip it -- this runner cannot add an index entry. Verified manually:
#: `git add`ing an empty docs/media/intro-en.mp4 turned it red on its named assertion
#: ("video file(s) committed to the repo: ['docs/media/intro-en.mp4']"), and `git rm --cached`
#: restored it. Written down because a reader counting the cases here would otherwise conclude
#: that guard has never been seen failing.
#:
#: And one real loss, named so nobody has to rediscover it: m123's direction used to be driven
#: from `.stage` in page.template.html against the recording's CODED frame size, read out of the
#: mp4's sample entry. With no recording it now compares page.template.html against
#: record_video.py's own STAGE_W/STAGE_H copy -- two places the number is WRITTEN, not a place it
#: was measured. Changing both together passes, and no test can see it.
def m120(t):
    """Give the EN player URL a caption line, so it stops being alone in its paragraph.

    Retargeted: this control used to downgrade a poster image to a text link, and both the poster
    and the mp4 it pointed at are deleted. The rule that replaced them is that the URL must stand
    ALONE in its paragraph -- and this is the way that gets broken, because a bare unexplained URL
    reads like an accident and captioning it looks like tidying up. GitHub then renders the
    caption and the URL as one paragraph of text: a live link, no player, nothing 404ing, and
    every other guard green.

    Drives the `before.endswith("\\n\\n")` assertion specifically, which is why the caption goes
    on the line ABOVE rather than in front of the URL on the same line: prefixing the same line
    would stop `^…$` matching at all and the guard would die on its FIRST assertion instead,
    leaving the paragraph rule unproven. That is the exact hole the last pass left -- two controls
    both dying on the same early assertion -- so the two halves of the paragraph rule are now
    split across m120 (text before, EN) and m121 (text after, zh-TW).
    """
    old = "\nhttps://github.com/user-attachments/assets/"
    assert t.count(old) == 1, f"the EN player URL has moved; found {t.count(old)}"
    i = t.index(old) + 1
    return t[:i] + "Five minutes, narrated:\n" + t[i:]


case("readme: the EN player URL is captioned, so it is no longer alone in its paragraph",
     "README.md", m120,
     ["tests/test_intro_video.py::test_both_readmes_carry_the_inline_player_url"])


def m121(t):
    """Put a note under the zh-TW player, in the same paragraph as the URL.

    The other half of the paragraph rule -- text AFTER the URL with no blank line between -- and
    on the zh-TW side because the guard reads both files and the two are not the same file. A note
    directly under the player is the likeliest thing anyone adds ("執行時間 5:04"), it looks like
    it belongs to the video, and markdown makes it one paragraph with the URL. The player stops
    rendering and the page still looks finished.

    Retargeted from deleting the README's path to the committed mp4, which no longer exists. What
    the old version pinned is worth keeping written down, because the guard it tested is gone:
    that assertion was first `"/intro" in text`, which the video's own path
    `docs/media/intro-en.mp4` satisfied, so the mutation PASSED; it was later satisfied by a
    second, unrelated mention of the same directory (`record_video.py`) and went from caught to
    UNCAUGHT, which is how that defect was found. Substring-anywhere is not a claim.
    """
    old = "\nhttps://github.com/user-attachments/assets/"
    assert t.count(old) == 1, f"the zh-TW player URL has moved; found {t.count(old)}"
    end = t.index("\n", t.index(old) + 1)
    return t[:end] + "\n執行時間 5:04。" + t[end:]


case("readme: a note is added under the zh-TW player, inside the URL's own paragraph",
     "README.zh-TW.md", m121,
     ["tests/test_intro_video.py::test_both_readmes_carry_the_inline_player_url"])


def m121b(t):
    """Write a <video> tag into the EN README.

    The direction that will actually happen, and it is likelier now than when this control was
    written: the section is a heading and a bare URL with no prose left to explain why it is not
    a tag, so anyone who wants "a real player" reaches for the obvious markup and points it at
    the URL that is already sitting there. GitHub's sanitizer deletes the element, so it renders
    as an empty gap. Measured against GitHub's own POST /markdown, not assumed -- six embed
    forms, all erased.

    Retargeted onto the heading, and pointed at the guard that outlived the split: the <video>
    assertion used to ride inside the test that also required a committed mp4, so it died with
    that premise. It is `test_neither_readme_hand_writes_a_video_tag` now, parametrized over both
    READMEs, and unaffected by where the bytes live.

    Deliberately leaves the bare URL in place below the tag: the mutation must fail on the
    video-tag guard, not incidentally break the player guard as well and make it ambiguous which
    assertion caught it.
    """
    old = "## Watch it — five minutes, narrated\n"
    assert t.count(old) == 1, f"the EN walkthrough heading has moved; found {t.count(old)}"
    tag = ('\n<video src="https://github.com/user-attachments/assets/'
           'f189afb1-c326-49b6-b023-785da5ed3e6a" controls></video>\n')
    return t.replace(old, old + tag, 1)


case("readme: a <video> tag GitHub silently deletes is written into the EN README",
     "README.md", m121b,
     ["tests/test_intro_video.py::test_neither_readme_hand_writes_a_video_tag"])


def m122(t):
    """Reintroduce a budget figure beside the player, in Chinese.

    The reporting reference is whatever each team sets, so no amount belongs in material that
    describes the product. The walkthrough section is the likeliest place for one to come back:
    it is the part that summarises what the video shows, and "it flags runs over $X" reads like
    a helpful specific rather than like this platform's own test setting.

    zh-TW rather than EN on purpose -- a `$`-shaped pattern would miss 兩萬 entirely, so the
    half of the guard that has to understand Chinese numerals is the half worth breaking.

    Retargeted: the anchor was the section's scene summary ("沒人看著的閒置 endpoint"), which was
    cut when the section was trimmed to a player and a poster link. It now mutates the heading,
    which is the shortest-lived thing that is still guaranteed to be inside the section -- and a
    caption bolted onto the heading is a realistic place for a figure to reappear now that there
    is no prose left to put one in.
    """
    old = "## 看五分鐘導覽（有旁白）"
    assert t.count(old) == 1, f"the zh-TW walkthrough heading has moved; found {t.count(old)}"
    return t.replace(old, old + "：包含超過兩萬美元基準的 run 如何被指名", 1)


case("readme: a budget amount reappears next to the player (zh-TW)",
     "README.zh-TW.md", m122,
     ["tests/test_intro_video.py::test_the_video_section_names_no_budget_amount"])


def m123(t):
    """Re-author the stage larger and leave the recorder pointed at the old size.

    A stage the recorder does not match means fit() scales it, and every diagram in the next
    recording ships resampled. Nobody notices that by watching the film once, which is why it was
    measured rather than eyeballed.

    Retargeted, and the retarget is a genuine weakening worth stating rather than glossing. The
    guard this used to trip compared `.stage` against the CODED frame size read out of the
    committed mp4's sample entry -- a real measurement of a real artifact. With the mp4 deleted,
    the surviving guard compares `.stage` against record_video.py's own STAGE_W/STAGE_H copy: two
    places the number is WRITTEN. This mutation still fires, because it changes one of the two.
    What no longer fires -- and no test can see -- is someone changing BOTH together while a
    recording made at the old size is what people watch.
    """
    old = "width:1180px; height:664px"
    assert t.count(old) == 1, f"the .stage size declaration has moved; found {t.count(old)}"
    return t.replace(old, "width:1280px; height:720px", 1)


case("intro: the stage is re-authored larger and the recorder is not updated",
     "deploy/console/intro/page.template.html", m123,
     ["tests/test_intro_video.py"
      "::test_the_recorder_records_at_the_size_the_page_is_authored_at"])


def m124(t):
    """Widen the API-Gateway-hostname excuse from whole-id equality back to a substring search.

    This is the direction the rule was FIRST written in, and it is the reason the rule needed a
    control rather than a reading. With `any(e in match ...)`, a hostname whose id merely
    *contains* an example word walks straight through -- so any real id containing `apiid` is
    excused. The excuse list exists so a legitimate sample origin is not a finding; a substring
    version of it silences the rule it is attached to.

    This control earned its keep on its first run: it PASSED -- i.e. it did NOT trip its guard.
    The guard's sneaky hostname was a hand-spelled `exampleandthenrealbits99`, which contains no
    entry of the excuse tuple at all, so the substring version excused nothing and the assertion
    held against the very defect it named. The guard now builds the id from
    `rs.EXAMPLE_API_IDS[0]`. A control that passes is not a formality: it is the only reason that
    weakness was found.

    The mutation is on the scanner, and the guard it must trip lives in test_redaction_scan.py.
    Note it does NOT quote a real hostname: this file is scanned by the same gate.
    """
    old = "        if m.group(1).lower() in EXAMPLE_API_IDS:"
    assert t.count(old) == 1, f"the hostname excuse check has moved; found {t.count(old)}"
    return t.replace(old, "        if any(e in m.group(0).lower() for e in EXAMPLE_API_IDS):", 1)


case("redaction: the live-hostname excuse is loosened to a substring match",
     "tests/redaction_scan.py", m124,
     ["tests/test_redaction_scan.py::test_the_examples_do_not_excuse_the_real_shape"])


def m125(t):
    """Delete the live-endpoint rule from the scanner altogether.

    The direction that actually happened: the rule did not exist, so this repo shipped the
    address of its own admin console in a rendered README, in both ARCHITECTURE files and in two
    test files, and every gate stayed green through the merge. A control that only checks the
    excuse logic would not have caught its total absence.
    """
    old = "    for m in LIVE_ENDPOINT.finditer(blob):"
    assert t.count(old) == 1, f"the hostname scan loop has moved; found {t.count(old)}"
    return t.replace(old, "    for m in ():", 1)


case("redaction: the live API Gateway hostname rule is removed entirely",
     "tests/redaction_scan.py", m125,
     ["tests/test_redaction_scan.py::test_a_live_api_gateway_hostname_is_a_finding"])


def m126(t):
    """Narrow the binary-classification guard's diff base from the empty tree back to HEAD.

    The direction that actually happened, and it was found in a CI log rather than by reading:
    `git diff --cached --numstat` with no base lists only what differs from HEAD, so on a clean
    checkout it returns nothing and the guard did `pytest.skip("nothing staged")`. CI *is* a
    clean checkout -- the run for the previous commit printed `910 passed, 4 skipped`, one more
    skip than the three ffprobe cross-checks, and this was the fourth. A guard named "for every
    tracked file" was checking ZERO of them on the only machine that gates the merge.

    The mutation restores the narrow base. It does not need to fabricate an empty index: run from
    a dirty worktree the diff still lists something, which is why the guard now also asserts it
    covered `len(git ls-files)` files rather than merely "at least one". That count assertion is
    what this control trips.
    """
    old = 'subprocess.run(["git", "diff", "--cached", "--numstat", _EMPTY_TREE],'
    assert t.count(old) == 1, f"the binary-classification diff has moved; found {t.count(old)}"
    return t.replace(old, 'subprocess.run(["git", "diff", "--cached", "--numstat"],', 1)


case("redaction: the binary-classification guard only sees the current change, not every file",
     "tests/test_redaction_scan.py", m126,
     ["tests/test_redaction_scan.py"
      "::test_binary_classification_matches_git_for_every_tracked_file"])


def m127(t):
    """Put the account id back into the scanner as two adjacent halves.

    THE defect this whole change exists to close, restored in the exact form it shipped in. The
    theory behind it was "a value no scanner's regex matches is a value the repo does not
    contain", and it was wrong in the only way that matters: the halves sit next to each other,
    in source order, in a file GitHub renders. A reader recombines them by eye in about a second.
    Every automated scanner missed it -- including this repo's own, which reported its own source
    CLEAN -- so the splitting hid the id from the machines that look for it and from no human at
    all. GitHub secret scanning never flagged it either, because an account id is not a
    credential and has no detector.

    The mutation cannot spell the real id -- and it must not try to LOOK IT UP either. The first
    two versions of this control tried to recover it from git history (`git log -S 'arn:aws:iam::'`,
    then join literals and ask the digest), and that approach has two independent ways to die
    quietly, one of which it hit:

      * `actions/checkout@v4` clones at depth 1. CI sees ONE commit, the `-S` walk finds nothing,
        and the control raises "could not reconstruct" -- red on every PR for a reason that has
        nothing to do with the guard it protects. Measured: a `--depth 1` clone of this branch
        shows 1 commit against the full history's many.
      * even at full depth it is archaeology, not a guard. The id was never in history as twelve
        consecutive digits, only ever as two halves -- the defect restating itself -- and the day
        the id is scrubbed from history for real, this control's own assert says to delete it.
        A check whose subject can legitimately disappear cannot be the check.

    So the mutation manufactures the situation instead of excavating it: it adds a fabricated
    id's digest to `REAL_ACCOUNT_DIGESTS` -- making that id genuinely WATCHED, by the only
    definition the scanner has -- and writes the same id into the file as two adjacent literals.
    The property under test is unchanged and needs no history at all: "no split that reconstructs
    a WATCHED id survives". Raw, the file holds `5555` and `55555555` and no twelve-digit run, so
    the guard's first pass sees nothing; only after `_with_literals_joined` collapses them does
    the digest match. That is precisely the step the fixed guard added, and the reason the
    reconstruction has to be a REAL match: a guard that merely counted split literals would flag
    half the scanner, since ALLOWED is split on purpose and legitimately so -- those three ids are
    published by AWS.

    The real account id is therefore never needed, never spelled and never looked up, which is
    also the property the scanner itself is built around.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO / "tests"))
    import redaction_scan as _rs

    # Split here too, and not for symmetry: spelled whole, these twelve digits would be a
    # 12-digit run in a TRACKED file, which moves the "N such runs, M distinct" counts that
    # redaction_scan.py derives -- and specifically breaks its stated invariant that the
    # DISTINCT count never moves because every run in a test is the same published
    # placeholder. A control is not allowed to change the measurement it sits beside.
    fabricated = b"5555" + b"55555555"
    assert fabricated.decode() not in t, (
        "the fabricated id this control plants is already spelled in the file, so the mutation "
        "would not be a change -- pick another twelve digits")
    digest = _rs.account_digest(fabricated)

    anchor = "REAL_ACCOUNT_DIGESTS = (\n"
    assert t.count(anchor) == 1, f"the digest tuple has moved; found {t.count(anchor)}"
    split = f'\n_split_id = b"{fabricated[:4].decode()}" + b"{fabricated[4:].decode()}"\n'
    return t.replace(anchor, f'{split}{anchor}    "{digest}",\n', 1)


case("redaction: this account's id goes back into the scanner as two adjacent halves",
     "tests/redaction_scan.py", m127,
     ["tests/test_redaction_scan.py"
      "::test_the_real_account_id_is_never_recoverable_from_either_file"])


def m128(t):
    """Replace the iterated KDF with a bare sha256 of the same input.

    The plausible "simplification", and the one nobody would question in review: the digests
    still look like digests, every scan still produces identical findings, and the whole suite
    stays green. What changes is only that the stored digest becomes a lookup. Twelve digits is
    ~40 bits; measured on this laptop single-threaded CPython does 3.1M sha256/s, so the entire
    1e12 space falls in about four days here and roughly 100 seconds on a GPU. At that point
    publishing the digest publishes the id -- i.e. the fix would have moved the exposure rather
    than closed it, while reading as strictly more careful than before.

    Tripped by the round-count assertion, which exists precisely because this property is
    invisible in a passing scan.
    """
    old = 'return hashlib.pbkdf2_hmac("sha256", candidate, _KDF_SALT, _KDF_ROUNDS).hex()'
    assert t.count(old) == 1, f"account_digest has moved; found {t.count(old)}"
    return t.replace(old, "return hashlib.sha256(_KDF_SALT + candidate).hexdigest()", 1)


case("redaction: the account-id KDF is 'simplified' to a bare sha256 a GPU sweeps in 100 seconds",
     "tests/redaction_scan.py", m128,
     ["tests/test_redaction_scan.py"
      "::test_the_watched_account_is_stored_as_an_iterated_digest"])


def m129(t):
    """Turn the bare player URL into a tidy markdown link.

    The direction that will actually happen, and the one that looks like an improvement: a bare
    URL on its own line reads like an accident, so someone wraps it as `[Watch the walkthrough]
    (…)`. Measured through GitHub's own POST /markdown mode=gfm: the bare form renders
    <details open> + <video controls>, and this repo does not get to assume the wrapped form
    does too. If GitHub stops promoting it, the page silently loses its only player while every
    link still resolves and every other guard stays green -- which is the state both READMEs
    were in before this URL existed.
    """
    old = "\nhttps://github.com/user-attachments/assets/"
    assert t.count(old) == 1, f"the EN player URL has moved; found {t.count(old)}"
    i = t.index(old) + 1
    end = t.index("\n", i)
    return t[:i] + f"[Watch the five-minute walkthrough]({t[i:end]})" + t[end:]


case("readme: the inline player URL is 'tidied' into a markdown link that renders no player",
     "README.md", m129,
     ["tests/test_intro_video.py::test_both_readmes_carry_the_inline_player_url"])


def m130(t):
    """Point the zh-TW README at a different upload from the EN one.

    Two READMEs, two uploads, and nothing that compares them: a reader of one watches a
    different film from a reader of the other, and both pages look correct in isolation. This is
    the same class as every bilingual drift this repo has had to fix -- the pair is only equal
    while something asserts it -- except that here neither copy can be diffed, because both
    URLs resolve to opaque uploads behind a signed JWT.
    """
    old = "user-attachments/assets/f189afb1"
    assert t.count(old) == 1, f"the zh-TW player URL has moved; found {t.count(old)}"
    return t.replace(old, "user-attachments/assets/0badf00d", 1)


case("readme: the zh-TW README points at a different upload from the EN one",
     "README.zh-TW.md", m130,
     ["tests/test_intro_video.py::test_both_readmes_carry_the_inline_player_url"])


def m131(t):
    """Add a helpful download size to the EN walkthrough heading.

    "How big is it" is the obvious thing to tell a reader deciding whether to press play, and it
    is correct on the day it is typed. Retargeted onto the heading because the link it used to
    annotate is deleted -- and the deletion makes the rule STRONGER rather than weaker: the bytes
    a reader now downloads are GitHub's own re-encode of the upload, so a size written here cannot
    be right even on the day it is typed, and nothing in this repo can measure it.

    Worth keeping the finding the old version pinned, because the guard it tested has changed
    shape: this same mutation once PASSED, because the assertion was scoped to the whole `##`
    section and a paragraph above the link still mentioned 10.7 MB. Presence anywhere in a section
    is satisfied by the sentence ABOUT a number, which is not the sentence a reader acts on.
    """
    old = "## Watch it — five minutes, narrated"
    assert t.count(old) == 1, f"the EN walkthrough heading has moved; found {t.count(old)}"
    return t.replace(old, old + " (10.7 MB)", 1)


case("readme: a retyped file size is added back to the EN walkthrough section",
     "README.md", m131,
     ["tests/test_intro_video.py"
      "::test_the_walkthrough_section_states_no_number_it_does_not_derive"])


def m132(t):
    """Add the encoder setting back to the zh-TW section, in the other half of the pattern.

    Retargeted twice, and each retarget records a real loss. It first mutated `"-crf", "26"` in
    record_video.py, and it caught a guard that DERIVED that number and compared it to what both
    READMEs said -- the direction a hardcoded guard cannot see, where the source changes and the
    prose keeps looking measured. Both READMEs stopped quoting the CRF, so nothing derives it and
    retuning the recorder now breaks no test, correctly: there is no claim left to falsify. It
    then mutated the poster link, which is deleted too.

    What remains guardable is the prose half, so that is what this mutates. zh-TW rather than EN
    on purpose: m131 covers the MB half in English, the pattern's two branches are different code
    paths, and the two READMEs are only equal while something checks both.
    """
    old = "## 看五分鐘導覽（有旁白）"
    assert t.count(old) == 1, f"the zh-TW walkthrough heading has moved; found {t.count(old)}"
    return t.replace(old, old + "，CRF 26 錄製", 1)


case("readme: an encoder setting is added back to the zh-TW walkthrough section",
     "README.zh-TW.md", m132,
     ["tests/test_intro_video.py"
      "::test_the_walkthrough_section_states_no_number_it_does_not_derive"])


def m133(t):
    """Make the ffprobe cross-check skip every clip, by moving where it looks for them.

    The direction the coverage assertion exists for, and the reason it exists at all: the
    `if not p.exists(): continue` above it means a wrong audio path is not an error, it is
    silence. `assert not bad` is then satisfied by comparing NOTHING, and the test reports
    green having measured zero of 35 files against the decoder it exists to consult.

    Measured before this control was written, not feared: pointing INTRO at an empty directory
    passed the whole test. The reason it does not ALSO trip the missing-clip guard above is that
    this mutation changes only THIS test's path expression, which is the honest shape of the
    failure -- a guard whose coverage depends on a different guard's existence is one refactor
    from checking nothing, and the refactor is what this simulates.
    """
    old = """            p = INTRO / "audio" / lang / f"{scene}.mp3"
            if not p.exists():
                continue
            out = subprocess.run("""
    assert t.count(old) == 1, f"the ffprobe clip loop has moved; found {t.count(old)}"
    return t.replace(old, old.replace('"audio" / lang', '"audio" / "no-such-lang" / lang'), 1)


case("intro: the ffprobe cross-check compares zero clips and still reports green",
     "tests/test_intro_bundle.py", m133,
     ["tests/test_intro_bundle.py::test_the_duration_measurement_agrees_with_ffprobe"])


def m134(t):
    """Let the scanner's own coverage comment keep a count the repo has moved past.

    The direction that actually happened, twice. These numbers were carefully RE-MEASURED when
    the walkthrough mp4 was deleted -- and "re-measured" describes how a number was produced, not
    whether it stays true. The next commit that adds one tracked file falsifies all four sites and
    the whole suite stays green, which is exactly what happened: 161 was correct on the day it was
    typed and wrong on the day after.

    Mutating the comment rather than adding a file because the guard derives the real count from
    `git ls-files`: a control that added a file would trip the tracked assertion for both sites at
    once and prove nothing about which one is watched.

    Anchored on the guard's OWN regex rather than on a literal, and that is the second
    finding here. Written as a literal naming 161 this control was
    correct for exactly as long as the comment said 161 -- the repo grew to 163, the comment
    was correctly updated, and the control silently stopped applying. Four of these went
    stale together and none of them said so, because a raise out of `mutate` aborted the
    whole runner at case 70. A control that hardcodes the number it is testing for staleness
    has the defect it exists to catch, so it now reads the number off the file and decrements
    it: whatever the comment claims today, the mutant claims one less.
    """
    pat = re.compile(r"(measured across all )(\d+)(\s*\n#:\s*tracked files)")
    assert len(pat.findall(t)) == 1, "the tracked-file coverage claim has moved"
    return pat.sub(lambda m: f"{m.group(1)}{int(m.group(2)) - 1}{m.group(3)}", t, count=1)


case("redaction: the scanner's comment claims a tracked-file count the repo has grown past",
     "tests/redaction_scan.py", m134,
     ["tests/test_redaction_scan.py"
      "::test_the_scanners_own_coverage_claims_match_the_repo"])


def m135(t):
    """Drift the BINARY half of the same claim, which the tracked half cannot cover for it.

    Two counts in one sentence are two claims. The binary number is the one that says why
    dropping the generic 12-digit heuristic on binaries is defensible -- if 35 becomes 40 without
    anyone re-reading that argument, the argument is about a repo that no longer exists.

    Separate from m134 on purpose, and the separation is the finding: when this was first driven
    by adding a tracked BINARY file, the `tracked` assertion fired first and the `binary` half was
    never reached, so a control written that way would have looked correct while proving only what
    m134 already proves. Driven instead by editing the binary number alone.

    Derived from the file, not hardcoded -- see m134 for why: the literal that named 35 went
    stale the moment the tracked count moved, and took its own applicability with it. The
    tracked half is left alone so this control still exercises only the binary assertion.
    """
    pat = re.compile(r"(the whole index -- \d+ files, )(\d+)( of)")
    assert len(pat.findall(t)) == 1, "the binary coverage claim has moved"
    return pat.sub(lambda m: f"{m.group(1)}{int(m.group(2)) - 1}{m.group(3)}", t, count=1)


case("redaction: the scanner's comment claims a binary-file count the repo has moved past",
     "tests/test_redaction_scan.py", m135,
     ["tests/test_redaction_scan.py"
      "::test_the_scanners_own_coverage_claims_match_the_repo"])


def m136(t):
    """Reword one coverage claim so the anchored pattern matches nothing at all.

    The failure mode that turns a derived guard back into decoration: an anchored regex that hits
    nothing is indistinguishable from a claim that is correct, so a comment reworded in good faith
    silently retires its own guard. Same reason LAMBDA_COUNT_PATTERNS in test_docs_claims.py
    asserts its match rather than iterating whatever it happened to find.

    Rewording rather than deleting because deletion is the honest case the guard's message tells
    you how to handle (drop the entry); a rewrite is the one that looks like nothing happened.

    The count is read off the file rather than named here, and it is carried through the
    rewrite unchanged: this control must break the guard by rewording, not by drifting a
    number, or it would prove only what m134 and m135 already prove. Hardcoding it also made
    the control expire silently the moment the repo grew -- see m134.
    """
    pat = re.compile(r"checking a single file out of (\d+)")
    assert len(pat.findall(t)) == 1, "the single-file phrasing has moved"
    return pat.sub(lambda m: f"checking just one of the {m.group(1)} files", t, count=1)


case("redaction: a coverage claim is reworded so its anchored pattern matches nothing",
     "tests/test_redaction_scan.py", m136,
     ["tests/test_redaction_scan.py"
      "::test_the_scanners_own_coverage_claims_match_the_repo"])


def m137(t):
    """Let the CURRENT half of the past-tense count line go stale.

    The carve-out that makes the past-tense phrasing legal is narrow, and this is what keeps it
    narrow. "N files became M" is allowed to state a former number because it says so; the
    number it says the repo BECAME is a claim about today and is held to today. Without this the
    carve-out would be a hole the size of every count in the comment -- any stale number could be
    made legal by writing "X became Y" around it.

    The historical half is deliberately NOT mutated here: the former count must stay allowed,
    which the guard's own passing run proves and which no control should contradict. Both halves
    are read off the file for the reason in m134 -- the literal pair this once named ("163 files
    became 161") stopped matching when the comment was correctly updated to "162 files became
    163", and the control retired itself without a word.
    """
    pat = re.compile(r"(\d+)( files became )(\d+)")
    found = pat.findall(t)
    assert len(found) == 1, "the past-count phrase has moved"
    was, now = int(found[0][0]), int(found[0][2])
    # Only the CURRENT half moves, and it must not land on `was`: the guard also asserts
    # was != now, so a mutant that collapsed them would go red for the wrong reason -- "not a
    # change at all" rather than "stale" -- and prove nothing about the staleness check. The
    # live pair is 162 -> 163, where a plain decrement does exactly that.
    bad = next(v for v in (now - 1, now + 1, now - 2) if v != was)
    return pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{bad}", t, count=1)


case("redaction: the past-tense count line says the repo became a size it is not",
     "tests/redaction_scan.py", m137,
     ["tests/test_redaction_scan.py"
      "::test_the_scanners_own_coverage_claims_match_the_repo"])


def m138(t):
    """State a past fleet size the record does not have (zh-TW).

    The era carve-out let a marked section say ANY number. Both existing checks passed on
    "五個 agent ... v1 當時 ... 今天是七個": the section carries an era marker, and it states
    today's count, so nothing looked at whether five was ever the fleet size. Measured on merged
    main before the fix, not reasoned about -- 六 -> 五 was green there.

    A doc may state a FORMER count. It may not state a former count that never existed, which is
    why the accepted value is read from the evidence file the section cites rather than restated
    in the guard.
    """
    old = "而是六個 agent\n在例行巡檢"
    assert t.count(old) == 1, f"the zh-TW past-fleet phrase has moved; found {t.count(old)}"
    return t.replace(old, "而是五個 agent\n在例行巡檢", 1)


case("case study: the past fleet size is a number the cited evidence never records (zh-TW)",
     "docs/CASE_STUDY.zh-TW.md", m138,
     ["tests/test_docs_claims.py"
      "::test_the_agent_count_readers_see_first_matches_the_fleet"])


def m139(t):
    """The same break in English. Separate case, not a second assertion in one: the two halves of
    a bilingual claim are guarded by separate patterns, and a repo where only the English half is
    driven to red has an unverified half."""
    old = "six agents that hold the pager"
    assert t.count(old) == 1, f"the EN past-fleet phrase has moved; found {t.count(old)}"
    return t.replace(old, "five agents that hold the pager", 1)


case("case study: the past fleet size is a number the cited evidence never records (EN)",
     "docs/CASE_STUDY.md", m139,
     ["tests/test_docs_claims.py"
      "::test_the_agent_count_readers_see_first_matches_the_fleet"])


def m140(t):
    """Reword the evidence sentence the carve-out reads its accepted count FROM.

    Deriving the past count from a record is only better than hardcoding it while the record still
    states it. If that sentence is reworded, `re.search` returns None -- and a guard that treats
    "no match" as "nothing to check" would silently accept any past count again, which is the
    failure mode this whole file exists to catch. So the miss must be loud, and this proves it is.
    """
    old = "All six harnesses currently run"
    assert t.count(old) == 1, f"the v1 fleet-size sentence has moved; found {t.count(old)}"
    return t.replace(old, "Every harness currently runs", 1)


case("evidence: the sentence the era carve-out derives the past fleet size from is reworded",
     "deploy/evidence/VERIFICATION_phase5.md", m140,
     ["tests/test_docs_claims.py"
      "::test_the_agent_count_readers_see_first_matches_the_fleet"])


# ── a triage's records must be addressed by the invocation, not by the agent ───────────
#
# 141. handle_page_human goes back to trusting the agent's run_id, which is where it was.
#      On a triage `event["run_id"]` is `triage-<subject>`, so a page whose args omit
#      run_id -- schema-legal, it is not in page_human's required list -- files the
#      HumanPaged row and the owner's brief under the conductor's own timeline. Measured
#      live: 3 of the 12 pages on record, all ARC-2 lineage runs. Restoring the fallback
#      must red the parametrised addressing guard, not merely the one arg shape that
#      happens to name the subject (the pre-existing test used only that shape, which is
#      why the defect survived it).
def m141(t):
    old = ('    subject_run = triage_subject(event) or str(args.get("run_id") or "")')
    assert t.count(old) == 1, f"the page addressing line has moved; found {t.count(old)}"
    return t.replace(
        old, '    subject_run = str(args.get("run_id") or event.get("run_id") or "")', 1)


case("driver: a page is addressed by the agent's run_id again, not by the escalation",
     "orchestration/harness_driver/handler.py", m141,
     ["tests/test_orchestration.py::TestDriver"
      "::test_a_bus_triage_page_is_addressed_by_the_event_not_the_agent",
      "tests/test_orchestration.py::TestDriver"
      "::test_a_page_with_no_derivable_subject_is_still_recorded"])


# 142. resolve_escalation goes back to skipping the delivery when the agent names no run,
#      while still returning {"status": "resolved"} -- a status inside TRIAGE_ANSWERED, so
#      #72's backstop stays quiet too and an unanswered escalation is reported as
#      answered. The guard must catch BOTH halves: the verdict not reaching the mailbox,
#      and the false success.
def m142(t):
    old = '                if not subject:'
    assert t.count(old) == 1, f"the subject-less resolve gate has moved; found {t.count(old)}"
    return t.replace(old, '                if False:', 1)


case("driver: a resolve naming no run silently reports success again",
     "orchestration/harness_driver/handler.py", m142,
     ["tests/test_orchestration.py::TestDriver"
      "::test_a_resolve_naming_no_run_is_rejected_not_reported_resolved"])


# 143. A control's anchor goes stale exactly the way six of them really did -- and the fast
#      check has to be the thing that notices, in a second, rather than a 5-minute run
#      nobody does per commit. This mutation drifts m1's target so it raises, which is the
#      harder half: an anchor that RETURNS UNCHANGED is a visible no-op, while one that
#      raises used to abort the whole runner at that case and leave everything after it
#      silently unverified.
def m143(t):
    # Split literal, same reason as m83: this case mutates the file it lives in, so an
    # anchor written whole would appear twice -- once in m1 and once here -- and the count
    # assertion would fire against the runner's own source rather than against drift.
    old = 'del d["States"]["Monitor' + 'Health"]'
    assert t.count(old) == 1, f"m1's ASL mutation has moved; found {t.count(old)}"
    return t.replace(old, 'del d["States"]["NoSuchStateHasEverExisted"]', 1)


case("controls: a control's own anchor no longer matches the code it mutates",
     "tests/negative_controls/monitor_dispatch.py", m143,
     ["tests/test_docs_claims.py"
      "::test_every_negative_control_still_matches_the_code_it_mutates"])


def m144(t):
    """Give a control a git-history dependency again -- the thing CI has and this laptop hides.

    The reverted defect is `m127`'s original recovery of the account id by walking commits.
    It cannot be tested by running it here: a full-depth worktree has the history, so the
    control passes locally no matter what, which is exactly why the guard is structural and
    exactly why this mutation reintroduces the CALL rather than the failure. The planted argv
    is the one that shipped, verbatim.

    The paired guard must also be shown NOT to fire on `git ls-files` / `git show :path` /
    `git diff --cached`, which several controls here use legitimately and which answer
    identically at depth 1; that half is asserted directly in the guard's own test file,
    since a negative control can only demonstrate the red direction.
    """
    anchor = '    fabricated = b"5555" + b"55555555"\n'
    assert t.count(anchor) == 1, f"m127's fabricated id has moved; found {t.count(anchor)}"
    return t.replace(anchor, '    subprocess.run(["git", "log", "--all", "-S", '
                             '"arn:aws:iam::", "--format=%H"],\n'
                             '                   capture_output=True, text=True, cwd=REPO)\n'
                     + anchor, 1)


case("controls: a control reads git commit history, which CI clones at depth 1",
     "tests/negative_controls/monitor_dispatch.py", m144,
     ["tests/test_docs_claims.py"
      "::test_no_negative_control_depends_on_commit_history"])


# ---------------------------------------------------------------------------------------
# 146-150. Bug #18: 11 interface endpoints billed for a consumer that does not exist, and
# a cost note that was exactly half the real figure.
#
# Registered late, with bug #19's and bug #20's below: all three were mutation-checked by
# hand instead of here, and the hand method produced a false result (see the bug #20 block
# for what the .pyc cache did). A control that lives in a shell script verifies one
# afternoon; a control registered here verifies every run, and is counted.

_NETWORK = "deploy/02_network.py"


# 146. The cost note drops the AZ factor and lands back on $2.64/day -- the exact number
#      that was printed while $5.28 was billed, because SubnetIds makes the ENI the
#      billed unit and the script attaches every endpoint to both subnets.
def m146(t):
    old = "    return ENDPOINT_USD_PER_AZ_HOUR * n_services * n_azs * 24"
    assert t.count(old) == 1, f"the cost derivation has moved; found {t.count(old)}"
    return t.replace(old, "    return ENDPOINT_USD_PER_AZ_HOUR * n_services * 24", 1)


case("network: the endpoint cost note halves itself by ignoring AZs", _NETWORK, m146,
     ["tests/test_orchestration.py"
      "::test_the_endpoint_cost_note_counts_every_az_not_every_endpoint"])


# 147. The printed total is inlined again rather than derived from the two lists. The
#      original defect was a correct-LOOKING expression in the print call, so a guard on
#      the function alone passed against it.
def m147(t):
    old = "endpoint_cost_per_day("
    n = t.count(old)
    assert n >= 2, f"endpoint_cost_per_day is no longer called from main(); found {n}"
    body_at = t.index("def main(")
    head, body = t[:body_at], t[body_at:]
    assert old in body, "main() no longer calls endpoint_cost_per_day"
    return head + body.replace(old, "(lambda *_: 5.28)(", 1)


case("network: the printed cost is inlined instead of derived from both lists", _NETWORK,
     m147, ["tests/test_orchestration.py::test_the_printed_cost_is_derived_from_both_lists"])


# 148. `want_interface` is hardcoded True -- the mutation that passed every guard until a
#      test drove main() itself. Two correct components wired together wrongly.
def m148(t):
    old = "    want_interface = bool(consumers) or args.force_unused_endpoints"
    assert t.count(old) == 1, f"the want_interface decision has moved; found {t.count(old)}"
    return t.replace(old, "    want_interface = True", 1)


case("network: main() provisions the billed endpoints for nobody again", _NETWORK, m148,
     ["tests/test_orchestration.py"
      "::test_main_withholds_the_billed_endpoints_when_nothing_consumes_them"])


# 149. The consumer check stops reading harness.prod.json, so a VPC-mode harness someone
#      really builds would never re-enable the endpoints it needs. The check has to be
#      able to go green on its own, or it is the hand-set flag it replaced.
def m149(t):
    old = '    for cfg in sorted((repo / "agents").glob("*/harness.prod.json")):'
    assert t.count(old) == 1, f"the prod-config scan has moved; found {t.count(old)}"
    return t.replace(old, "    for cfg in []:", 1)


case("network: the consumer check stops reading the prod harness configs", _NETWORK, m149,
     ["tests/test_orchestration.py::test_the_consumer_check_reads_the_files_a_deploy_reads"])


# 150. The consumer check stops reading 07_lambdas.py for VpcConfig -- the other half of
#      the same "capability with no deploy path" claim.
def m150(t):
    old = '    if lambdas.exists() and "VpcConfig" in lambdas.read_text():'
    assert t.count(old) == 1, f"the VpcConfig scan has moved; found {t.count(old)}"
    return t.replace(old, "    if False:", 1)


case("network: the consumer check stops reading the Lambda deploy for VpcConfig",
     _NETWORK, m150,
     ["tests/test_orchestration.py::test_the_consumer_check_reads_the_files_a_deploy_reads"])


# ---------------------------------------------------------------------------------------
# 151-153. Bug #19: five harnesses called the terminal exit a pause.
#
# `escalate_human` ENDS the invocation (send_task_failure -> EscalateFail -> Fail, and
# "escalated" is in UNREACHABLE_RUN_STATES so a directive sent afterwards reaches nobody);
# `checkpoint` is the platform's only live human-in-the-loop pause. Descriptions that said
# otherwise sent every blocked agent to the exit.


# 151. A tool description calls the terminal exit a pause again.
def m151(t):
    d = json.loads(t)
    hits = [tool for tool in d["tools"] if tool.get("name") == "escalate_human"]
    assert len(hits) == 1, f"eval's escalate_human tool has moved; found {len(hits)}"
    fn = hits[0]["config"]["inlineFunction"]
    fn["description"] = ("The pipeline pauses and waits for a human to decide. "
                         + fn["description"])
    return json.dumps(d, indent=2, ensure_ascii=False)


case("prompt: escalate_human is described as a pause again", "agents/eval/harness.json",
     m151, ["tests/test_orchestration.py::TestConductorDispatch"
            "::test_no_tool_description_calls_the_terminal_exit_a_pause"])


# 152. The docs describe escalation as a pause again -- the claim an operator plans a
#      night around, and the one that made the audit propose a second HumanGate state
#      beside a working pause.
def m152(t):
    old = "**terminal**"
    assert t.count(old) >= 1, f"the escalate row's terminal marker has moved; found {t.count(old)}"
    return t.replace(old, "The pipeline pauses and waits", 1)


case("docs: the escalate row calls the terminal exit a pause (EN)", "docs/ARCHITECTURE.md",
     m152, ["tests/test_docs_claims.py::test_the_docs_do_not_describe_escalation_as_a_pause"])


# 153. Same claim in the zh-TW twin. Registered separately because a twin that drifts is
#      the failure mode bilingual docs actually have -- one language gets corrected.
def m153(t):
    old = "**終止**"
    assert t.count(old) >= 1, f"the zh-TW terminal marker has moved; found {t.count(old)}"
    return t.replace(old, "管線會暫停", 1)


case("docs: the escalate row calls the terminal exit a pause (zh-TW)",
     "docs/ARCHITECTURE.zh-TW.md", m153,
     ["tests/test_docs_claims.py::test_the_docs_do_not_describe_escalation_as_a_pause"])


# ---------------------------------------------------------------------------------------
# 154-170. Bug #20: four names for one fact, and a resolver nothing consumed.
#
# The plan a human signs is PRICED by cost_model.py, RESOLVED by start_pipeline, and
# EXECUTED by the driver, and all three named the model differently. Measured: a
# console-signed Fable-5 plan produced manifest.models.teacher = us.deepseek.r1-v1:0 --
# priced as one model, run on another, with every artifact agreeing.
#
# These live here rather than in a shell script for the reason this file's own docstring
# gives: I ran them by hand first, and the .pyc hazard documented at the top of this
# module bit exactly as described. `{**approved, **payload["params"]}` and
# `{**payload["params"], **approved}` are the same byte count, so a mutate-run-restore
# cycle inside one second let the restored source import the MUTATED bytecode -- the
# override case was reported as caught while the interpreter ran the wrong code. `run()`
# above sets PYTHONDONTWRITEBYTECODE and the loop clears __pycache__ per case, which is
# the only reason these results mean anything.

_RESOLVER = "orchestration/start_pipeline/handler.py"
_DRIVER = "orchestration/harness_driver/handler.py"
_REPORT = "pipeline/contracts/report.py"
_D = "tests/test_orchestration.py::TestConductorDispatch"
_CON = "tests/test_orchestration.py::TestContracts"


# 154. The resolver reads only the nested `models` block again -- the original bug. The
#      console form posts the FLAT `teacher_model`, so `models` is absent, "the plan is
#      silent" wins, and DEFAULT_MODELS spends on a model nobody approved.
def m154(t):
    old = '    plan_roles = _role_assignments(_as_obj(plan, "plan"), "plan")'
    assert t.count(old) == 1, f"the plan-side resolve has moved; found {t.count(old)}"
    return t.replace(old, '    plan_roles = {k: v for k, v in _as_obj(_as_obj(plan, "plan")'
                          '.get("models"), "m").items() if k in MODEL_ROLES}', 1)


case("resolver: the signed plan's flat teacher_model is ignored again", _RESOLVER, m154,
     [f"{_D}::test_the_console_form_field_name_is_the_one_consent_is_read_from",
      f"{_D}::test_every_plan_field_the_estimator_prices_from_is_one_the_dispatcher_obeys"])


# 155. One document naming the same role twice with two ids is resolved by precedence
#      instead of refused -- a silent choice where a signature exists to settle it.
def m155(t):
    old = "        if len(distinct) > 1:"
    assert t.count(old) == 1, f"the alias-conflict gate has moved; found {t.count(old)}"
    return t.replace(old, "        if False:", 1)


case("resolver: a plan naming one role twice with two ids picks one silently", _RESOLVER,
     m155, [f"{_D}::test_a_plan_that_names_one_model_twice_with_two_ids_is_refused"])


# 156. The conflict check compares FIELD NAMES rather than facts, so
#      params.teacher_model contradicting plan.models.teacher is waved through.
def m156(t):
    old = ('    conflicts = sorted(r for r in set(plan_roles) & set(param_roles)\n'
           '                       if plan_roles[r] != param_roles[r])')
    assert t.count(old) == 1, f"the conflict check has moved; found {t.count(old)}"
    return t.replace(old, '    conflicts = sorted(r for r in '
                          'set(_as_obj(plan, "plan").get("models") or {}) & '
                          'set(_as_obj(params, "params").get("models") or {})\n'
                          '                       if plan_roles.get(r) != param_roles.get(r))',
                     1)


case("resolver: an alias-spelled conflict with the signed plan is not seen", _RESOLVER,
     m156, [f"{_D}::test_a_conflict_is_caught_across_two_different_alias_spellings"])


# 157. The unknown-key check goes away, so `teachr` means "the plan is silent about the
#      teacher" again and a typo becomes an unapproved spend instead of one error.
def m157(t):
    old = "    if unknown:"
    assert t.count(old) == 1, f"the unknown-key gate has moved; found {t.count(old)}"
    return t.replace(old, "    if False:", 1)


case("resolver: a misspelled role is read as silence again", _RESOLVER, m157,
     [f"{_D}::test_a_misspelled_role_is_refused_rather_than_read_as_silence"])


# 158. A mirrored, licence-checked repo that fills no role is accepted -- the run trains
#      on a model nobody cleared while the cleared one sits unused in the mirror.
def m158(t):
    old = "    if repo and repo not in set(found.values()):"
    assert t.count(old) == 1, f"the mirror/role gate has moved; found {t.count(old)}"
    return t.replace(old, "    if False:", 1)


case("resolver: a mirrored repo filling no role is accepted", _RESOLVER, m158,
     [f"{_D}::test_a_mirrored_repo_that_fills_no_role_is_refused"])


# 159. The mirror check compares publishers instead of model identities, so
#      hf_repo=meta-llama/Llama-3.2-1B with student=meta-llama/Llama-3.1-70B passes:
#      a different model, different pinned revision, 70x the size. This escaped the
#      first time -- the guard only tested the absent-role case, never the near-miss.
def m159(t):
    old = "    if repo and repo not in set(found.values()):"
    assert t.count(old) == 1, f"the mirror/role gate has moved; found {t.count(old)}"
    return t.replace(old, '    if repo and not any(repo.split("/")[0] in v '
                          'for v in found.values()):', 1)


case("resolver: the mirror check matches the publisher, not the model", _RESOLVER, m159,
     [f"{_D}::test_a_mirrored_repo_that_fills_no_role_is_refused"])


# 160. The alias list loses `teacher_model` -- the exact field the console form posts and
#      cost_model prices from. The dispatcher then knows a vocabulary the UI does not use.
def m160(t):
    old = '"teacher": ("teacher", "teacher_model", "teacher_model_id"),'
    assert t.count(old) == 1, f"ROLE_ALIASES' teacher row has moved; found {t.count(old)}"
    return t.replace(old, '"teacher": ("teacher", "teacher_model_id"),', 1)


case("resolver: the alias list drops the console's own field name", _RESOLVER, m160,
     [f"{_D}::test_the_console_form_field_name_is_the_one_consent_is_read_from",
      f"{_D}::test_every_plan_field_the_estimator_prices_from_is_one_the_dispatcher_obeys"])


# 161. The estimator renames the field it prices the teacher from, out of the
#      dispatcher's vocabulary entirely. This escaped twice: a guard that intersects
#      cost_model's field names with ROLE_ALIASES is blind in exactly the direction the
#      bug travels, because a renamed field simply disappears from the intersection.
#      The fix identifies a model field by the model id it DEFAULTS to.
def m161(t):
    old = 'plan.get("teacher_model")'
    assert t.count(old) >= 1, f"cost_model's teacher field has moved; found {t.count(old)}"
    return t.replace(old, 'plan.get("tchr_mdl")')


case("estimator: prices the teacher from a field the dispatcher never reads",
     "pipeline/contracts/cost_model.py", m161,
     [f"{_D}::test_the_console_form_field_name_is_the_one_consent_is_read_from",
      f"{_D}::test_every_plan_field_the_estimator_prices_from_is_one_the_dispatcher_obeys"])


# 162. The estimator prices a model from a field the console form cannot post, so the
#      quote a customer signs is computed from a default they were never shown.
def m162(t):
    old = 'harness_model = str(plan.get("harness_model", "global.anthropic.claude-fable-5"))'
    assert t.count(old) == 2, f"cost_model's harness field has moved; found {t.count(old)}"
    return t.replace(old, 'harness_model = str(plan.get("harness", '
                          '"global.anthropic.claude-fable-5"))')


case("estimator: prices from a field the console form cannot post",
     "pipeline/contracts/cost_model.py", m162,
     [f"{_D}::test_the_console_form_field_name_is_the_one_consent_is_read_from"])


# 163. The console form posts a field nothing prices and nothing dispatches. This
#      escaped the first run for the same reason m161 did: the guard SKIPPED any field
#      whose role it
#      did not recognise, so renaming it made the mismatch invisible rather than red.
def m163(t):
    old = '"teacher_model"'
    assert t.count(old) >= 1, f"the console's STR_KEYS has moved; found {t.count(old)}"
    return t.replace(old, '"teacher_mdl"', 1)


case("console: the signed form posts a field nothing prices or dispatches",
     "deploy/console/lambda_function.py", m163,
     [f"{_D}::test_the_console_form_field_name_is_the_one_consent_is_read_from"])


# The stage-payload merge line, DERIVED. m164/m165/m183 below all patch it, and all
# three went dead the moment it correctly gained a fourth source (`signed`, the settings
# the plan a human signed carries -- see signed_plan_params). A control pinned to a
# literal expires when that literal is legitimately corrected, and the expiry is SILENT:
# the mutation raises its own assert, the runner counts a "catch", and the guard it
# claimed to verify goes unverified. So the anchor is a shape, not a string.
def _payload_merge_line(t: str) -> str:
    m = re.search(r'^ +payload\["params"\] = \{\*\*.*\}$', t, re.M)
    assert m, "the stage-payload params merge no longer matches; re-anchor these controls"
    return m.group(0)


# 164. The driver stops injecting the manifest's approved models -- the consumer half of
#      the bug. The resolver stays correct and every prompt still reads
#      params.teacher_model_id, so agents fall back to whatever model their persona line
#      names. Two correct halves, never connected: this repo's recurring bug shape.
def m164(t):
    old = _payload_merge_line(t)
    assert t.count(old) == 1, f"the driver's model injection has moved; found {t.count(old)}"
    return t.replace(old, "        pass  # injection removed", 1)


case("driver: the approved models never reach the agent turn", _DRIVER, m164,
     [f"{_D}::test_a_stage_payload_carries_the_approved_models",
      f"{_D}::test_a_caller_supplied_model_overrides_the_manifest_but_is_not_the_default"])


# 165. The merge is reversed, so the manifest OVERRIDES an explicit caller param and a
#      remediation iteration that deliberately names a model is silently overruled.
#      The mutation this file's docstring warning is about: same byte count as the
#      original, so a same-second hand-run cycle validated stale bytecode and reported a
#      catch. It also escaped a second, real way -- the test recomputed the merge in its
#      own body, and a merge order restated in a test is satisfied by any order in the
#      code. The guard now drives the real `_run_stage`.
def m165(t):
    old = _payload_merge_line(t)
    assert t.count(old) == 1, f"the driver's model merge has moved; found {t.count(old)}"
    return t.replace(
        old, '        payload["params"] = {**payload["params"], **approved, **facts}', 1)


case("driver: the manifest overrides an explicit caller model", _DRIVER, m165,
     [f"{_D}::test_a_caller_supplied_model_overrides_the_manifest_but_is_not_the_default"])


# 166. A role the manifest is silent about gets a DEFAULT again -- the same bug one layer
#      down. A stage that needs a teacher and finds no param must fail visibly.
def m166(t):
    old = ("            for role, param in MODEL_PARAM_FOR_ROLE.items()\n"
           "            if models.get(role)}")
    assert t.count(old) == 1, f"the role filter has moved; found {t.count(old)}"
    return t.replace(old, "            for role, param in MODEL_PARAM_FOR_ROLE.items()\n"
                          '            if True} if models else '
                          '{"teacher_model_id": "us.deepseek.r1-v1:0"}', 1)


case("driver: a role the manifest is silent about is defaulted", _DRIVER, m166,
     [f"{_D}::test_a_stage_payload_carries_the_approved_models",
      f"{_D}::test_a_caller_supplied_model_overrides_the_manifest_but_is_not_the_default"])


# 167. The driver supplies the model under a param name no prompt reads, so the injection
#      exists and reaches nobody -- indistinguishable from not injecting at all.
def m167(t):
    old = 'MODEL_PARAM_FOR_ROLE = {"teacher": "teacher_model_id", "student": "student_model_id",'
    assert t.count(old) == 1, f"MODEL_PARAM_FOR_ROLE has moved; found {t.count(old)}"
    return t.replace(old, 'MODEL_PARAM_FOR_ROLE = {"teacher": "teacher_model", '
                          '"student": "student_model_id",', 1)


case("driver: the approved model arrives under a name no prompt reads", _DRIVER, m167,
     [f"{_D}::test_a_stage_payload_carries_the_approved_models",
      f"{_D}::test_a_caller_supplied_model_overrides_the_manifest_but_is_not_the_default"])


# 168. A malformed `models` block crashes the stage instead of degrading to "no approved
#      models", which would take out deploy smoke tests and monitor sweeps that need none.
def m168(t):
    old = ("    if not isinstance(models, dict):\n"
           "        return {}")
    assert t.count(old) == 1, f"the manifest type guard has moved; found {t.count(old)}"
    return t.replace(old, "    pass", 1)


case("driver: a malformed manifest models block crashes the stage", _DRIVER, m168,
     [f"{_D}::test_the_driver_injects_the_manifest_models_under_the_prompt_names"])


# 169. A persona line hardcodes a model id again -- the thing an agent falls back to when
#      its model param is absent, and the reason the platform could not run a customer's
#      own open-weight distillation or a YOLO fine-tune without editing prompts.
def m169(t):
    d = json.loads(t)
    p = d["systemPrompt"][0]["text"]
    old = "model customisation"
    assert p.count(old) == 1, f"the eval persona line has moved; found {p.count(old)}"
    d["systemPrompt"][0]["text"] = p.replace(
        old, "knowledge distillation: teacher DeepSeek-R1 on Bedrock -> "
             "student Qwen3-1.7B", 1)
    return json.dumps(d, indent=2, ensure_ascii=False)


case("prompt: a persona line hardcodes the model to use again", "agents/eval/harness.json",
     m169, [f"{_D}::test_no_prompt_hardcodes_a_model_id_as_the_one_to_use"])


# 170. A prompt reads a model param the driver does not supply -- the absent-param
#      fallback that made the persona line load-bearing in the first place.
def m170(t):
    d = json.loads(t)
    p = d["systemPrompt"][0]["text"]
    old = "params.student_model_id"
    assert p.count(old) >= 1, f"finetune's student param has moved; found {p.count(old)}"
    d["systemPrompt"][0]["text"] = p.replace(old, "params.base_model_id", 1)
    return json.dumps(d, indent=2, ensure_ascii=False)


case("prompt: reads a model param the driver never supplies",
     "agents/finetune/harness.json", m170,
     [f"{_D}::test_every_model_param_a_prompt_reads_is_one_the_driver_supplies"])


# ---------------------------------------------------------------------------------------
# 171-179. Bug #21: the rest of the signed plan, dropped the same way the models were.
#
# Bugs #9 and #20 cured model consent and then the NAME model consent is written under.
# Both left every other field of the plan behind: seed_manifest read `plan` for models and
# nothing else, so `{**DEFAULT_PARAMS, **params}` silently substituted ARC-shaped defaults
# for what a human signed. Measured on a signed industrial-defect plan: priced on
# ml.p4d.24xlarge with 40000 samples and a {"map50": 0.75} gate, executed on ml.g5.2xlarge
# with 2000 samples and ARC's relative_solve_rate gate, every artifact agreeing.


# 171. The merge drops the plan again -- the original bug, byte for byte.
def m171(t):
    old = "    return {**DEFAULT_PARAMS, **params, **plan_params}"
    assert t.count(old) == 1, f"the params merge has moved; found {t.count(old)}"
    return t.replace(old, "    return {**DEFAULT_PARAMS, **params}", 1)


case("resolver: the signed plan's stage settings are dropped for ARC defaults", _RESOLVER,
     m171,
     [f"{_D}::test_every_plan_field_the_estimator_prices_reaches_the_stage_that_spends_it",
      f"{_D}::test_the_plan_can_displace_the_arc_specific_defaults"])


# 172. The ARC defaults outrank the plan -- the same merge written the other way round,
#      which is the version that looks correct and silently discards the signature.
def m172(t):
    old = "    return {**DEFAULT_PARAMS, **params, **plan_params}"
    assert t.count(old) == 1, f"the params merge has moved; found {t.count(old)}"
    return t.replace(old, "    return {**params, **plan_params, **DEFAULT_PARAMS}", 1)


case("resolver: DEFAULT_PARAMS outranks the plan a human signed", _RESOLVER, m172,
     [f"{_D}::test_the_plan_can_displace_the_arc_specific_defaults"])


# 173. The conflict gate compares `params` against the plan's TOP-LEVEL keys only, so a
#      dispatch contradicting a field that arrived through the nested `data` block is
#      resolved by precedence instead of refused -- silently, and on the one field a data
#      audit is entirely about: which customer bytes it reads.
#
# This slot first held `{**DEFAULT_PARAMS, **plan_params, **params}` -- params outranking
# the plan, the bug #9 bypass reopened for money. That mutation is UNOBSERVABLE and the
# runner proved it: the gate above has already established that every key the two share
# holds an equal value, so the two merge orders are the same dict by construction. A
# control whose mutation cannot change any output is not evidence about the guard, it is
# evidence about the control. The precedence that CAN be subverted is the gate's own
# reach, which is what this mutates instead.
def m173(t):
    old = "    conflicts = sorted(k for k in set(plan_params) & set(params)"
    assert t.count(old) == 1, f"the params conflict gate has moved; found {t.count(old)}"
    return t.replace(old, "    conflicts = sorted(k for k in set(plan) & set(params)", 1)


case("resolver: a dispatch silently overrides a plan field nested under `data`", _RESOLVER,
     m173, [f"{_D}::test_a_dispatch_contradicting_the_signed_plans_settings_is_refused"])


# 174. The contradiction is resolved by precedence instead of refused. Both readings are
#      defensible, which is exactly why neither may be chosen silently.
def m174(t):
    old = "    if conflicts:\n        detail = \", \".join(f\"{k}: plan={plan_params[k]!r} vs params={params[k]!r}\""
    assert t.count(old) == 1, f"the params conflict gate has moved; found {t.count(old)}"
    return t.replace(old, "    if False:\n        detail = \", \".join(f\"{k}: plan={plan_params[k]!r} vs params={params[k]!r}\"", 1)


case("resolver: plan-vs-dispatch disagreement picks a side silently", _RESOLVER, m174,
     [f"{_D}::test_a_dispatch_contradicting_the_signed_plans_settings_is_refused"])


# 175. PLAN_META_KEYS becomes an ALLOWLIST of what may reach params -- the direction that
#      omits the field nobody thought of, which is how pipeline_mode and gates went missing.
def m175(t):
    old = "    out = {k: v for k, v in plan.items() if k not in PLAN_META_KEYS}"
    assert t.count(old) == 1, f"the plan-params filter has moved; found {t.count(old)}"
    return t.replace(old, '    out = {k: v for k, v in plan.items() '
                          'if k in ("sample_count", "task_count")}', 1)


case("resolver: only a remembered handful of plan fields reach params", _RESOLVER, m175,
     [f"{_D}::test_every_plan_field_the_estimator_prices_reaches_the_stage_that_spends_it",
      f"{_D}::test_pipeline_mode_in_a_signed_plan_reaches_the_choice_state"])


# 176. The plan's nested `data` block stops being flattened, so data-prep's audit task
#      reads params.source_uri and finds nothing -- and its prompt forbids guessing one.
def m176(t):
    old = '    data = plan.get("data")'
    assert t.count(old) == 1, f"the data-block flatten has moved; found {t.count(old)}"
    return t.replace(old, '    data = None', 1)


case("resolver: the plan's data block never reaches the flat params the prompt reads",
     _RESOLVER, m176,
     [f"{_D}::test_the_plans_data_block_reaches_the_prompt_that_reads_it_flat"])


# 177. The nested `data` value OVERWRITES an explicit top-level one -- the same
#      more-specific-statement-loses defect, one layer in.
def m177(t):
    old = "            out.setdefault(k, v)"
    assert t.count(old) == 1, f"the setdefault has moved; found {t.count(old)}"
    return t.replace(old, "            out[k] = v", 1)


case("resolver: a nested data key overwrites the plan's explicit top-level one", _RESOLVER,
     m177, [f"{_D}::test_the_plans_data_block_reaches_the_prompt_that_reads_it_flat"])


# 178. The console's approve->launch forwards no plan again, so a customer's priced
#      instance types and teacher model die between the estimate record and the run.
#
# Two controls, because the block can be broken two ways that no single guard sees. The
# orchestration guard reads start_run's SOURCE for `payload["plan"] =` -- so DELETING the
# assignment (m178) trips it, but neutering the branch around it (m178b) does not: the text
# is still there. The runner proved that escape. The catch for m178b has to be a test that
# inspects the payload the Lambda client was actually HANDED, which is what
# test_the_launch_payload_carries_the_priced_plan does.
def m178(t):
    old = '            payload["plan"] = priced'
    assert t.count(old) == 1, f"the console plan forward has moved; found {t.count(old)}"
    return t.replace(old, '            pass', 1)


case("console: the priced plan is not forwarded to start-pipeline",
     "deploy/console/lambda_function.py", m178,
     [f"{_D}::test_the_console_launch_forwards_the_priced_plan_not_two_integers",
      "tests/test_console_cost.py::test_the_launch_payload_carries_the_priced_plan"])


# 178b. The forwarding code stays, unreachable -- the shape a source-text guard cannot see.
def m178b(t):
    old = '    if est is not None:\n        try:\n            priced = json.loads(est.get("plan", "{}"))'
    assert t.count(old) == 1, f"the console plan forward has moved; found {t.count(old)}"
    return t.replace(old, '    if False:\n        try:\n            priced = json.loads(est.get("plan", "{}"))', 1)


case("console: the plan-forwarding block is present but unreachable",
     "deploy/console/lambda_function.py", m178b,
     ["tests/test_console_cost.py::test_the_launch_payload_carries_the_priced_plan"])


# 179. The eval agent gates on a bar it remembers instead of the one the plan named --
#      the consumer half. A detector run would be judged on ARC's relative_solve_rate.
def m179(t):
    d = json.loads(t)
    p = d["systemPrompt"][0]["text"]
    old = "the quality gates NAMED IN params.gates"
    assert p.count(old) == 1, f"eval's gate line has moved; found {p.count(old)}"
    d["systemPrompt"][0]["text"] = p.replace(
        old, "the quality gates (student judge-score >= 0.80 x teacher score)", 1)
    return json.dumps(d, indent=2, ensure_ascii=False)


case("prompt: eval gates on a remembered bar, not the one the plan named",
     "agents/eval/harness.json", m179,
     [f"{_D}::test_the_gate_prompt_reads_the_thresholds_the_plan_named"])


# 180. The stage results never go back to S3 -- bug #22 itself. The driver assembles
#      `stages[stage]`, hands it to write_run_report, and drops it. Measured before the fix:
#      `manifest.stages` was still `{}` after a deploy stage reported an endpoint_name, so
#      the report humans read carried every metric and the manifest AGENTS read carried none.
def _manifest_writeback_line(t: str) -> str:
    """The driver's `_save_manifest(...)` call inside handle_stage_complete, derived.

    Two controls need this exact line and both used to spell it out, so adding one keyword
    argument to the call retired both at once -- each reporting "anchor drifted", which the
    runner counts as neither caught nor uncaught. Matched on the call's opening rather than
    its argument list: the arguments are what evolves, the statement's position between the
    `try` and the manifest `except` is what these mutations depend on.
    """
    m = re.search(r'^ {8}_save_manifest\(c\["s3"\], event\["manifest_uri"\], .*\)$',
                  t, re.M)
    assert m, "the manifest write-back has moved; re-anchor these controls"
    return m.group(0)


def m180(t):
    # `pass`, not deletion: since #25 split the two artifact writes, _save_manifest is the
    # only statement in its own try block, so removing the line leaves an IndentationError
    # and pytest exits 4 (collection error) instead of 1 (test failed). A control that
    # cannot even import the module under test verifies nothing -- it reports the guard as
    # UNCAUGHT while the mutation it claims to make was never really applied.
    old = _manifest_writeback_line(t) + "\n"
    assert t.count(old) == 1, f"the manifest write-back has moved; found {t.count(old)}"
    return t.replace(old, "        pass  # write-back removed\n", 1)


# Only the persistence guard is named. The endpoint guard drives `_run_stage` against a
# manifest that ALREADY holds `stages`, so it reads the forwarding and not the write-back --
# naming it here would have been a control asserting a guard it cannot move.
case("driver: stage results are assembled and never persisted to the manifest",
     _DRIVER, m180,
     [f"{_D}::test_a_completed_stages_results_persist_to_the_manifest"])


# 181. The write-back becomes a blind put of the driver's own copy. Every specialist prompt
#      tells the agent to append its results to this same object and the harness role really
#      can (S3PipelineObjects grants PutObject on runs/*), so the driver is the SECOND
#      writer -- a blind put erases whatever the agent wrote during the turn. It also puts
#      the signed blocks back under the driver's control, which is bugs #9/#20/#21's shape.
def m181(t):
    old = '    current["stages"] = manifest.get("stages", {})\n'
    assert t.count(old) == 1, f"the narrowed stages write has moved; found {t.count(old)}"
    return t.replace(old, "    current = manifest\n", 1)


case("driver: the manifest write-back is a blind put, not a narrowed merge",
     _DRIVER, m181,
     [f"{_D}::test_a_stage_write_cannot_restate_the_signed_blocks",
      f"{_D}::test_a_concurrent_agent_write_survives_the_drivers_stage_write"])


# 182. An absent manifest is manufactured instead of refused: a stages-only document with no
#      plan, no approval and no models reads downstream as "a run nobody planned".
def m182(t):
    old = ("    if not current:\n"
           "        # Nothing to merge into.")
    assert t.count(old) == 1, f"the absent-manifest refusal has moved; found {t.count(old)}"
    return t.replace(old, "    if False:\n        # Nothing to merge into.", 1)


case("driver: a stage write manufactures a manifest for a run nobody planned",
     _DRIVER, m182,
     [f"{_D}::test_a_stage_write_with_no_manifest_to_merge_into_is_refused"])


# 183. The prior-stage facts are no longer carried into the stage payload -- bug #22's
#      consumer half. eval and monitor read `params.student_endpoint` for an endpoint the
#      deploy stage created and named; nothing else can supply it, since no plan can be
#      signed with an endpoint name that does not exist yet.
def m183(t):
    old = _payload_merge_line(t)
    assert t.count(old) == 1, f"the params injection has moved; found {t.count(old)}"
    return t.replace(old, '        payload["params"] = {**signed, **approved, '
                          '**payload["params"]}', 1)


# The derived guard is NOT named here: it checks the `STAGE_FACT_PARAMS` declaration, and
# this mutation breaks the WIRING that reads it. Two different halves, so two controls --
# m185 below moves the declaration.
case("driver: prior-stage facts are dropped from the stage payload",
     _DRIVER, m183,
     [f"{_D}::test_the_endpoint_a_deploy_stage_created_reaches_the_stages_that_measure_it"])


# 184. `stage_fact_params` defaults instead of omitting -- the bug #21 shape one layer down.
#      A guessed endpoint name is worse than an absent one: the monitor stage reports
#      CloudWatch metrics for something that is not the model under test, and a metric
#      attributed to the wrong endpoint reads as evidence rather than as a gap.
def m184(t):
    old = "        if value:\n            out[param] = str(value)"
    assert t.count(old) == 1, f"the stage-fact omission has moved; found {t.count(old)}"
    return t.replace(
        old, '        out[param] = str(value or f"llmops-{param}-latest")', 1)


case("driver: an unreported stage fact is guessed instead of omitted",
     _DRIVER, m184,
     [f"{_D}::test_a_stage_fact_the_run_never_produced_is_omitted_not_guessed"])


# 185. The declaration half: `STAGE_FACT_PARAMS` no longer claims student_endpoint, so the
#      derived guard must notice that a param two prompts read has no writer left. This is
#      the mutation m183 CANNOT make -- m183 breaks the wiring that reads this constant,
#      and the derived guard reads the constant itself. Bug #21 lost two controls to exactly
#      this confusion, so the declaration and the wiring get one control each.
def m185(t):
    old = '    "student_endpoint": ("deploy", "endpoint_name"),\n'
    assert t.count(old) == 1, f"the stage-fact declaration has moved; found {t.count(old)}"
    return t.replace(old, "", 1)


case("driver: the stage-fact declaration drops the param two prompts read",
     _DRIVER, m185,
     [f"{_D}::test_every_param_a_prompt_reads_has_something_that_writes_it",
      f"{_D}::test_the_endpoint_a_deploy_stage_created_reaches_the_stages_that_measure_it"])


_DATAPREP = "agents/data-prep/harness.json"
_EVAL = "agents/eval/harness.json"
_C = "tests/test_orchestration.py::TestTheCustomersOwnDataIsActuallyRead"

# 186. Bug #23 restored EXACTLY: the generate bullet reverts to the pre-cure sentence that
#      self-instructs from params.domain and never names params.source_uri. The param stays
#      in `audit`'s bullet, which is what made this survive so long -- a file-level grep for
#      source_uri was green, and the broad "some dispatched task reads it" guard passes too,
#      because audit IS dispatched. Only the full-path intersection can see it: audit's only
#      Next is Complete, so the mode that reads the customer's file cannot train and the mode
#      that trains cannot read the file. Both halves correct, never connected.
def m186(t):
    old = re.search(r'- \\"generate\\":.*?run\'s S3 prefix\.', t, re.S)
    assert old, "the generate bullet no longer matches; re-anchor this mutation"
    return t.replace(
        old.group(0),
        '- \\"generate\\": produce seed prompts per llm-prompt-engineering self-instruct '
        'patterns for the domain in params.domain, invoke the teacher model via '
        "'aws bedrock-runtime converse' (model id in params.teacher_model_id) in batches, "
        'strip <think>...</think> reasoning blocks keeping final answers (unless '
        "params.keep_reasoning), write distillation/generated.jsonl to the run's S3 prefix.",
        1)


case("prompt: the only data-prep task on the full path stops reading the customer's data",
     _DATAPREP, m186,
     [f"{_C}::test_the_full_path_and_not_only_the_audit_reads_the_source_uri",
      f"{_C}::test_the_generate_task_prefers_customer_data_over_inventing_it"])

# 187. The subtler half of #23, and the one a reviewer would wave through: generate still
#      NAMES params.source_uri, but self-instruction leads and the customer's file becomes
#      the second option rather than the branch. A model reading two eligible instructions
#      takes the first, and choosing wrong produces no error -- the corpus is the right size,
#      the schema validates, and every downstream artifact agrees. So mentioning the param is
#      not the contract; stated precedence is. This is why the precedence guard exists
#      separately from the reachability one: m186 cannot make this mutation, because deleting
#      the param entirely is a different (and louder) defect.
def m187(t):
    old = ('  * If params.source_uri is present, THE CUSTOMER\'S OWN DATA IS THE CORPUS')
    assert t.count(old) == 1, f"the customer-data branch has moved; found {t.count(old)}"
    fallback = '  * ONLY if params.source_uri is absent entirely: produce seed prompts per '
    assert t.count(fallback) == 1, "the fallback branch has moved; re-anchor this mutation"
    # Swap the leading marker so self-instruct reads as the primary branch and the
    # customer-data branch as the alternative, with no wording deleted anywhere.
    t = t.replace(fallback, '  * Produce seed prompts per ', 1)
    return t.replace(old, '  * Alternatively params.source_uri may be present, in which '
                          "case the customer's own data can be the corpus", 1)


case("prompt: self-instruction is offered ahead of the customer's data instead of after it",
     _DATAPREP, m187,
     [f"{_C}::test_the_generate_task_prefers_customer_data_over_inventing_it"])

# 188. Eval reverts to scoring the 10% val split unconditionally. The broad guard stays green
#      here on data-prep's curate alone, which reads customer_eval_uri to decontaminate the
#      training corpus -- a real use, and precisely the wrong one to be satisfied by: the
#      acceptance set would be excluded from training and then never measured against, so the
#      gate reports agreement with the TEACHER on rows the customer never chose. Two
#      defensible halves whose pairing is the bug, which is the shape this whole class tracks.
def m188(t):
    old = re.search(r'- \\"evaluate\\":.*?next run\.', t, re.S)
    assert old, "the evaluate bullet no longer matches; re-anchor this mutation"
    return t.replace(
        old.group(0),
        '- \\"evaluate\\": prepare the student-vs-teacher comparison on the held-out prompt '
        'set (the 10% val split from distillation/curated.jsonl).', 1)


case("prompt: the gate goes back to scoring the val split whatever the plan named",
     _EVAL, m188,
     [f"{_C}::test_the_scoring_task_anchors_the_gate_to_the_customers_acceptance_set"])

# 189. Eval keeps the customer's set but drops the word that ranks the two: with "Fall back
#      to ... only when no customer set was named" gone, both sets read as eligible and which
#      one a run scored is decided per-turn by the model. A gate whose evaluation set varies
#      between runs cannot be compared across runs, and nothing in the report would say so.
def m189(t):
    old = ('Fall back to the 10% val split from distillation/curated.jsonl only when no '
           'customer set was named, and say in the report which set was used')
    assert t.count(old) == 1, f"the fallback ranking has moved; found {t.count(old)}"
    return t.replace(old, 'The 10% val split from distillation/curated.jsonl is also '
                          'available, and say in the report which set was used', 1)


case("prompt: the val split stops being labelled the fallback and becomes an equal option",
     _EVAL, m189,
     [f"{_C}::test_the_scoring_task_anchors_the_gate_to_the_customers_acceptance_set"])


_DRV = "tests/test_orchestration.py::TestDriver"

# 190. Bug #24 restored: the stop_reason check applies to inline functions again. This is the
#      exact pre-cure line, and the reason it looked right is that it IS right for
#      code_interpreter and shell -- the harness services those itself, so answering one
#      would make the next ConverseStream invalid. The half never connected: an inline
#      function is BY DEFINITION one the harness cannot service, so a call arriving with
#      end_turn is not "already serviced", it is a call nobody will ever answer. Live:
#      run-20260810T174626Z-3f08b4c6 died MissingStageComplete at DataPrepGenerate with 300
#      verified customer rows already in S3 -- a stage_complete that was CALLED, reported as
#      never called.
def m190(t):
    old = re.search(r'\n        if tu and out\["stop_reason"\] != "tool_use" and tu'
                    r'\.get\("name"\) in SERVICED_TOOLS:.*?\n            out = '
                    r'\{\*\*out, "stop_reason": "tool_use"\}\n', t, re.S)
    assert old, "the inline-function override no longer matches; re-anchor this mutation"
    return t.replace(old.group(0), "\n", 1)


case("driver: an inline function riding with end_turn is discarded and counted as prose",
     _DRIVER, m190,
     [f"{_DRV}::test_a_stage_complete_riding_with_end_turn_is_serviced_not_discarded",
      f"{_DRV}::test_a_rejected_courtesy_ack_cannot_un_complete_a_settled_stage"])

# 191. The override survives but SERVICED_TOOLS loses one name, so exactly one tool keeps the
#      old behaviour. Chosen as job_launched because its discard is the quietest of the
#      eleven: the job is running on SageMaker and billing, the token is parked, and the
#      driver has decided the turn was prose -- so the stage fails while the GPU it launched
#      keeps going. A per-tool version of the same bug is what a hand-kept second copy of the
#      dispatch table produces the first time an agent gains a tool, which is why the set is
#      derived from the branches rather than trusted.
def m191(t):
    old = '                            "job_launched", "publish_cost_report"'
    assert t.count(old) == 1, f"the serviced-tool set has moved; found {t.count(old)}"
    return t.replace(old, '                            "publish_cost_report"', 1)


case("driver: the serviced-tool set drops one name the dispatch still has a branch for",
     _DRIVER, m191,
     [f"{_DRV}::test_the_serviced_tool_set_matches_the_dispatch_branches"])

# 192. The ack goes back to a bare _invoke on the stage_complete branch. Nothing about the
#      happy path changes -- the token is settled, the outputs verified, the return value
#      identical -- so every test that does not reject an ack stays green. The bug is one
#      state further on than #24's: the call is now serviced, and answering it re-invokes a
#      runtime with no open toolUse, which rejects. Raising there reports a stage that
#      genuinely finished as a crashed one, and the state machine sees a settled token AND an
#      invocation error for the same stage.
def m192(t):
    old = ('                _ack_terminal(c, event, sess, tu, {"status": "acknowledged"},\n'
           '                              "the task token was settled and the outputs '
           'verified")\n')
    assert t.count(old) == 1, f"the stage_complete ack has moved; found {t.count(old)}"
    return t.replace(old,
                     '                _invoke(c["agentcore"], event["harness_id"], sess,\n'
                     '                        _tool_result_content(tu, '
                     '{"status": "acknowledged"}),\n'
                     '                        event.get("qualifier"))\n', 1)


case("driver: a rejected courtesy ack raises after the token is already settled",
     _DRIVER, m192,
     [f"{_DRV}::test_a_rejected_courtesy_ack_cannot_un_complete_a_settled_stage"])

# 193-195. Bug #25: bug #22's cure was inert in production for three days because the write
#      it added was granted to no role. These three controls cover the two halves of the cure
#      plus the hazard splitting them opened.
#
# 193. The IAM statement is deleted, restoring the exact production state: the driver has
#      s3:PutObject (on reports/*) and writes runs/<run_id>/manifest.json. So an
#      action-level check stays green -- the action IS granted -- and only a KEY-level one
#      can see it. That is why the guard derives every key the driver's source can put
#      rather than asserting on actions: the previous guard even listed
#      runs/r/manifest.json as forbidden, which was true when written and became a green pin
#      on a live defect the moment _save_manifest existed.
_ROLES = "deploy/iam/lambda_roles.json"


def m193(t):
    d = json.loads(t)
    stmts = d["roles"]["driver"]["permissionsPolicy"]["Statement"]
    keep = [s for s in stmts if s.get("Sid") != "WriteStageResultsToRunManifest"]
    assert len(keep) == len(stmts) - 1, \
        "WriteStageResultsToRunManifest is not in the driver role; re-anchor this mutation"
    d["roles"]["driver"]["permissionsPolicy"]["Statement"] = keep
    return json.dumps(d, indent=2)


case("IAM: the driver loses PutObject on the manifest it writes on every stage_complete",
     _ROLES, m193,
     ["tests/test_orchestration.py::test_every_s3_key_the_driver_writes_is_inside_a_granted_prefix"])

# 194. The grant widens from runs/*/manifest.json to runs/*, which is what "just make it
#      work" produces. Every write the driver performs still succeeds, so no functional test
#      can see it -- and the driver becomes able to rewrite distillation/curated.jsonl and
#      evaluation/report.json, the artifacts it head_objects to decide whether a stage's
#      token settles. A role that can rewrite what it verifies can launder its own evidence,
#      which is the property the narrow scope exists for.
def m194(t):
    old = '"arn:aws:s3:::<DATA_BUCKET>/runs/*/manifest.json"'
    assert t.count(old) == 1, f"the manifest grant has moved; found {t.count(old)}"
    return t.replace(old, '"arn:aws:s3:::<DATA_BUCKET>/runs/*"', 1)


case("IAM: the driver's manifest grant widens to every object under runs/",
     _ROLES, m194,
     ["tests/test_orchestration.py::test_the_driver_role_can_write_the_report_the_driver_always_writes"])

# 195. The two writes go back into one try block, manifest first -- the shape that made one
#      refused PutObject delete a second, permitted artifact. Nothing on the happy path
#      changes, because when both writes succeed the two forms are indistinguishable; only a
#      test that refuses ONE key can tell them apart.
def m195(t):
    # The whole span from the manifest write through the report branch is replaced, not just
    # the two call lines: the second `except` and the `else` belong to structure this
    # mutation removes, and leaving them orphaned is a SyntaxError -- pytest then exits 4
    # (collection error) rather than 1, and the runner reports the guard as uncaught while
    # the mutation it claims to have made was never really applied. A mutation that cannot
    # produce importable code proves nothing about the guard.
    writeback = _manifest_writeback_line(t)
    old = re.search(
        re.escape(writeback) + '\n'
        r'    except Exception.*?'
        r'\n        print\(f"\[driver\] report SKIPPED for \{run_id\}/\{stage\}: '
        r'\{failures\[-1\]\}"\)\n', t, re.S)
    assert old, "the split stage-artifact writes no longer match; re-anchor this mutation"
    return t.replace(old.group(0), (
        writeback + '\n'
        '        write_run_report(c["s3"], os.environ["DATA_BUCKET"], manifest)\n'
        '    except Exception as exc:  # noqa: BLE001\n'
        '        failures.append(f"{type(exc).__name__}: {exc}")\n'
        '        print(f"[driver] canonical report FAILED for {run_id}/{stage}: '
        '{failures[-1]}")\n'), 1)


case("driver: a refused manifest write again suppresses the permitted report write",
     _DRIVER, m195,
     [f"{_D}::test_a_refused_manifest_write_still_publishes_the_run_report"])

# 196. The run_id guard around the report write is dropped. Only reachable BECAUSE the writes
#      were split: an unloadable manifest used to abort before the report, and now it does
#      not, so report_key_for("") resolves to the run-latest alias and a document describing
#      nothing overwrites the last real run's published report. The cure for one bug creating
#      the next is the thing negative controls exist to notice.
def m196(t):
    old = '    if manifest.get("run_id"):\n'
    assert t.count(old) == 1, f"the report run_id guard has moved; found {t.count(old)}"
    return t.replace(old, '    if True:\n', 1)


case("driver: a report for an unreadable manifest overwrites the run-latest alias",
     _DRIVER, m196,
     [f"{_D}::test_an_unreadable_manifest_does_not_overwrite_the_published_report_alias"])

# 197-200. Bug #26: the deadline handoff needed a chunk in order to notice that no chunk was
#      coming. `out_of_wall` is evaluated once per stream chunk, so a stream that goes QUIET
#      reaches it never; boto's read_timeout restarts per read, so after a chunk at elapsed t
#      it is next due at t + 870 -- past the 900s wall for every t > 30. A last chunk anywhere
#      in (30, 855)s left both escape hatches unreachable and the runtime hard-killed the
#      invocation with the agent's stage_complete unanswered.
#
# 197. The watchdog is removed, restoring the exact production state. Every existing deadline
#      test stays green, because they all use TricklingStream -- a stream that keeps arriving,
#      which is the only case out_of_wall can see. Only a test that BLOCKS can tell them
#      apart, which is why the new one opens a real socket instead of using a double: a fake
#      whose __next__ returns cannot express "nothing returns".
def m197(t):
    old = "        with _stream_watchdog(remaining_ms):\n"
    assert t.count(old) == 1, f"the watchdog wrapper has moved; found {t.count(old)}"
    return t.replace(old, "        if True:  # watchdog removed\n", 1)


case("driver: a stream that goes quiet at the wall is hard-killed instead of handed off",
     _DRIVER, m197,
     [f"{_DRV}::test_a_stream_that_goes_quiet_at_the_wall_still_hands_off"])

# 198. The watchdog's exception is relabelled as a stream DEATH rather than a deadline cut.
#      Both paths recover, so the stage still finishes -- what is lost is the one same-session
#      salvage retry, spent on a turn that never failed, leaving a REAL death later in the
#      same stage unprotected. A functional test that only asserts "the run completed" cannot
#      see a reserve being quietly drained.
def m198(t):
    old = "    except _StreamWatchdogFired as exc:\n"
    assert t.count(old) == 1, f"the watchdog except clause has moved; found {t.count(old)}"
    # Fall through to the generic handler, which stringifies it as a death.
    return t.replace(old, "    except (_StreamWatchdogFired,) if False else ():\n", 1)


case("driver: a deadline cut is mislabelled a stream death and burns the salvage retry",
     _DRIVER, m198,
     [f"{_DRV}::test_a_quiet_stream_is_a_deadline_cut_not_a_stream_death"])

# 199. The ValueError guard around signal.signal is dropped. On the main thread nothing
#      changes -- every production invocation and every other test still passes -- but any
#      caller off the main thread now crashes a turn that would have succeeded. A guard that
#      converts a working path into a failure is worse than the hang it prevents.
def m199(t):
    old = ("        except ValueError:  # not the main thread — degrade, do not fail\n"
           "            yield\n"
           "            return\n")
    assert t.count(old) == 1, f"the off-main-thread guard has moved; found {t.count(old)}"
    return t.replace(old, "        except ValueError:\n            raise\n", 1)


case("driver: a watchdog that cannot be armed fails the turn instead of degrading",
     _DRIVER, m199,
     [f"{_DRV}::test_a_watchdog_that_cannot_arm_does_not_kill_the_turn"])

# 200. The heartbeat's by-design refusal goes back to being reported as a failure. Nothing
#      functional changes -- the beat was never going to be written for a non-run invocation
#      -- so only a test that reads the LOG can see it. It matters because 11 lines of
#      "heartbeat write failed" describing correct behaviour is how the line that would mean
#      "the beat is actually broken" stopped meaning anything.
def m200(t):
    old = ("            if _is_condition_failure(exc):\n"
           "                _beat_liveness()  # not a run row: the beat belongs in __liveness__\n"
           "                return\n")
    assert t.count(old) == 1, f"the heartbeat condition check has moved; found {t.count(old)}"
    return t.replace(old, "", 1)


case("driver: every triage heartbeat is logged as a failure for refusing by design",
     _DRIVER, m200,
     [f"{_DRV}::test_a_triage_heartbeat_is_refused_by_design_and_says_nothing"])

# 201-202. Bug #27: `outputs` sent as a JSON STRING skipped verification entirely.
#      `verify_outputs` head_objects every element that startswith("s3://"); a one-element
#      list holding the text '["s3://a", "s3://b"]' starts with '[', so nothing inside it was
#      ever checked and the stage passed verification having proved nothing. `metrics` had
#      had the JSON-string parse since the contract was written -- outputs, the field with a
#      security consequence, did not.

# 201. The list parse is removed, restoring production: the string is wrapped as a single
#      element again. Every other normalize test stays green because they all pass a real
#      list or a bare URI -- the two shapes that were never broken.
def m201(t):
    old = ("        if isinstance(parsed, list):\n"
           "            outputs = parsed\n")
    assert t.count(old) == 1, f"the outputs list-parse has moved; found {t.count(old)}"
    return t.replace(old, "        if False:\n            outputs = parsed\n", 1)


case("contracts: outputs sent as a JSON string bypass s3 verification entirely",
     _REPORT, m201,
     [f"{_CON}::test_outputs_sent_as_a_json_string_are_still_verified"])

# 202. The JSON-scalar unwrap is removed. '"s3://b/x"' keeps its leading quote, which also
#      fails startswith("s3://") -- the identical vacuous check one layer down. Fixing only
#      the list case would have left this reachable, and no functional test can see it
#      because the run still completes either way.
def m202(t):
    old = ("        elif isinstance(parsed, str):\n"
           "            outputs = [parsed]\n")
    assert t.count(old) == 1, f"the outputs scalar-unwrap has moved; found {t.count(old)}"
    return t.replace(old, "        elif False:\n            outputs = [parsed]\n", 1)


case("contracts: a JSON-quoted single URI keeps its quote and is never verified",
     _REPORT, m202,
     [f"{_CON}::test_a_json_quoted_single_uri_is_unwrapped_before_verification"])

# 203-207. Bug #28: the model TYPED the call instead of making one, so no structured reader
#      could see it. #24's sibling and its opposite: there the runtime emitted a block the
#      driver discarded, here the runtime emitted no block at all. One control per branch of
#      the cure, because each branch is a different way to get this wrong -- and two of them
#      (204, 205) fail SAFE-looking, i.e. the run still completes while the driver believes
#      something it never verified.
#
# 203. The dispatch hook is deleted, restoring production exactly: a turn whose text is
#      `<invoke name="job_launched">...` is prose. Live cost: rehearsal
#      run-20260811T005043Z-320cc47e launched SageMaker job
#      llmops-qlora-...-i0, confirmed it InProgress, wrote the manifest, then typed the
#      signal twice -- stage failed MissingStageComplete while the job ran on to Completed
#      (442 billable seconds) with no parked token, so nothing would ever have settled it.
#      Measured: `tool=job_launched` appears ZERO times in 25 days of driver logs.
def m203(t):
    old = re.search(r'\n        if not tu:\n            typed = parse_typed_call'
                    r'\(out\["text"\]\).*?\n                out = \{\*\*out, '
                    r'"stop_reason": "tool_use"\}\n', t, re.S)
    assert old, "the typed-call dispatch hook no longer matches; re-anchor this mutation"
    return t.replace(old.group(0), "\n", 1)


case("driver: a call the model typed instead of made is invisible and counted as prose",
     _DRIVER, m203,
     [f"{_DRV}::test_a_typed_job_launched_parks_the_token_instead_of_failing_the_stage",
      f"{_DRV}::test_a_typed_stage_complete_still_has_to_prove_its_outputs_exist"])

# 204. The `serviced` gate is dropped, so ANY name in `<invoke name="...">` is recovered.
#      This is the mutation that looks like a simplification and is the security bug: shell
#      and code_interpreter run INSIDE the harness, so a typed one is either a transcript of
#      a call already served or text quoted from a log, a customer ticket, or a training
#      sample -- and the driver would then dispatch prose. Nothing functional goes red; only
#      a test that asserts a refusal can see it.
def m204(t):
    old = ("        if name not in serviced:\n"
           "            continue\n")
    assert t.count(old) == 1, f"the serviced-name gate has moved; found {t.count(old)}"
    return t.replace(old, "        if False:\n            continue\n", 1)


case("driver: a typed shell/code_interpreter call is recovered and dispatched from prose",
     _DRIVER, m204,
     [f"{_DRV}::test_a_typed_shell_call_is_never_recovered"])

# 205. The parameter JSON parse is removed, so a typed `outputs` arrives as the literal text
#      '["s3://a", "s3://b"]'. That is bug #27's exact shape one layer up: the string starts
#      with '[', so verify_outputs skips it and every URI inside passes unchecked. The run
#      still completes, which is what makes this the worst of the five -- the driver reports
#      verified outputs it never head_object'd.
def m205(t):
    old = ("            try:\n"
           "                args[pm.group(1)] = json.loads(raw)\n")
    assert t.count(old) == 1, f"the typed-parameter parse has moved; found {t.count(old)}"
    return t.replace(old, "            try:\n                args[pm.group(1)] = raw\n", 1)


case("driver: a typed JSON list stays text, so verify_outputs skips every URI in it",
     _DRIVER, m205,
     [f"{_DRV}::test_a_typed_outputs_list_arrives_as_a_list_not_as_text"])

# 206. The null-id branch in _tool_result_content is dropped, so a recovered call is answered
#      with an assistant toolUse echo carrying toolUseId=None plus a matching toolResult. The
#      runtime has no such pending call and rejects the pair -- and on a NON-terminal branch
#      (checkpoint, a rejected stage_complete) that rejection kills the next turn rather than
#      a courtesy message, converting the recovery into a different stage failure.
def m206(t):
    old = re.search(r'    if not tool_use\.get\("toolUseId"\):\n        return _user_text'
                    r'\(.*?\n(?=    return \[)', t, re.S)
    assert old, "the recovered-call text branch no longer matches; re-anchor this mutation"
    return t.replace(old.group(0), "", 1)


case("driver: a recovered call is echoed back with a null toolUseId the runtime rejects",
     _DRIVER, m206,
     [f"{_DRV}::test_a_recovered_call_is_answered_as_text_not_as_a_tool_result"])

# 207. The `if not tu` precondition widens to unconditional, so a recovered call OVERRIDES a
#      real one in the same turn. A turn that narrates one call and structurally makes
#      another then gets serviced on the narration -- the driver acting on what the agent
#      talked about instead of what it did, which is the whole failure mode the `serviced`
#      gate in 204 exists to bound, arriving by a different door.
def m207(t):
    old = "        if not tu:\n            typed = parse_typed_call"
    assert t.count(old) == 1, f"the typed-call precondition has moved; found {t.count(old)}"
    return t.replace(old, "        if True:\n            typed = parse_typed_call", 1)


case("driver: a typed call in the same turn's text displaces the real tool call",
     _DRIVER, m207,
     [f"{_DRV}::test_a_real_tool_call_always_wins_over_a_typed_one"])


#: Where the pristine text of the file currently mutated is parked, so a kill -9 -- which
#: no handler can intercept -- still leaves the original recoverable. Under the repo root
#: rather than /tmp because it must be obvious to whoever finds the tree dirty, and
#: .gitignore'd so it can never be committed. The `finally` deletes it on the normal path,
#: so its mere existence IS the "a run died mid-case" signal.
JOURNAL = REPO / ".negative_control_journal"


def _restore_from_journal():
    """Undo a mutation left behind by a run that died before its ``finally``.

    Runs at import, before any case executes, because the damage a leak does is not to the
    run that leaked -- it is to the NEXT run, which mutates an already-mutated file and then
    reports on code nobody wrote. Restoring first makes the harness self-healing instead of
    compounding, and it prints what it did: a silent repair would hide that a previous run
    was killed, which is itself worth knowing.
    """
    if not JOURNAL.exists():
        return
    saved = json.loads(JOURNAL.read_text())
    target = REPO / saved["path"]
    if target.read_text() != saved["text"]:
        target.write_text(saved["text"])
        print(f"RECOVERED  a previous run died mid-case and left {saved['path']} mutated; "
              "restored it from the journal before starting")
    JOURNAL.unlink()


def _die_on_signal(signum, _frame):
    """Turn a terminating signal into an exception so the restore ``finally`` actually runs.

    Raising from the handler is the point: the default disposition for SIGTERM/SIGINT/SIGHUP
    terminates the process without unwinding the stack, so `finally` never executes and the
    mutation stays on disk. ``KeyboardInterrupt`` is deliberate rather than a custom class --
    it propagates through the loop exactly like a Ctrl-C already does, a path this runner has
    always handled correctly, so signalled and interrupted become one code path instead of
    two. SIGKILL cannot be caught at all, which is what the journal is for.
    """
    raise KeyboardInterrupt(f"terminated by signal {signum}")


#: Everything below mutates tracked files, so it runs ONLY as a script. Importing this
#: module has to be safe and side-effect-free, because the fast anchor check in
#: test_docs_claims.py imports it to call every ``mutate`` against in-memory text -- the
#: only way a drifted anchor gets named in under a second instead of waiting for a 5-minute
#: run nobody does on every commit. Four anchors had gone stale and the suite was green.
#: The guard also has to cover the module-level setup: installing signal handlers from an
#: imported module would clobber pytest's, and ``_restore_from_journal`` DELETES the journal,
#: so an import racing a killed run would throw away the only copy of a mutated file's
#: original text. This file is still not a pytest module and still is not collected -- the
#: name does not match ``test_*``.
if __name__ == "__main__":
    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(_sig, _die_on_signal)
    _restore_from_journal()

failed = []
for name, rel, mutate, tests in (CASES if __name__ == "__main__" else ()):
    p = REPO / rel
    orig = p.read_text()
    # A drifted anchor raises out of `mutate`, and that assert is deliberate -- silently
    # skipping a case whose pattern no longer matches is the no-op failure below. But it
    # must fail ONE case, not the run: uncaught, it terminated the loop, so m70's anchor
    # going stale (README "6 Lambdas" -> 7, corrected two PRs earlier, mutation left
    # behind) meant cases 70-143 never executed at all while the harness exited non-zero
    # for a single named reason. 74 controls silently unverified, and the exit code looked
    # like the one honest failure it printed. Every case now gets its turn and every
    # broken anchor is named in the same summary.
    try:
        new = mutate(orig)
    except Exception as exc:
        print(f"SKIP-BROKEN  {name}: mutation raised {type(exc).__name__}: {exc} "
              "(guard NOT verified)")
        failed.append(f"{name} / anchor drifted: {exc}")
        continue
    if new == orig:
        print(f"SKIP-BROKEN  {name}: patch was a no-op (guard NOT verified)")
        failed.append(name)
        continue
    # Journal BEFORE mutating, never after: a crash in the window between the two would
    # otherwise leave a mutated file with no record of what it used to be.
    JOURNAL.write_text(json.dumps({"path": rel, "text": orig, "case": name}))
    p.write_text(new)
    for cache in REPO.glob("**/__pycache__"):
        if ".venv" not in str(cache):
            shutil.rmtree(cache, ignore_errors=True)
    try:
        results = [(t, *run(t)) for t in tests]
    finally:
        p.write_text(orig)
        JOURNAL.unlink(missing_ok=True)
    for t, rc, last in results:
        ok = rc == PYTEST_TESTS_FAILED
        why = "" if ok else f"  [pytest exit {rc}, wanted {PYTEST_TESTS_FAILED}]"
        print(f"{'PASS' if ok else 'FAIL'}  {name}{why}\n      -> {t.split('::')[-1]}: {last}")
        if not ok:
            failed.append(f"{name} / {t}")

if __name__ == "__main__":
    print()
    print("all guards caught their break" if not failed else f"UNCAUGHT: {failed}")
    sys.exit(1 if failed else 0)
