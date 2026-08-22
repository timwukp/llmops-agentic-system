"""Tests for the two numbers a training sign-off needs: seconds per step, and peak VRAM.

Run e1g6 burned 43 GPU-minutes and delivered nothing because its only save point sat past
its own MaxRuntime. That was catchable with multiplication -- and the multiplication needs
an input nothing in this repo measured. `validate_job_config.py` takes `--sec-per-it`, and
used to default it to 27.0: a number measured once, over 11 steps, on a different instance,
at max_length 4096, for a 1.7B model. At 14336 on a 4B model that is not even the right
order of magnitude to guess. Worse, the block that uses it was skipped outright when the
input was missing, and a skipped check prints "PASS -- safe to launch". Peak VRAM was
recorded nowhere at all, so "does max_length 14336 fit" had no answer short of a job that
OOMs an hour in.

So the trainer gained `--max_steps` (stop after N optimizer steps), per-step timing, and a
VRAM snapshot. These tests guard the three ways that instrumentation can lie:

  1. counting the wrong unit -- micro-batches instead of optimizer steps, which at
     gradient_accumulation=8 reports a runtime an eighth of the truth;
  2. a mean poisoned by the first step's CUDA autotune, which over the 20 steps a preflight
     can afford IS most of the measurement;
  3. a capped probe writing completed_fraction 1.0 and reading downstream as a finished run.

Run: .venv/bin/python -m pytest tests/test_preflight_measurement.py -q
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _mirrored_sourcedir() -> pathlib.Path:
    """The directory deploy/03_storage.py ensure_code() uploads -- derived, not written down.

    Same reasoning as tests/test_training_deliverability.py: a hardcoded path is how a
    guard stays green about a trainer no job downloads.
    """
    src = (REPO / "deploy/03_storage.py").read_text()
    body = src.split("def ensure_code(")[1].split("\ndef ")[0]
    parts = re.findall(r'"([a-z_0-9]+)"', body.split("src_dir =")[1].split("\n")[0])
    assert parts, "ensure_code no longer builds its source dir from literal path parts"
    d = REPO.joinpath(*parts)
    assert d.is_dir(), f"ensure_code mirrors {d}, which does not exist"
    return d


MIRRORED = _mirrored_sourcedir()
TRAINERS = sorted(REPO.glob("pipeline/training/**/train_qlora.py"))
assert TRAINERS, "no trainer found -- this file cannot guard anything"
trainer_src = (MIRRORED / "train_qlora.py").read_text()


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


validator = _load("validate_job_config_pf", MIRRORED / "validate_job_config.py")


def _extract(func_name: str):
    """Exec one top-level function out of the trainer in a bare namespace.

    train_qlora imports torch and trl at module scope; these tests must run on a laptop.
    """
    lines = trainer_src.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"def {func_name}("))
    end = start + 1
    while end < len(lines) and (not lines[end] or lines[end][0].isspace()):
        end += 1
    ns = {}
    exec("import glob, math, os, re\nWARMUP_STEPS_EXCLUDED = 3\n" + "\n".join(lines[start:end]), ns)
    return ns[func_name]


intended_steps = _extract("intended_steps")
completed_fraction = _extract("completed_fraction")
step_timing = _extract("step_timing")
vram_snapshot = _extract("vram_snapshot")

# The v2 production shape, from pipeline/v2/out/split_stats.json.
ROWS, EPOCHS, PER_DEVICE, ACCUM = 20200, 1.0, 1, 8


# ------------------------------------------------------------------ the unit being counted

def test_an_optimizer_step_consumes_the_accumulated_batch_not_one_micro_batch():
    """The error this exists to prevent, with the wrong answers named explicitly.

    An optimizer step eats per_device_batch_size * gradient_accumulation rows. Counting
    micro-batches gives 8x the steps here; forgetting the accumulation on the other side of
    the multiplication gives an 8x runtime. Both pass a smell test and neither is close.
    """
    assert intended_steps(ROWS, EPOCHS, PER_DEVICE, ACCUM) == 2525
    assert intended_steps(ROWS, EPOCHS, PER_DEVICE, ACCUM) != ROWS            # micro-batches
    assert intended_steps(ROWS, EPOCHS, PER_DEVICE, ACCUM) != ROWS // PER_DEVICE
    # and the accumulation genuinely moves it, so the assertion above is not vacuous
    assert intended_steps(ROWS, EPOCHS, PER_DEVICE, 1) == 20200


def test_the_step_count_agrees_with_the_preflight_validators_own_arithmetic():
    """Two producers of the same number, one decision. The validator computes steps to
    decide whether MaxRuntime is enough; the trainer computes them to label a capped run.
    If they ever disagree, a run passes the gate and then reports a fraction of a different
    denominator, and nobody can tell which one was wrong."""
    job = {
        "TrainingJobName": "unit-test",
        "HyperParameters": {"epochs": "1", "max_length": "14336",
                            "per_device_batch_size": str(PER_DEVICE),
                            "gradient_accumulation": str(ACCUM), "save_steps": "50",
                            "max_train_seconds": "40500", "drop_overlong": "true"},
        "StoppingCondition": {"MaxRuntimeInSeconds": 43200},
        "CheckpointConfig": {"S3Uri": "s3://bucket/ckpt/"},
    }
    def validator_steps(rows):
        _, _, facts = validator.check(job, sec_per_it=11.0, rows=rows)
        step_facts = [f for f in facts if "steps at" in f]
        assert step_facts, f"the validator no longer states a step count: {facts}"
        return int(re.search(r"~(\d+) steps", step_facts[0]).group(1))

    # Exactly equal, not within one. "Within one" is what hid the fact that the validator
    # floored while the trainer ceiled -- a tolerance wide enough to cover the defect.
    for rows in (ROWS, ROWS + 1, ROWS + 7, 10, 1):
        mine = intended_steps(rows, EPOCHS, PER_DEVICE, ACCUM)
        assert validator_steps(rows) == mine, \
            f"at {rows} rows the validator says {validator_steps(rows)}, the trainer says {mine}"
    # and the shapes above genuinely include one that does not divide, so the equality
    # assertions can actually see a floor/ceil disagreement
    assert (ROWS + 1) % (PER_DEVICE * ACCUM) != 0


def test_the_preflight_fails_rather_than_passing_when_it_cannot_do_the_arithmetic():
    """A payload with no measured inputs used to print "PASS - safe to launch".

    `if rows and sec_per_it and max_runtime:` skipped the entire block that exists to catch
    the e1g6 defect, and nothing else in the script produces a failure for a config that is
    otherwise well-formed. So the one script written to stop an unaffordable launch reported
    clean on every launch where the operator forgot a flag -- which, measured after the fact,
    was every launch it ever had. A guard that cannot run must not report clean.
    """
    good = {
        "TrainingJobName": "unit-test",
        "HyperParameters": {"epochs": "1", "max_length": "14336", "per_device_batch_size": "1",
                            "gradient_accumulation": "8", "save_steps": "50",
                            "max_train_seconds": "40500", "drop_overlong": "true"},
        "StoppingCondition": {"MaxRuntimeInSeconds": 43200},
        "CheckpointConfig": {"S3Uri": "s3://bucket/ckpt/"},
    }
    # the control: with both measurements supplied this payload is clean, so the failures
    # below are caused by the missing inputs and not by the fixture
    assert validator.check(good, sec_per_it=11.0, rows=ROWS)[0] == []

    no_rows = validator.check(good, sec_per_it=11.0, rows=0)[0]
    assert any("--rows" in f for f in no_rows), no_rows

    no_sec = validator.check(good, sec_per_it=None, rows=ROWS)[0]
    assert any("--sec-per-it" in f for f in no_sec), no_sec
    # and it has to say how to get the number, not just that it is missing
    joined = " ".join(no_sec)
    assert "--max_steps 20" in joined and "p50_steady" in joined, joined

    assert validator.check(good, sec_per_it=0.0, rows=0)[0], "neither input, still no failure"


def _capped_job(cap):
    """The real probe payload's shape, with the cap as the only variable."""
    return {
        "TrainingJobName": "unit-test",
        "HyperParameters": {"epochs": "3", "max_length": "14336",
                            "per_device_batch_size": str(PER_DEVICE),
                            "gradient_accumulation": str(ACCUM), "save_steps": "50",
                            "max_train_seconds": "1200", "drop_overlong": "true",
                            **({"max_steps": str(cap)} if cap else {})},
        "StoppingCondition": {"MaxRuntimeInSeconds": 5400},
        "CheckpointConfig": {"S3Uri": "s3://bucket/ckpt/"},
    }


