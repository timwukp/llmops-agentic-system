"""Tests for the held-out gate: the step that turns `verified` from a tautology
into a measurement.

A distilled solver marked `verified: true` reproduces the training pairs that were
in its own prompt. The generator saw those pairs, so that verdict cannot fail for
the reason anyone cares about. Measured on the real ARC training corpus (n=742),
8.8% of shown-pair-verified solvers are wrong programs, and 14.7% of those
repaired after being told which pair mismatched. `build_heldout_source.py` executes
each solver against the ARC *test* pairs -- which its author never saw -- and drops
the ones that fail.

Everything here is designed around one hazard: a gate that cannot say no is
indistinguishable from a corpus that needs no gating. Both look like 100%. So each
test below pins the DISAGREEMENT between the old verdict and the new one, or feeds
the gate a verifier that lies and checks that it notices.

Run: .venv/bin/python -m pytest tests/test_heldout_gate.py -q
"""
from __future__ import annotations

import json
import math
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline/v2"))
sys.path.insert(0, str(REPO / "tools"))

import build_heldout_source as gate  # noqa: E402


# ── the duplicated Wilson formula is allowed only while a test pins it ─────────

# k, n. Includes the degenerate ends (0/n and n/n), where the interval is the
# whole point -- 0/5 must not read as "0% and certain" -- and the n values the
# reports actually use.
_WILSON_TABLE = [(0, 1), (1, 1), (0, 5), (5, 5), (1, 3), (677, 742), (439, 463),
                 (238, 279), (848, 849), (100, 151), (2, 1000)]


def test_the_wilson_interval_matches_the_probe_tools():
    """A third copy of the formula, permitted by the repo's precedent only on the
    condition that a test compares it against an existing one and turns red the day
    they disagree. Compared unrounded: at 4dp two different formulas can agree by
    coincidence on small n."""
    import probe_protocol_reliability as probe

    for k, n in _WILSON_TABLE:
        mine = gate.wilson(k, n)
        theirs = probe.wilson(k / n, n)
        assert mine is not None and theirs is not None, (k, n)
        assert abs(mine[0] - theirs[0]) < 1e-12, (k, n, mine, theirs)
        assert abs(mine[1] - theirs[1]) < 1e-12, (k, n, mine, theirs)


def test_that_comparison_could_fail():
    """Two calls to the same formula agree trivially. What makes the test above
    worth its runtime is that a wrong z, or the textbook normal-approximation
    interval it is often confused with, lands outside 1e-12."""
    k, n = 677, 742
    mine = gate.wilson(k, n)
    assert abs(mine[0] - probe_wilson_at(k, n, z=1.64)[0]) > 1e-3
    p = k / n
    normal_lo = p - 1.96 * math.sqrt(p * (1 - p) / n)
    assert abs(mine[0] - normal_lo) > 1e-4, \
        "the Wilson lower bound must not coincide with the normal approximation"


def probe_wilson_at(k: int, n: int, z: float):
    import probe_protocol_reliability as probe
    return probe.wilson(k / n, n, z)


def test_no_sample_gives_no_interval():
    assert gate.wilson(0, 0) is None
    assert gate.wilson95(0, 0) is None, \
        "a report must show an absent interval, not [0.0, 0.0]"


def test_the_report_form_rounds_and_the_measured_form_does_not():
    """Rounding inside `wilson` would cap the cross-check above at its own
    precision, so the split is load-bearing rather than cosmetic."""
    assert gate.wilson95(677, 742) == [round(x, 4) for x in gate.wilson(677, 742)]
    assert any(x != round(x, 4) for x in gate.wilson(677, 742))


# ── the gate must be able to say no ───────────────────────────────────────────

def test_the_self_check_passes_against_the_real_sandbox():
    checks = gate.gate_self_check()
    assert checks == {"accepts_correct": True, "rejects_corruption": True,
                      "rejects_infinite_loop": True}, checks


def test_the_self_check_catches_a_verifier_that_never_says_no(monkeypatch):
    """The failure this exists for: a permissive verifier -- a sandbox whose
    SIGALRM never fires, a comparison that coerces types, an exception swallowed
    into a pass -- reports every solver held-out-correct, and the gate becomes an
    expensive no-op that reads as good news about the corpus.

    The double LIES (always all_pass) rather than echoing the real verifier,
    because a double that computes the right answer leaves the branch that matters
    unreachable and the test vacuous."""
    monkeypatch.setattr(gate, "verify_code",
                        lambda code, pairs, t=5: {"all_pass": True, "n_pass": len(pairs),
                                                  "n_pairs": len(pairs),
                                                  "fail_reason": None})
    checks = gate.gate_self_check()
    assert checks["accepts_correct"] is True, \
        "a liar still gets this one right -- which is why it is not the assertion"
    assert checks["rejects_corruption"] is False
    assert checks["rejects_infinite_loop"] is False


