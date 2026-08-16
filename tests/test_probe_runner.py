"""Guards for the protocol-reliability probe.

This tool is unusual in the repo: running it SPENDS MONEY, one GPU endpoint per slot. So the
tests here are not only about correctness, they are about the two ways a spending tool
betrays the person who authorized it -- by charging for more runs than the confidence it
reports actually needs, and by charging twice for one slot. `runs_needed` and the dispatch
ledger get a test each for exactly that reason, and both totals are pinned as literals
because a silent edit to `PROBE_UNIT_COST_USD` changes what a human agreed to spend.

The third failure mode is scoring: a probe that counts a run it could not read as a pass
would report stability it never observed. `outcome_of` never guesses upward, and
`test_a_run_whose_outcome_is_unknown_is_not_counted_as_a_pass` is the tripwire.

Nothing here reaches AWS -- conftest.py's autouse fixture would turn a slip into an
AssertionError naming the call -- and every dispatch test is `--dry-run` or driven through
fakes.
"""

import ast
import json
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import probe_protocol_reliability as probe  # noqa: E402

ARTIFACT = "s3://llmops-agentic-123456789012-us-east-1/runs/run-r6e/model.tar.gz"
STUDENT = "Qwen/Qwen3-8B"
START = probe._parse_iso("2026-08-20T00:00:00Z")


def console_wilson():
    """The console's own `_wilson`, lifted out of the deployed bundle by AST.

    Not imported: deploy/console/lambda_function.py builds ~12 clients and calls
    sts:GetCallerIdentity at import time (see tests/test_console_cost.py). Compiling the one
    function definition is also the stricter comparison -- it pins THAT SOURCE, so an edit
    to the console's formula cannot hide behind a shim.
    """
    src = (REPO / "deploy" / "console" / "lambda_function.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_wilson")
    ns = {"math": math}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "console_wilson", "exec"), ns)
    return ns["_wilson"]


# ── fakes ───────────────────────────────────────────────────────────────────
class FakeLambda:
    """Records every invoke and hands back a run_id, like llmops-start-pipeline does."""

    def __init__(self, boom=False):
        self.calls, self.boom = [], boom

    def invoke(self, **kw):
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("TooManyRequestsException")
        rid = f"run-2026082{len(self.calls)}T000000Z-probe{len(self.calls)}"
        body = json.dumps({"run_id": rid, "manifest_uri": "s3://b/m.json",
                           "execution_arn": "arn:aws:states:::execution/x"})
        return {"Payload": _Body(body)}


class _LedgerWatchingLambda(FakeLambda):
    """A FakeLambda that reads the ledger off disk DURING the invoke.

    The only way to assert an ordering rather than an outcome: every state a re-run can
    observe is identical whether the slot was recorded before or after its invoke, and the
    difference between them is exactly one crash window wide.
    """

    def __init__(self, path):
        super().__init__()
        self.path, self.seen = path, []

    def invoke(self, **kw):
        on_disk = json.loads(Path(self.path).read_text())
        self.seen.append([bool(s.get("attempted_at")) for s in on_disk["slots"]])
        return super().invoke(**kw)


class _Body:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text.encode()


def _run_id_of(cond):
    """Pull the run_id out of a boto3 Key condition, so the fake can answer per run."""
    parts = list(cond.get_expression()["values"])
    for part in parts:
        expr = part.get_expression()
        if expr["operator"] == "=":
            return expr["values"][1]
    raise AssertionError("no equality on the partition key")


class FakeTable:
    def __init__(self, name, rows, events, boom=()):
        self.name, self.rows, self.events, self.boom = name, rows, events, set(boom)
        self.queries = []

    def get_item(self, Key):  # noqa: N803 — boto3's own casing
        rid = Key["run_id"]
        if rid in self.boom:
            raise RuntimeError("AccessDeniedException")
        item = self.rows.get(rid)
        return {"Item": item} if item else {}

    def query(self, **kw):
        rid = _run_id_of(kw["KeyConditionExpression"])
        self.queries.append(rid)
        return {"Items": list(self.events.get(rid, []))}