def test_a_step_capped_payload_is_sized_at_the_cap_not_at_the_full_pass():
    """The gate has to size the job in the payload, not the job the payload resembles.

    Found by running the real 20-step probe payload through it: the gate reported "~75 steps
    at 30.0s/it" for a job configured to run 20. `--max_steps` is the flag the preflight's own
    usage line tells you to pass, and the arithmetic did not read it -- so the one line an
    operator reads stated a step count for a job nobody was launching, and the checkpoint
    plan below it was derived from the same number.
    """
    def steps_reported(job):
        _, _, facts = validator.check(job, sec_per_it=30.0, rows=200)
        f = [x for x in facts if "steps at" in x]
        assert f, f"the validator no longer states a step count: {facts}"
        return int(re.search(r"~(\d+) steps", f[0]).group(1))

    full = intended_steps(200, 3.0, PER_DEVICE, ACCUM)
    assert steps_reported(_capped_job(0)) == full == 75      # control: uncapped, unchanged
    assert steps_reported(_capped_job(20)) == 20
    # a cap above the full pass cannot invent steps that do not exist
    assert steps_reported(_capped_job(500)) == full


def test_a_pass_on_a_capped_payload_does_not_carry_over_to_the_run_it_measures():
    """A 20-step probe clears every limit by construction. Saying PASS and nothing else
    invites the reader to carry that verdict to the multi-hour run -- which is the launch
    this whole script exists to refuse. So the cap is stated, with both numbers."""
    _, warns, _ = validator.check(_capped_job(20), sec_per_it=30.0, rows=200)
    joined = " ".join(warns)
    assert "--max_steps 20" in joined and "20 of 75" in joined, warns
    assert "MEASUREMENT" in joined and "uncapped" in joined, warns
    # and the warning is caused by the cap, not by the rest of the fixture
    _, uncapped, _ = validator.check(_capped_job(0), sec_per_it=30.0, rows=200)
    assert not any("max_steps" in w for w in uncapped), uncapped