def test_the_self_check_catches_a_verifier_that_never_says_yes(monkeypatch):
    """The other direction: a gate that rejects everything empties the corpus, and
    `main()` must not read that as "the teacher was bad"."""
    monkeypatch.setattr(gate, "verify_code",
                        lambda code, pairs, t=5: {"all_pass": False, "n_pass": 0,
                                                  "n_pairs": len(pairs),
                                                  "fail_reason": "stub"})
    checks = gate.gate_self_check()
    assert checks["accepts_correct"] is False
    assert checks["rejects_corruption"] is True


def test_the_corruption_control_is_one_character_from_the_correct_solver():
    """If the two self-check programs differed structurally, the control would
    prove only that the verifier rejects garbage. One operator apart is the
    distance a real overfit solver sits at."""
    a, b = gate.SELF_CHECK_GOOD, gate.SELF_CHECK_CORRUPT
    assert len(a) == len(b)
    assert sum(1 for x, y in zip(a, b) if x != y) == 1


# ── ARC held-out pairs: all of them, or the gate has a hole ───────────────────

def _write_arc(tmp_path, tasks: dict, solutions: dict):
    ch = tmp_path / "challenges.json"
    so = tmp_path / "solutions.json"
    ch.write_text(json.dumps(tasks))
    so.write_text(json.dumps(solutions))
    return str(ch), str(so)


def test_every_test_pair_is_carried_not_just_the_first(tmp_path):
    """69 of the 1,000 ARC training tasks have more than one test pair. Gating on
    the first would pass a solver that handles one unseen input and not the other,
    which is precisely the shape of a rule that is nearly right."""
    ch, so = _write_arc(
        tmp_path,
        {"aaa": {"train": [], "test": [{"input": [[1]]}, {"input": [[2]]}]}},
        {"aaa": [[[2]], [[4]]]})
    heldout = gate.load_arc_heldout(ch, so)
    assert heldout["aaa"] == [{"input": [[1]], "output": [[2]]},
                              {"input": [[2]], "output": [[4]]}]


def test_files_that_disagree_on_pair_count_stop_the_build(tmp_path):
    """Pairing by index across two files that disagree verifies against the wrong
    grid, and every downstream number stays plausible."""
    ch, so = _write_arc(
        tmp_path,
        {"aaa": {"train": [], "test": [{"input": [[1]]}, {"input": [[2]]}]}},
        {"aaa": [[[2]]]})
    with pytest.raises(SystemExit) as e:
        gate.load_arc_heldout(ch, so)
    assert "aaa" in str(e.value) and "disagree" in str(e.value)


def test_a_task_with_no_published_solution_is_skipped_not_faked(tmp_path):
    ch, so = _write_arc(tmp_path,
                        {"aaa": {"train": [], "test": [{"input": [[1]]}]},
                         "bbb": {"train": [], "test": [{"input": [[9]]}]}},
                        {"aaa": [[[2]]]})
    assert set(gate.load_arc_heldout(ch, so)) == {"aaa"}


def test_provenance_is_optional_and_reads_both_result_shapes(tmp_path):
    assert gate.load_provenance(None) == {}
    p = tmp_path / "distill_results.json"
    p.write_text(json.dumps({"results": [{"task_id": "aaa", "rounds_used": 3,
                                          "code": RIGHT},
                                         {"task_id": "bbb"}]}))
    assert gate.load_provenance(str(p)) == {"aaa": (3, RIGHT)}
    p.write_text(json.dumps([{"task_id": "ccc", "rounds_used": 1, "code": RIGHT}]))
    assert gate.load_provenance(str(p)) == {"ccc": (1, RIGHT)}


# ── the round count belongs to a program, not to a task_id ────────────────────

def test_the_round_count_is_only_used_when_it_describes_this_solver():
    """`task_id` is not a key for this join. A corpus is assembled over several
    distillation passes, and a later pass can replace a task's solver while keeping
    its id -- which leaves the recorded round count describing a program that is no
    longer in the corpus.

    Measured on the real files: only 676 of 848 rows carry the code their provenance
    entry describes, and among the entries recording `rounds_used: 10` just 6 of 154
    do. Joined on task_id alone, those 148 rows reported `10 -> 154/154 = 100%`,
    which sits in the report as evidence against the dose-response the same module
    documents -- a wrong join reads exactly like a finding."""
    prov = {"aaa": (3, RIGHT), "ccc": (7, None)}
    assert gate.repair_rounds_for({"task_id": "aaa", "code": RIGHT}, prov) == 3
    assert gate.repair_rounds_for({"task_id": "aaa", "code": LIAR}, prov) == gate.SUPERSEDED
    assert gate.repair_rounds_for({"task_id": "bbb", "code": RIGHT}, prov) == gate.UNKNOWN
    assert gate.repair_rounds_for({"task_id": "ccc", "code": RIGHT}, prov) == gate.SUPERSEDED