class FakeDDB:
    def __init__(self, rows=None, events=None, boom=()):
        self.tables = {}
        self.rows, self.events, self.boom = rows or {}, events or {}, boom

    def Table(self, name):  # noqa: N802 — boto3's own casing
        return self.tables.setdefault(
            name, FakeTable(name, self.rows, self.events, self.boom))


def protocol_row(run_id, stage, task="generate", epoch=0, turns=5, serviced=4):
    return {"run_id": run_id, "sk": f"{probe.driver.PROTOCOL_SK}{stage}#{task}#e{epoch}",
            "detail": json.dumps({"stage": stage, "task": task, "epoch": epoch}),
            "turns": turns, "serviced_turns": serviced, "prose_turns": turns - serviced,
            "filtered_turns_total": 0, "recovered_typed_calls": 0,
            "recovered_ending_in_prose": 0, "infra_error_turns_total": 0,
            "deadline_cuts": 0}


def state(n=2, spread_days=7.0, target=0.80):
    return probe.new_state(n, target, probe.DEFAULT_ALPHA, spread_days,
                           ARTIFACT, STUDENT, START)


def dispatched(n=2, statuses=("completed", "completed")):
    """A ledger whose slots all went out, plus the run rows those slots produced."""
    st = state(n)
    rows = {}
    for i, slot in enumerate(st["slots"]):
        rid = f"run-probe-{i}"
        slot.update({"attempted_at": probe._iso(START), "run_id": rid,
                     "dispatched_at": probe._iso(START)})
        if i < len(statuses) and statuses[i] is not None:
            rows[rid] = {"run_id": rid, "status": statuses[i],
                         "pipeline_mode": "deploy_only"}
    return st, rows


def rc(st, rows=None, events=None, boom=(), out=None):
    summary = probe.collect(st, {"ddb": FakeDDB(rows, events, boom)})
    return probe.report(summary, out=out or (lambda *a: None)), summary


# ── the sample size, and what it costs ──────────────────────────────────────
def test_fourteen_runs_are_what_eighty_percent_at_ninety_five_costs():
    """The headline number, derived and priced. Both literals are the authorization."""
    assert probe.runs_needed(0.80) == 14
    assert probe.cost_usd(14) == 7.42
    lines = []
    assert probe.cmd_plan(_Args(target=0.80, alpha=0.05), out=lines.append) == 0
    assert any("14 runs, $7.42" in ln for ln in lines), lines


def test_twenty_nine_runs_are_what_ninety_percent_costs():
    assert probe.runs_needed(0.90) == 29
    assert probe.cost_usd(29) == 15.37


def test_the_sample_size_rounds_up_and_never_to_nearest():
    """13.43 runs is 14, not 13.

    `round` would return 13, and 0.80**13 = 0.055 -- a 5.5% chance of a clean sweep from a
    system that fails one run in five, quoted as 95% confidence. The whole exercise would
    then claim slightly more than it bought, in the direction nobody checks.
    """
    exact = math.log(0.05) / math.log(0.80)
    assert 13 < exact < 14 and round(exact) == 13
    assert probe.runs_needed(0.80) == 14
    assert 0.80 ** 14 <= 0.05 < 0.80 ** 13


def test_a_target_of_one_is_refused_rather_than_priced():
    """No finite number of successes proves a rate of 1.0, so there is no price to quote."""
    with pytest.raises(ValueError, match="unfalsifiable"):
        probe.runs_needed(1.0)
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            probe.runs_needed(bad)
    assert probe.main(["plan", "--target", "1.0"]) == 3


def test_the_unit_cost_is_the_measured_one():
    """A named constant with a source, not a guess folded into a total."""
    assert probe.PROBE_UNIT_COST_USD == 0.53
    src = Path(probe.__file__).read_text()
    assert "2026-08-15" in src.split("PROBE_UNIT_COST_USD")[0][-1200:]


# ── dispatch ────────────────────────────────────────────────────────────────
class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_a_dispatch_without_a_student_is_refused():
    """DEFAULT_MODELS would silently supply a 1.7B base for an 8B artifact.

    The refusal is start_pipeline's own `_check_mode_prerequisites`, called here rather than
    restated, so a mode that grows a requirement gets it enforced by this tool the same day.
    """
    payload = probe.dispatch_payload(ARTIFACT, None, 1, 14)
    with pytest.raises(ValueError, match="explicitly named student"):
        probe.refuse_undispatchable(payload)
    lam = FakeLambda()
    st = state(1)
    st["student"] = None
    with pytest.raises(ValueError):
        probe.launch(st, None, {"lambda": lam}, START, dry_run=False, out=lambda *a: None)
    assert lam.calls == []
    assert probe.main(["launch", "--artifact", ARTIFACT, "--dry-run"]) == 3


