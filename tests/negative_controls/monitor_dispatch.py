"""Negative controls for #58: break one thing, confirm the matching guard fails, restore.

A guard nobody has ever seen fail is a guard nobody has tested. Every test in this repo's
suite asserts something about a file, and a test that reads the wrong file, greps a pattern
that no longer exists, or asserts a tautology passes exactly as loudly as one that works.
The only way to know a guard guards is to break what it guards and watch it go red.

Run it: ``.venv/bin/python tests/negative_controls/monitor_dispatch.py``

It is deliberately NOT a pytest module. Each case edits a tracked source file in place and
restores it in a ``finally``; collecting that alongside the suite it mutates would let a
crash mid-case leave the working tree broken and the next run's results meaningless.

Two lessons are baked in, both learned by this harness reporting a false result:

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
"""
import json, os, pathlib, shutil, subprocess, sys

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

failed = []
for name, rel, mutate, tests in CASES:
    p = REPO / rel
    orig = p.read_text()
    new = mutate(orig)
    if new == orig:
        print(f"SKIP-BROKEN  {name}: patch was a no-op (guard NOT verified)")
        failed.append(name)
        continue
    p.write_text(new)
    for cache in REPO.glob("**/__pycache__"):
        if ".venv" not in str(cache):
            shutil.rmtree(cache, ignore_errors=True)
    try:
        results = [(t, *run(t)) for t in tests]
    finally:
        p.write_text(orig)
    for t, rc, last in results:
        ok = rc == PYTEST_TESTS_FAILED
        why = "" if ok else f"  [pytest exit {rc}, wanted {PYTEST_TESTS_FAILED}]"
        print(f"{'PASS' if ok else 'FAIL'}  {name}{why}\n      -> {t.split('::')[-1]}: {last}")
        if not ok:
            failed.append(f"{name} / {t}")

print()
print("all guards caught their break" if not failed else f"UNCAUGHT: {failed}")
sys.exit(1 if failed else 0)