def test_superseded_and_unknown_are_not_the_same_answer():
    """They call for different work: `unknown` means nobody recorded the effort,
    `superseded` means someone did and it was spent on a different program. Collapsing
    them would hide a broken join inside a gap in the records."""
    assert gate.SUPERSEDED != gate.UNKNOWN


def test_a_superseded_tag_is_reported_and_kept_out_of_the_rates(tmp_path):
    proc, kept, _, report = _run_gate(
        tmp_path,
        [{"task_id": "aaa", "prompt": PROMPT, "code": RIGHT},
         {"task_id": "bbb", "prompt": PROMPT, "code": RIGHT}],
        _TASKS, _SOLUTIONS,
        # bbb's entry describes a solver that is not the one in the corpus.
        provenance={"results": [{"task_id": "aaa", "rounds_used": 2, "code": RIGHT},
                                {"task_id": "bbb", "rounds_used": 10, "code": LIAR}]})
    assert proc.returncode == 0, proc.stderr
    tags = {r["task_id"]: r["repair_rounds"] for r in kept}
    assert tags == {"aaa": 2, "bbb": "superseded"}
    assert report["provenance"] == {"rounds_known": 1, "superseded": 1, "unknown": 0,
                                    "note": report["provenance"]["note"]}
    assert "148" not in report["provenance"]["note"], \
        "the note must count THIS run, not quote the run that found the defect"
    assert "1 of 2 rows" in report["provenance"]["note"]
    assert "10" not in report["by_repair_rounds"], \
        "a stale round count must not appear as a bucket at all"
    assert report["by_repair_rounds"]["superseded"]["n"] == 1


# ── end to end: the lying double survives the old check and dies here ─────────

# Doubling. The lying double reproduces every SHOWN pair via a lookup table that
# happens to cover the values it was shown -- runnable, plausible, and wrong.
RIGHT = "def transform(grid):\n    return [[c * 2 for c in row] for row in grid]\n"
LIAR = ("def transform(grid):\n"
        "    table = {1: 2, 2: 4, 3: 6}\n"
        "    return [[table.get(c, c) for c in row] for row in grid]\n")
# Right on the first unseen input, wrong on the second: only a gate that checks
# every pair rejects it.
HALF_RIGHT = ("def transform(grid):\n"
              "    if grid == [[4]]:\n"
              "        return [[8]]\n"
              "    return grid\n")
SHOWN_PAIRS = [{"input": [[1, 2]], "output": [[2, 4]]},
               {"input": [[3]], "output": [[6]]}]
PROMPT = ("Solve this ARC task.\n\nTraining pair 1:\nInput (1x2):\n1 2\n"
          "Output (1x2):\n2 4\n\nTraining pair 2:\nInput (1x1):\n3\n"
          "Output (1x1):\n6\n")