def test_a_dispatch_without_an_artifact_is_refused():
    """deploy_only enters at Deploy: there is no finetune stage to infer the artifact from."""
    with pytest.raises(ValueError, match="model_artifact_uri"):
        probe.refuse_undispatchable(probe.dispatch_payload("", STUDENT, 1, 14))


def test_the_payload_names_the_mode_the_probe_prices():
    """A probe that dispatched `full` would be measuring something else, at another price."""
    payload = probe.dispatch_payload(ARTIFACT, STUDENT, 3, 14)
    assert payload["trigger_source"] == "protocol-probe"
    assert payload["params"]["pipeline_mode"] == "deploy_only" == probe.PIPELINE_MODE
    assert payload["params"]["model_artifact_uri"] == ARTIFACT
    assert payload["params"]["models"] == {"student": STUDENT}
    assert payload["params"]["note"] == "protocol probe 3/14"
    probe.refuse_undispatchable(payload)          # the real check accepts it


def test_dry_run_makes_no_aws_call(tmp_path, capsys):
    """The default in tests and CI. It must not even build a client."""
    ledger = tmp_path / "probe.json"
    assert probe.main(["launch", "--artifact", ARTIFACT, "--student", STUDENT,
                       "--state", str(ledger), "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "[dry-run]" in printed and "llmops-start-pipeline" in printed
    assert "slot 14/14" in printed                # the whole schedule, not just what is due
    assert "$7.42 total" in printed
    assert "nothing was dispatched" in printed
    assert not ledger.exists(), "a dry run must not leave a ledger claiming slots were used"


def test_a_slot_is_never_dispatched_twice(tmp_path):
    """The ledger is the guard, and it is written BEFORE the invoke.

    Re-running `launch` is the expected operating mode -- fourteen runs spread over a week
    means cron or a human comes back to it -- so "dispatch what is due" must be idempotent
    per slot. Ordering matters more than it looks: recording the attempt after the invoke
    would turn any crash in between into a second charge for the same sample.
    """
    ledger = str(tmp_path / "probe.json")
    st = state(3, spread_days=3.0)
    probe.save_state(ledger, st)
    lam = _LedgerWatchingLambda(ledger)
    later = START + __import__("datetime").timedelta(days=1, hours=1)

    assert probe.launch(st, ledger, {"lambda": lam}, later, False, out=lambda *a: None) == 2
    assert len(lam.calls) == 2                    # slots 1 and 2 are due, slot 3 is not
    # The ORDER, which is the half of this guard a re-run cannot show: at the moment each
    # invoke was made, the ledger ON DISK already said that slot had been attempted. A
    # ledger written after the invoke leaves a crash window in which money was spent and
    # nothing recorded it, and the next run charges for the same sample again.
    assert lam.seen == [[True, False, False], [True, True, False]], (
        f"the ledger read mid-invoke was {lam.seen}: the slot being dispatched must "
        "already be marked attempted on disk before its invoke goes out")

    again = probe.load_state(ledger)              # a fresh process, same ledger
    assert probe.launch(again, ledger, {"lambda": lam}, later, False,
                        out=lambda *a: None) == 0
    assert len(lam.calls) == 2, "a due slot that already went out was dispatched again"
    assert [s["run_id"] for s in again["slots"]][:2] == [s["run_id"] for s in st["slots"]][:2]
    assert again["slots"][2]["run_id"] is None


def test_an_invoke_that_never_confirmed_is_not_retried_and_not_a_pass():
    """A charge we cannot attribute is a lost sample, deliberately. Losing one is cheap."""
    st = state(1)
    lam = FakeLambda(boom=True)
    assert probe.launch(st, None, {"lambda": lam}, START, False, out=lambda *a: None) == 0
    slot = st["slots"][0]
    assert slot["attempted_at"] and slot["run_id"] is None and "TooManyRequests" in slot["error"]
    assert probe.due_slots(st, START) == []
    outcome, why = probe.outcome_of(None, slot)
    assert outcome == "unknown" and "never confirmed" in why


def test_the_schedule_spreads_the_runs_instead_of_bursting_them():
    """Fourteen runs in one minute measure one minute, not the system."""
    slots = probe.slot_schedule(14, 7.0, START)
    assert len(slots) == 14
    assert slots[0]["due_at"] == "2026-08-20T00:00:00Z"
    assert slots[1]["due_at"] == "2026-08-20T12:00:00Z"
    assert slots[-1]["due_at"] == "2026-08-26T12:00:00Z"
    assert probe.due_slots({"slots": slots}, START) == [slots[0]]


# ── scoring ─────────────────────────────────────────────────────────────────
def test_a_run_whose_outcome_is_unknown_is_not_counted_as_a_pass():
    """Still running, no row, unreadable row: three unknowns, none of them stability.

    This is the rule tools/audit_landed.py and tools/audit_drift.py already hold -- a check
    that cannot answer must not report clean -- applied to money. A probe that scored an
    unreadable run as a pass would report a rate it never observed, and the sweep would look
    complete.
    """
    st, rows = dispatched(3, ("completed", "running", None))
    code, summary = rc(st, rows)
    assert summary["passes"] == 1 and summary["failures"] == 0 and summary["unknown"] == 2
    assert [s["outcome"] for s in summary["slots"]] == ["pass", "unknown", "unknown"]
    assert "still running" in summary["slots"][1]["why"]
    assert "no run row" in summary["slots"][2]["why"]
    assert code == 2, "unknown outcomes must not exit 0"

    st, rows = dispatched(1, ("completed",))
    code, summary = rc(st, rows, boom=[st["slots"][0]["run_id"]])
    assert summary["unknown"] == 1 and code == 2
    assert "AccessDenied" in summary["slots"][0]["why"]


def test_an_escalated_run_is_a_failure_not_a_pass():
    """`escalated` is the terminal state MissingStageComplete produces.

    It is the exact outcome this probe exists to count. Reading it as anything but a failure
    would make the tool report the bug it is measuring as evidence against itself.
    """
    assert "escalated" in probe.TERMINAL_STATES and probe.PASS_STATE == "completed"
    st, rows = dispatched(2, ("completed", "escalated"))
    code, summary = rc(st, rows)
    assert summary["passes"] == 1 and summary["failures"] == 1
    assert code == 1, "a failed probe run refutes the claim"


def test_a_clean_sweep_shorter_than_the_target_needs_does_not_exit_zero():
    """Two clean runs are two clean runs, not 95% confidence in 0.80."""
    st, rows = dispatched(2, ("completed", "completed"))
    lines = []
    code, summary = rc(st, rows, out=lines.append)
    assert summary["passes"] == 2 and summary["failures"] == 0 and summary["unknown"] == 0
    assert summary["needed"] == 14
    assert code == 2 and any("14 are needed" in ln for ln in lines), lines


def test_a_full_clean_sweep_is_the_only_zero():
    st, rows = dispatched(14, tuple(["completed"] * 14))
    code, summary = rc(st, rows)
    assert summary["passes"] == 14 and code == 0
    assert summary["spent_usd"] == 7.42


def test_the_exit_code_table_is_the_documented_one():
    """Every code in the docstring, produced. 3 is covered by the refusal tests above."""
    table = {0: tuple(["completed"] * 14), 1: ("failed",) + tuple(["completed"] * 13),
             2: ("running",) + tuple(["completed"] * 13)}
    for expected, statuses in table.items():
        st, rows = dispatched(14, statuses)
        code, _ = rc(st, rows)
        assert code == expected, statuses[0]
    doc = probe.__doc__
    for code in (0, 1, 2, 3):
        assert f"\n    {code}   " in doc


def test_the_per_stage_rate_comes_from_the_protocol_rows():
    """The actionable number, and it is the driver's own rollup rather than a second copy.

    Run level answers "is it stable"; per stage answers "why not". At 0.84 per stage a
    five-stage lane is 42%, so the run-level number is a CONSEQUENCE of this one -- which is
    why both are printed and why this one is broken out per stage.
    """
    st, rows = dispatched(2, ("completed", "completed"))
    ids = [s["run_id"] for s in st["slots"]]
    events = {ids[0]: [protocol_row(ids[0], "deploy", turns=10, serviced=8),
                       protocol_row(ids[0], "smoke", turns=5, serviced=5)],
              ids[1]: [protocol_row(ids[1], "deploy", turns=10, serviced=9)]}
    lines = []
    code, summary = rc(st, rows, events, out=lines.append)
    assert summary["turns"] == 25 and summary["structured_call_rate"] == 22 / 25
    assert summary["per_stage"]["deploy"] == {
        "turns": 20, "structured_call_rate": 17 / 20,
        "interval": probe.wilson(17 / 20, 20)}
    assert summary["per_stage"]["smoke"]["structured_call_rate"] == 1.0
    assert any("per stage:" in ln for ln in lines)
    assert summary["structured_call_rate"] == \
        probe.driver.protocol_rollup(sum(events.values(), []))["structured_call_rate"]


def test_no_protocol_rows_is_reported_as_unavailable_not_as_zero():
    """Before this PR's driver is deployed there are no such rows.

    A rate of 0.0000 would read as "no turn ever ended in a call", which is a catastrophic
    claim about the system rather than an honest statement about the instrument.
    """
    st, rows = dispatched(2, ("completed", "completed"))
    lines = []
    code, summary = rc(st, rows, out=lines.append)
    assert summary["turns"] == 0 and summary["structured_call_rate"] is None
    assert summary["call_interval"] is None and summary["per_stage"] == {}
    text = "\n".join(lines)
    assert "no protocol# rows found" in text and "0.0000" not in text


def test_the_wilson_interval_matches_the_consoles():
    """One formula, two deployed copies, pinned to 1e-12 -- the DIRECTIVE_SK precedent.

    The console is its own bundle and this is a repo tool, so there is nowhere shared to put
    the function. What keeps a duplicate honest is a test that compares them, so an edit to
    either side turns this red instead of manufacturing a borderline out of rounding.
    """
    theirs = console_wilson()
    for score, n in [(1.0, 1), (1.0, 14), (0.0, 14), (0.5, 2), (80 / 95, 95),
                     (0.8421, 95), (17 / 20, 20), (1 / 3, 3), (0.999, 1000)]:
        mine, ref = probe.wilson(score, n), theirs(score, n)
        assert mine is not None and ref is not None
        assert abs(mine[0] - ref[0]) < 1e-12 and abs(mine[1] - ref[1]) < 1e-12, (score, n)
    assert probe.wilson(1.0, 0) is None and theirs(1.0, 0) is None
    #: The interval the 1/1 rehearsal actually earned -- the reason this tool exists.
    low, high = probe.wilson(1.0, 1)
    assert round(low, 3) == 0.207 and round(high, 3) == 1.0
    assert probe.Z_95 == 1.96


def test_the_protocol_query_asks_only_for_protocol_rows():
    """A run's partition also holds stage events, checkpoints and the heartbeat.

    Reading them and filtering in Python would work and would also pay for every timeline row
    of every probe run, so the prefix belongs in the KeyConditionExpression.
    """
    st, rows = dispatched(1, ("completed",))
    ddb = FakeDDB(rows, {})
    probe.collect(st, {"ddb": ddb})
    table = ddb.tables[probe.EVENTS_TABLE]
    assert table.queries == [st["slots"][0]["run_id"]]
    src = Path(probe.__file__).read_text()
    body = src.split("def _protocol_rows")[1].split("\ndef ")[0]
    assert "begins_with(driver.PROTOCOL_SK)" in body
    assert "LastEvaluatedKey" in body, "one page of rows is not the run"


def test_an_undispatched_ledger_scores_nothing_and_claims_nothing():
    st = state(14)
    code, summary = rc(st, {})
    assert summary["dispatched"] == 0 and summary["passes"] == 0
    assert summary["spent_usd"] == 0.0 and summary["run_rate"] is None
    assert summary["run_interval"] is None
    assert code == 2


def test_collect_without_a_ledger_is_a_usage_error(tmp_path):
    assert probe.main(["collect", "--state", str(tmp_path / "nope.json")]) == 3