def test_the_measured_input_has_no_default_in_any_copy_of_the_preflight():
    """A flag whose default is a plausible lie is worse than a missing argument: it makes
    the gate pass on a measurement of a different instance, a different model and a quarter
    of the sequence length. Globbed rather than named, for the usual reason."""
    copies = sorted(REPO.glob("pipeline/training/**/validate_job_config.py"))
    assert copies, "no preflight found -- this test cannot guard anything"
    for c in copies:
        s = c.read_text()
        assert 'default=27.0' not in s and 'default=26.6' not in s, \
            f"{c} still defaults --sec-per-it to a number from another experiment"
        decl = s.split('"--sec-per-it"')[1].split("ap.add_argument")[0]
        assert "default=None" in decl, f"{c} does not require a measured --sec-per-it: {decl}"


def test_a_leftover_partial_batch_is_still_a_step():
    """20,200 / 8 divides exactly, which is why this needs its own case: on the corpus we
    have, ceil and floor agree, so nothing else here can tell them apart. A floor
    UNDERSTATES the step count, and an understated step count understates the wall clock --
    the direction that gets a run killed at 39% with no artifact, which is the whole reason
    this arithmetic exists.
    """
    assert 20200 % 8 == 0, "the production shape divides exactly; this test is the guard"
    assert intended_steps(20200, 1.0, 1, 3) == 6734          # 6733.33 -> 6734, not 6733
    assert intended_steps(10, 1.0, 1, 4) == 3                # 2.5 -> 3, not 2
    assert intended_steps(20200, 1.5, 1, 8) == 3788          # 3787.5 -> 3788, not 3787


def test_a_tiny_corpus_still_takes_at_least_one_step():
    """Fewer rows than one accumulated batch must not floor to zero steps -- zero is the
    denominator completed_fraction divides by."""
    assert intended_steps(5, 1.0, 1, 8) == 1
    assert intended_steps(0, 1.0, 1, 8) == 1
    assert completed_fraction(1, 1, intended_steps(5, 1.0, 1, 8), True) == 1.0


# ------------------------------------------------------------ honest labelling of a probe