def _run_gate(tmp_path, rows, tasks, solutions, provenance=None):
    src = tmp_path / "source.jsonl"
    src.write_text("".join(json.dumps(r) + "\n" for r in rows))
    ch, so = _write_arc(tmp_path, tasks, solutions)
    out = tmp_path / "gated.jsonl"
    cmd = [sys.executable, str(REPO / "pipeline/v2/build_heldout_source.py"),
           "--source", str(src), "--challenges", ch, "--solutions", so,
           "--out", str(out), "--workers", "1"]
    if provenance:
        p = tmp_path / "distill_results.json"
        p.write_text(json.dumps(provenance))
        cmd += ["--provenance", str(p)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    kept = [json.loads(ln) for ln in out.read_text().splitlines()] \
        if out.exists() else []
    rej_p = tmp_path / "gated.jsonl.rejected.jsonl"
    rejected = [json.loads(ln) for ln in rej_p.read_text().splitlines()] \
        if rej_p.exists() else []
    rep_p = tmp_path / "gated.jsonl.report.json"
    report = json.loads(rep_p.read_text()) if rep_p.exists() else None
    return proc, kept, rejected, report


_TASKS = {"aaa": {"train": [], "test": [{"input": [[4]]}]},
          "bbb": {"train": [], "test": [{"input": [[4]]}]}}
_SOLUTIONS = {"aaa": [[[8]]], "bbb": [[[8]]]}


def test_the_wrong_solver_is_dropped_and_the_right_one_kept(tmp_path):
    proc, kept, rejected, report = _run_gate(
        tmp_path,
        [{"task_id": "aaa", "prompt": PROMPT, "code": RIGHT},
         {"task_id": "bbb", "prompt": PROMPT, "code": LIAR}],
        _TASKS, _SOLUTIONS,
        provenance={"results": [{"task_id": "aaa", "rounds_used": 1, "code": RIGHT},
                                {"task_id": "bbb", "rounds_used": 3, "code": LIAR}]})
    assert proc.returncode == 0, proc.stderr
    assert [r["task_id"] for r in kept] == ["aaa"]
    assert kept[0]["heldout_pairs"] == [{"input": [[4]], "output": [[8]]}]
    assert kept[0]["heldout_ok"] is True and kept[0]["n_heldout_pairs"] == 1
    assert kept[0]["repair_rounds"] == 1
    assert kept[0]["prompt"] == PROMPT and kept[0]["code"] == RIGHT, \
        "the gate filters rows; it must not rewrite them"

    assert [r["task_id"] for r in rejected] == ["bbb"]
    assert rejected[0]["fail_reason"], "a verdict without a reason is not evidence"
    assert rejected[0]["code"] == LIAR, \
        "the rejected solver's code is the evidence about the teacher; keep it"
    assert rejected[0]["repair_rounds"] == 3
    assert report["heldout_correct_rate"] == 0.5
    assert report["heldout_correct_wilson95"][0] < 0.5 < \
        report["heldout_correct_wilson95"][1]
    assert report["by_repair_rounds"]["1"]["rate"] == 1.0
    assert report["by_repair_rounds"]["3"]["rate"] == 0.0


def test_the_shown_pair_check_keeps_both_of_them(tmp_path):
    """The other half of the control, and the reason the gate is not redundant with
    the verification the corpus already carries: `verified: true` on row bbb is
    true. Re-running that check finds nothing wrong. Without this assertion the
    test above would still pass if `LIAR` were merely broken code, and would keep
    passing after the gate was replaced by a stricter shown-pair check."""
    from verify_sandbox import verify_code
    assert verify_code(RIGHT, SHOWN_PAIRS)["all_pass"] is True
    assert verify_code(LIAR, SHOWN_PAIRS)["all_pass"] is True
    assert verify_code(LIAR, [{"input": [[4]], "output": [[8]]}])["all_pass"] is False


def test_a_solver_right_on_one_unseen_input_and_wrong_on_another_is_dropped(tmp_path):
    proc, kept, rejected, report = _run_gate(
        tmp_path,
        [{"task_id": "aaa", "prompt": PROMPT, "code": HALF_RIGHT}],
        {"aaa": {"train": [], "test": [{"input": [[4]]}, {"input": [[5]]}]}},
        {"aaa": [[[8]], [[10]]]})
    assert proc.returncode == 1, "nothing survived, so this must not exit 0"
    assert kept == []
    assert rejected[0]["heldout_pairs_passed"] == 1
    assert rejected[0]["heldout_pairs_total"] == 2
    assert report["multi_test_tasks"] == 1


def test_an_empty_result_never_exits_zero(tmp_path):
    proc, kept, _, _ = _run_gate(
        tmp_path, [{"task_id": "bbb", "prompt": PROMPT, "code": LIAR}],
        _TASKS, _SOLUTIONS)
    assert proc.returncode == 1 and kept == []
    assert "must not exit 0" in proc.stderr


def test_a_corpus_the_arc_files_do_not_describe_is_refused(tmp_path):
    """A synthetic corpus has no ARC test pairs, so every row would be dropped for
    being unmatched rather than wrong -- and 0 gated of 0 matched is a rate of
    None, not a failure, unless this is checked."""
    proc, kept, _, _ = _run_gate(
        tmp_path, [{"task_id": "synth-0001", "prompt": PROMPT, "code": RIGHT}],
        _TASKS, _SOLUTIONS)
    assert proc.returncode == 1
    assert "no source task_id appears in the ARC files" in proc.stderr


def test_an_empty_source_is_refused_before_the_arc_files_are_read(tmp_path):
    proc, _, _, _ = _run_gate(tmp_path, [], _TASKS, _SOLUTIONS)
    assert proc.returncode == 1
    assert "read 0 rows" in proc.stderr


def test_missing_provenance_is_labelled_rather_than_defaulted(tmp_path):
    """147 of the 848 rows have no recorded repair count. Defaulting them to 0
    would put them in the clean bucket and flatten the dose-response that is the
    whole argument for gating."""
    proc, kept, _, report = _run_gate(
        tmp_path, [{"task_id": "aaa", "prompt": PROMPT, "code": RIGHT}],
        _TASKS, _SOLUTIONS)
    assert proc.returncode == 0
    assert kept[0]["repair_rounds"] == "unknown"
    assert "unknown" in report["by_repair_rounds"]
    assert "0" not in report["by_repair_rounds"]
