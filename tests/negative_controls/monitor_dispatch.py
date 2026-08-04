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
import importlib.util, json, os, pathlib, shutil, signal, subprocess, sys

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
    return t.replace("| Lambdas ×6 |", "| Lambdas ×5 |")
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
#     then takes the bus event, the stage event and the token settle with it. And SNS is the
#     channel with a KNOWN-ZERO audience (llmops-escalations has no subscribers live), so
#     this makes the one channel that reaches nobody the gate on the two that work.
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
    old = "state machine + 6 Lambdas"
    assert old in t, "the README's Lambda line has moved; re-anchor this mutation"
    return t.replace(old, "state machine + 5 Lambdas", 1)


case("docs: the English README states 5 Lambdas again while the deployer deploys 6",
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
    old = "(driver / start / resume / webhook / finops / monitor-sweep)"
    assert old in t, "the README's Lambda name list has moved; re-anchor this mutation"
    return t.replace(old, "(driver / start / resume / webhook / finops)", 1)


case("docs: the README's Lambda list omits monitor-sweep while the count still reads 6",
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
    # Anchored on the call at column 0, which is the invocation; the def and the reference
    # inside this docstring are both indented or quoted. Same self-mutation hazard as m83:
    # an unanchored "_restore_from_journal()" matches three places in this file.
    old = "\n_restore_from_journal()\n"
    assert t.count(old) == 1, (
        f"expected exactly one top-level recovery call, found {t.count(old)}; this case "
        "mutates its own file, so an anchor that matches itself would test nothing")
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
    """Drop the real-account-id rule, keeping only the structural patterns.

    The subtle half of m114. `AKIA…` and `arn:aws:…` are structural and would still fire, so
    a reviewer skimming for "do the high-signal rules run on binaries?" sees yes. But the
    generic 12-digit rule is text-only, so REAL_ACCOUNT_IDS is the ONLY thing catching this
    account's bare id in a binary -- delete it and the single most important string in the
    repo's threat model is unguarded in exactly the files nobody reads.
    """
    old = '    for pat_name, pat in ((("this repo\'s own account id"), re.compile(\n'
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


#: The README's embedded walkthrough video. Three cases, all on the TEXT side of the guard,
#: because this runner mutates the text of one existing tracked file and journals that path for
#: recovery -- it cannot swap a 10 MB binary.
#:
#: The binary directions were verified BY HAND instead, by building deliberately broken mp4s
#: with ffmpeg and driving each one past tests/test_intro_video.py. EVERY row below was re-run
#: with ffprobe removed from PATH, because that is the configuration CI runs in and a guard that
#: only fails on a laptop does not gate anything:
#:   4 KB truncation           -> "video is only 4000 bytes" (plus the moov/mdat check)
#:   -an, 4 KB                 -> "video is only ... bytes"
#:   -an, FULL LENGTH          -> "video has NO audio track — the narration was not muxed in"
#:   audio track only 3s       -> "video track is 304.68s but the audio track is 3.07s"
#:   scale=640:360             -> "recorded at 640x360, but the stage is authored at 1180x664"
#:   scale=640:360 setsar=59/32-> same; the coded size is read from the sample entry, not tkhd
#:   setsar=2/1 (coded intact) -> "coded 1180x664 but displayed as 2360x664"
#:   no +faststart             -> "moov comes after mdat"
#:   -t 240 (cut short)        -> "video is 240.08s but the narration plus tail is 304.72s"
#:
#: Four real guard defects were found that way rather than reasoned about:
#:   1. the moov check raised ValueError instead of asserting, on a truncated file;
#:   2. the mp3 fallback assumed MPEG-1 while Polly emits MPEG-2, so it reported 11.7s for
#:      303.8s of audio -- a wrong answer in the right units, the kind that survives review;
#:   3. the audio-stream and frame-size assertions sat behind skipif(ffprobe), and CI has no
#:      ffmpeg, so a FULL-LENGTH SILENT film passed the entire module (7 passed, 3 skipped) on
#:      the machine that gates merges. Both now read the container directly;
#:   4. the frame-size check read tkhd, which is DISPLAY geometry: a 640x360 frame tagged
#:      SAR 59:32 reports width 1180 -- the authored width exactly -- so a video with a third
#:      of the pixels would have passed. Measured, not hypothesised. It now reads the coded size
#:      from the sample entry and separately asserts the pixels are square.
#: Recorded here because the next person to widen this runner should know these directions are
#: covered, and by what.
def m120(t):
    """Delete the video reference from the EN README, leaving 10 MB nobody can reach.

    The direction that actually happens: someone rewrites the top of the README months from
    now and the embed goes with it. Nothing breaks, no link 404s, the file just stops being
    reachable and keeps costing every clone.
    """
    old = "**[▶ Play the five-minute walkthrough](docs/media/intro-en.mp4)**"
    assert t.count(old) == 1, f"the EN play link has moved; found {t.count(old)}"
    return t.replace(old, "**Play the five-minute walkthrough**", 1)


case("readme: the embedded walkthrough stops being reachable from the EN README",
     "README.md", m120,
     ["tests/test_intro_video.py::test_both_readmes_reach_the_video_and_the_live_page"])


def m121(t):
    """Strip the five-language pointer from the zh-TW README, leaving only the English mp4.

    This is the case that makes the guard's last assertion earn its place. The mp4 is English
    only; `deploy/console/intro/` is where the other four narrations are. Drop that pointer and
    a Cantonese or Korean reader is left with an English video and no hint the rest exists.

    It also pins a real defect this control found: the assertion was first written
    `"/intro" in text`, which the video's own path `docs/media/intro-en.mp4` satisfies -- so
    this exact mutation PASSED. (The pointer was then an absolute URL to the live console; that
    address was removed from the repo entirely, because publishing the front door of a page that
    launches runs and approves budgets is not something a README should do. The assertion moved
    to the in-repo path, which is where the narrations actually are.)
    """
    old = "`deploy/console/intro/`"
    assert t.count(old) == 1, f"the zh-TW narration pointer has moved; found {t.count(old)}"
    return t.replace(old, "那個動畫頁面", 1)


case("readme: the zh-TW walkthrough section loses the five-language narration pointer",
     "README.zh-TW.md", m121,
     ["tests/test_intro_video.py::test_both_readmes_reach_the_video_and_the_live_page"])


def m121b(t):
    """Put a <video> tag back into the EN README.

    The direction that will actually happen: someone sees a poster-image link, assumes an
    inline player is possible, and "improves" it into `<video src=...>`. GitHub's sanitizer
    deletes the tag, so the section renders with a blank gap where the player was meant to be
    and the reader is told nothing. Measured against GitHub's own POST /markdown, not assumed --
    six embed forms, all erased.
    """
    old = "[![Watch the five-minute walkthrough](docs/media/intro-poster.png)]" \
          "(docs/media/intro-en.mp4)"
    assert t.count(old) == 1, f"the EN poster link has moved; found {t.count(old)}"
    return t.replace(old, '<video src="docs/media/intro-en.mp4" controls></video>', 1)


case("readme: a <video> tag GitHub silently deletes is added back to the EN README",
     "README.md", m121b,
     ["tests/test_intro_video.py::test_both_readmes_reach_the_video_and_the_live_page"])


def m122(t):
    """Reintroduce a budget figure beside the player, in Chinese.

    The reporting reference is whatever each team sets, so no amount belongs in material that
    describes the product. The walkthrough section is the likeliest place for one to come back:
    it is the part that summarises what the video shows, and "it flags runs over $X" reads like
    a helpful specific rather than like this platform's own test setting.

    zh-TW rather than EN on purpose -- a `$`-shaped pattern would miss 兩萬 entirely, so the
    half of the guard that has to understand Chinese numerals is the half worth breaking.
    """
    old = "沒人看著的閒置 endpoint"
    assert t.count(old) == 1, f"the zh-TW scene summary has moved; found {t.count(old)}"
    return t.replace(old, "超過兩萬美元基準的 run、沒人看著的閒置 endpoint", 1)


case("readme: a budget amount reappears next to the player (zh-TW)",
     "README.zh-TW.md", m122,
     ["tests/test_intro_video.py::test_the_video_section_names_no_budget_amount"])


def m123(t):
    """Re-author the stage at a different size and leave the committed recording behind.

    The one binary-adjacent direction this runner CAN drive, because the mutation is in text:
    the guard derives the authored size from `.stage` in page.template.html rather than
    retyping it, so widening the stage without re-recording must fail. That derivation is the
    point -- record_video.py keeps its own STAGE_W/STAGE_H copy, and a guard that compared the
    video against the RECORDER's number would stay green while both drifted away from the page
    the scenes are actually laid out in.
    """
    old = "width:1180px; height:664px"
    assert t.count(old) == 1, f"the .stage size declaration has moved; found {t.count(old)}"
    return t.replace(old, "width:1280px; height:720px", 1)


case("intro: the stage is re-authored larger and the committed video is not re-recorded",
     "deploy/console/intro/page.template.html", m123,
     ["tests/test_intro_video.py"
      "::test_the_video_carries_an_audio_stream_and_the_authored_frame_size"])


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


for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(_sig, _die_on_signal)
_restore_from_journal()

failed = []
for name, rel, mutate, tests in CASES:
    p = REPO / rel
    orig = p.read_text()
    new = mutate(orig)
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

print()
print("all guards caught their break" if not failed else f"UNCAUGHT: {failed}")
sys.exit(1 if failed else 0)