def test_a_step_capped_probe_does_not_report_itself_complete():
    """20 of 2,525 steps is 0.79% of the run, and trainer.state.max_steps says 20/20.

    Every gate in this repo reads completed_fraction to tell a partial run from a finished
    one. A probe reporting 1.0 is a lie in precisely the field built to stop that mistake.
    """
    n = intended_steps(ROWS, EPOCHS, PER_DEVICE, ACCUM)
    frac = completed_fraction(global_step=20, trainer_max_steps=20, n_intended=n,
                              step_capped=True)
    assert frac == 20 / 2525
    assert frac < 0.01
    # the naive answer, which this must not be
    assert frac != 1.0


def test_an_uncapped_run_still_reports_progress_against_its_own_steps():
    """The budget-stopped case the deliverability work already relies on must not change:
    a run killed at step 496 of 1265 is 39% complete, and that 39% is a regression fixture
    elsewhere in this suite."""
    assert completed_fraction(496, 1265, n_intended=1265, step_capped=False) == 496 / 1265
    assert completed_fraction(1265, 1265, n_intended=1265, step_capped=False) == 1.0
    assert completed_fraction(3, 0, n_intended=99, step_capped=False) is None


# ---------------------------------------------------------------------- seconds per step

def test_seconds_per_step_reports_the_steady_state_not_the_warmup_mean():
    """A slow first step is real (CUDA autotune, allocator growth, the first checkpoint).

    Over 2,525 steps it is noise. Over the 20 a preflight can afford it is most of the mean,
    and the mean is what a naive train_runtime / global_step reports.
    """
    seconds = [90.0, 30.0, 14.0] + [11.0] * 17
    t = step_timing(seconds)
    assert t["n_steps_timed"] == 20
    assert t["first_step"] == 90.0
    assert t["p50_steady"] == 11.0
    assert t["warmup_steps_excluded"] == 3
    # the negative control: this fixture must actually be able to show the defect, or the
    # assertion above proves only that the arithmetic ran
    assert t["mean"] > 1.4 * t["p50_steady"], t
    # and the consequence, in the unit the sign-off is denominated in
    n = intended_steps(ROWS, EPOCHS, PER_DEVICE, ACCUM)
    overstatement_hours = n * (t["mean"] - t["p50_steady"]) / 3600
    assert overstatement_hours > 3, overstatement_hours


def test_the_steady_median_actually_drops_the_warmup_samples():
    """The 20-step fixture above cannot prove this: 17 of its 20 samples are 11.0, so the
    median is 11.0 whether or not the warmup is excluded, and an implementation that never
    excluded anything would pass it. Byte-identical output under a mutation means the wrong
    quantity is being asserted, so here is a fixture short enough for the exclusion to move
    the number -- which is also the realistic bad case, a probe cut short by a slow step.
    """
    seconds = [90.0, 60.0, 40.0, 11.0, 11.0, 11.0]
    t = step_timing(seconds)
    assert t["p50"] == 40.0, "the all-sample median must still be the warmup-poisoned one"
    assert t["p50_steady"] == 11.0
    assert t["p50_steady"] != t["p50"]          # the exclusion has to be load-bearing here


def test_the_unit_is_stated_in_the_output_because_the_wrong_unit_is_the_failure():
    t = step_timing([1.0, 1.0, 1.0, 1.0])
    assert "OPTIMIZER step" in t["unit"]
    assert "accumulation" in t["unit"]


def test_the_warmup_window_cannot_swallow_every_sample():
    """A run that stops inside the warmup window still has to report a number. Falling back
    to every sample is honest and low; returning None would make the preflight silently
    reuse its stale 26.6 default."""
    t = step_timing([40.0, 12.0])
    assert t["p50_steady"] is not None
    assert t["p50_steady"] in (12.0, 40.0)
    assert step_timing([])["p50_steady"] is None
    assert step_timing([])["n_steps_timed"] == 0


# ------------------------------------------------------------------------------- peak VRAM

class _FakeCuda:
    """A LYING torch.cuda: live tensors are small while the allocator pool is nearly full.

    An echoing double (allocated == reserved) would pass a snapshot that reads the wrong
    field, which is the whole thing being tested -- the number that OOMs is the pool.
    """

    def __init__(self, available=True, allocated=8 << 30, reserved=21 << 30, total=24 << 30,
                 raises=False):
        self._a, self._alloc, self._res, self._tot = available, allocated, reserved, total
        self._raises = raises

    def is_available(self):
        if self._raises:
            raise RuntimeError("CUDA driver version is insufficient")
        return self._a

    def get_device_properties(self, i):
        return type("P", (), {"total_memory": self._tot})()

    def get_device_name(self, i):
        return "NVIDIA L40S"

    def max_memory_reserved(self, i):
        return self._res

    def max_memory_allocated(self, i):
        return self._alloc


