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