class _FakeTorch:
    def __init__(self, cuda):
        self.cuda = cuda


def test_peak_vram_reports_the_allocator_pool_not_just_live_tensors():
    v = vram_snapshot(_FakeTorch(_FakeCuda()))
    assert v["available"] is True
    assert v["max_allocated_gib"] == 8.0
    assert v["max_reserved_gib"] == 21.0
    assert v["device_total_gib"] == 24.0
    # 21/24, not 8/24 -- a snapshot keyed on allocated would say 0.333 and read as roomy
    assert v["reserved_fraction_of_device"] == 0.875
    assert v["reserved_fraction_of_device"] != 0.333


def test_no_gpu_is_recorded_as_unavailable_with_a_reason_not_as_a_missing_field():
    """A CPU dry-run must not produce metrics.json that merely omits VRAM: an absent field
    and a fitting run look the same to anyone reading the file later."""
    v = vram_snapshot(_FakeTorch(_FakeCuda(available=False)))
    assert v["available"] is False
    assert v["reason"]


def test_a_torch_that_raises_does_not_lose_the_run():
    """This snapshot is instrumentation. It runs after training and before the adapter is
    saved on some paths; it must never be the reason a trained model is thrown away."""
    v = vram_snapshot(_FakeTorch(_FakeCuda(raises=True)))
    assert v["available"] is False
    assert "RuntimeError" in v["reason"]


# ------------------------------------------------------------- present in what gets deployed

def test_every_copy_of_the_trainer_can_be_step_capped_and_times_its_steps():
    """Globbed, not named. The mirror-integrity guard and all three deliverability rules
    once lived in the copy no job downloaded; a preflight knob in the wrong copy is the
    same defect with a cheaper failure."""
    for t in TRAINERS:
        s = t.read_text()
        assert '"--max_steps"' in s, f"{t} cannot be step-capped"
        assert "max_steps=args.max_steps if args.max_steps > 0 else -1" in s, \
            f"{t} declares --max_steps but never passes it to the trainer config"
        assert "StepTimerCallback" in s and "trainer.add_callback(timer_cb)" in s, \
            f"{t} does not time its steps"
        assert "vram_snapshot(torch)" in s, f"{t} does not record peak VRAM"
        assert "def completed_fraction(" in s, f"{t} labels a capped run naively"


def test_metrics_json_carries_both_numbers_the_signoff_needs():
    """The measurement has to survive the job. A number printed to a CloudWatch log and not
    written to metrics.json is a number the next launcher re-measures."""
    for key in ('"sec_per_step": step_timing(timer_cb.step_seconds)',
                '"peak_vram": {"training": vram_training',
                '"intended_steps_full_run": n_intended',
                '"step_capped": step_capped'):
        assert key in trainer_src, f"metrics.json does not record {key}"


def test_the_probe_labels_itself_in_the_log_as_well_as_in_the_metrics():
    """TRAINING_COMPLETE prints on a 20-step probe too, because the code path completed.
    Something adjacent has to say the run is a measurement, or a log reader promotes it."""
    assert "STEP-CAPPED RUN" in trainer_src
    assert "MEASUREMENT, not a trained adapter" in trainer_src
    assert "METRIC sec_per_step_p50_steady=" in trainer_src
    assert "METRIC peak_vram_reserved_gib=" in trainer_src


def test_the_vram_snapshot_for_training_is_taken_before_eval_can_raise_the_mark():
    """max_memory_reserved is a high-water mark that never falls. Reading it after
    trainer.evaluate() answers "does training plus eval fit", which is a different question
    and a strictly larger number -- so the training figure has to be read first."""
    after_train = trainer_src.index("vram_training = vram_snapshot(torch)")
    evaluate = trainer_src.index("trainer.evaluate()")
    assert after_train < evaluate, \
        "the training VRAM snapshot is taken after eval, so it is not a training figure"
