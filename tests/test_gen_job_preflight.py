"""Tests for the GENERATION job's preflight and SageMaker entry point.

A fine-tuning payload and a generation payload fail in different ways, and the repo
already had a preflight for the first one. `validate_job_config.py` multiplies
`save_steps`, `epochs`, `gradient_accumulation` and rows x epochs / effective batch --
none of which exist in a generation payload. Pointed at one it emits four FAILs that
are all correct for a trainer and all meaningless for a decoder, so the only way to
launch would be to override it, and a gate whose verdict is routinely overridden has
stopped being a gate. Hence `validate_gen_config.py`, and hence these tests.

The defect being gated is v2-code-distill-0001-e1g6 generalised: 43 GPU-minutes, zero
artifacts, because the only save point sat past the wall clock. `/opt/ml/output/data`
is uploaded when the container EXITS, and generation has nothing to checkpoint, so a
decode still running at `MaxRuntimeInSeconds` loses every generation already written.
The preflight's job is to refuse a payload whose graceful budget cannot finish before
the hard kill.

`run_eval_gen.py` is tested off a FAKE CHANNEL TREE on disk. Everything it does is
"find the thing in the directory", and every way of getting that wrong produces a
complete, plausible report against the wrong weights -- the base model and the
fine-tuned merge have identical architecture, tokenizer and file names.

Run: .venv/bin/python -m pytest tests/test_gen_job_preflight.py -q
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO / f"pipeline/v2/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vgc = _load("validate_gen_config")
reg = _load("run_eval_gen")


# ------------------------------------------------------------------- the preflight

def payload(runtime=2700, **hp):
    """A payload that PASSES, so each test below changes exactly one thing.

    Built from the values the eval120 dry run actually launched with. A baseline that
    already failed would make every FAIL assertion below pass for the wrong reason.
    """
    base = {"limit": 0, "max_new_tokens": 4096, "n_samples": 1, "temperature": 0.0,
            "batch_size": 2, "input_window": 20480, "max_seconds": 1800}
    base.update(hp)
    return {"TrainingJobName": "t",
            "HyperParameters": {k: str(v) for k, v in base.items()},
            "StoppingCondition": {"MaxRuntimeInSeconds": runtime}}


def check(p, n_rows=120, startup=600, tok_per_s=None):
    return vgc.check(p, n_rows, startup, tok_per_s)


def test_the_baseline_payload_passes():
    fails, _, facts = check(payload())
    assert fails == []
    assert facts, "a PASS with no facts printed is a gate nobody can audit"


def test_a_payload_with_no_graceful_budget_is_refused():
    """The whole mechanism. Without max_seconds the only bound is SageMaker's hard
    kill, which terminates the container mid-decode -- and the upload that would have
    delivered the generations happens on container EXIT."""
    fails, _, _ = check(payload(max_seconds=0))
    assert len(fails) == 1
    assert "max_seconds is not set" in fails[0]
    assert "container EXIT" in fails[0]


def test_a_budget_that_cannot_finish_before_the_hard_kill_is_refused():
    """startup 600 + budget 2500 + upload 120 = 3220 > MaxRuntime 2700. A budget that
    expires after the kill it exists to avoid buys nothing, and the run looks
    correctly configured right up to the moment it loses everything."""
    fails, _, _ = check(payload(max_seconds=2500))
    assert any("MaxRuntimeInSeconds" in f and ">=" in f for f in fails)


def test_a_budget_that_expires_exactly_at_the_hard_kill_is_refused():
    """The boundary is the reason the comparison is `>=`. startup 600 + 1980 + 120 =
    2700 == MaxRuntime: the graceful stop and the hard kill land on the same second, so
    the budget has no room to be the thing that ends the job. With `>` here this payload
    launches and is indistinguishable from a correctly configured one until it loses its
    output."""
    fails, _, _ = check(payload(max_seconds=1980))
    assert any("MaxRuntimeInSeconds" in f for f in fails)


def test_the_headroom_arithmetic_is_printed_with_its_terms():
    """A gate that prints only a verdict cannot be checked against a changed startup
    time. All four terms appear, so a reader can redo the sum."""
    _, _, facts = check(payload(), startup=333)
    line = [f for f in facts if "MaxRuntime" in f]
    assert len(line) == 1
    assert "333" in line[0] and "1800" in line[0] and "120" in line[0]
    assert "2253" in line[0], "the sum itself, not just the addends"


def test_missing_max_runtime_is_refused():
    p = payload()
    p["StoppingCondition"] = {}
    fails, _, _ = check(p)
    assert any("MaxRuntimeInSeconds is not set" in f for f in fails)


def test_the_slack_beyond_the_accounted_time_is_reported_as_a_number():
    """startup 600 + 1900 + 120 = 2620 against 2700 leaves 80s. Reported as a fact
    rather than a warning, because slack on its own does not say whether the run is
    safe: what makes it safe or not is how it compares to ONE BATCH's decode time,
    which is the check below. The 80s-of-slack heuristic this replaces is what passed
    the g5 job."""
    fails, _, facts = check(payload(max_seconds=1900))
    assert fails == []
    # Matched on the SLACK fact's own wording, not on "80s of slack" alone: the
    # required-rate fact added later also contains "...s of slack", and "80s of slack"
    # is a substring of its "780s of slack" -- so the loose assertion started passing
    # through the wrong fact and a mutant that deleted this line survived.
    hit = [f for f in facts if "beyond the accounted time" in f]
    assert len(hit) == 1, f"expected exactly one slack fact, got {hit}"
    assert hit[0].startswith("80s of slack"), hit[0]


def test_a_budget_that_cannot_bound_the_final_batch_is_refused():
    """The defect measured on arc2v2-gen-base-0823a-g5. The budget is read BEFORE a
    batch, never inside one, so the worst case is budget + one batch. At 35 tok/s a
    batch of 2 x 4096 tokens is 3.9 min; 600 + 1800 + 234 + 120 = 2754 > 2700, so the
    hard kill can land mid-batch and take every generation in it.

    This payload PASSES the plain headroom check (600 + 1800 + 120 = 2520, 180s of
    slack) -- which is the point: the old gate looked at slack and not at what could
    consume it."""
    fails, _, facts = check(payload(), tok_per_s=35.0)
    assert any("BETWEEN batches" in f and "2700" in f for f in fails)
    assert any("possible overshoot" in f for f in facts)


def test_a_batch_that_fits_inside_the_slack_is_not_refused():
    """The other side. At 5000 tok/s one batch of 2 x 4096 is 1.6s, so 600 + 1800 +
    1.6 + 120 clears 2700 comfortably. Without this, a gate that FAILED every payload
    would satisfy the assertion above."""
    fails, _, _ = check(payload(), tok_per_s=5000.0)
    assert fails == []


def test_the_overshoot_is_sized_from_the_batch_not_the_whole_run():
    """`min(batch, generations)`: with --limit 1 there is one generation, so a batch of
    2 decodes one sequence. Sizing the overshoot from `batch` regardless would refuse a
    payload for work it is not going to do."""
    fails, _, facts = check(payload(limit=1), tok_per_s=35.0)
    assert fails == []
    assert any("1 prompts x 1 samples = 1 sequences x 4096" in f for f in facts)


def test_the_overshoot_counts_sequences_not_prompts():
    """A batch decodes `batch_size * n_samples` sequences, not `batch_size` of them.

    This payload is built so the two readings DISAGREE ON THE VERDICT, which is the only
    kind of assertion that can detect the difference. batch 4, n_samples 2, 2048 new
    tokens, 39.1 tok/s:

      * per prompt (wrong): 4 x 2048 / 39.1 = 209.5s -> 600 + 13200 + 209.5 + 120
        = 14129.5s, under MaxRuntime 14200 -> PASS
      * per sequence (right): 8 x 2048 / 39.1 = 419.0s -> 14339.0s, over 14200 -> FAIL

    Every other overshoot test here runs at the n_samples=1 default, where the two terms
    are numerically equal, so all 42 of them passed against the per-prompt formula. The
    real launch that exposed it was Workflow 5's eval120 arms (120 prompts x 2 samples).
    """
    fails, _, facts = check(payload(runtime=14200, max_seconds=13200, batch_size=4,
                                    n_samples=2, temperature=0.7, max_new_tokens=2048),
                            tok_per_s=39.1)
    assert any("MaxRuntimeInSeconds" in f for f in fails), \
        f"the doubled overshoot must be refused, got fails={fails}"
    line = [f for f in facts if "possible overshoot" in f]
    assert len(line) == 1, line
    # The sequence count and both factors, so a reader can redo the multiplication
    # instead of trusting it.
    assert "4 prompts x 2 samples = 8 sequences x 2048" in line[0], line[0]
    assert "7.0 min of possible overshoot" in line[0], line[0]


def test_the_required_rate_counts_sequences_not_prompts():
    """The unmeasured-rate branch had the identical defect, and understating a REQUIRED
    rate is the direction that lets a job launch. slack = 14400 - (600 + 13200 + 120)
    = 480s; 8 sequences x 2048 / 480 = 34.1 tok/s, where the per-prompt reading would
    have quoted 17. The probe measured 39.1 tok/s aggregate, so 17 and 34 sit on opposite
    sides of nothing here -- but at batch 8 the same error is 4x, and that is a launch
    decision made on half the truth."""
    _, _, facts = check(payload(runtime=14400, max_seconds=13200, batch_size=4,
                                n_samples=2, temperature=0.7, max_new_tokens=2048),
                        tok_per_s=None)
    hit = [f for f in facts if "requires >=" in f]
    assert len(hit) == 1, hit
    assert "480s of slack" in hit[0], hit[0]
    assert "34 tok/s" in hit[0], hit[0]
    assert "4 prompts x 2 samples = 8 sequences x 2048" in hit[0], hit[0]


def test_an_unmeasured_rate_says_the_final_batch_is_unbounded():
    """Unconditionally, not when slack looks thin. The g5 job had 180s of slack and a
    first batch that ran past 25 minutes; a warning keyed on slack cannot say anything
    about that, because slack is not the quantity at risk."""
    _, warns, _ = check(payload(max_seconds=1200), tok_per_s=None)
    hit = [w for w in warns if "only read BETWEEN batches" in w]
    assert len(hit) == 1
    assert "4096" in hit[0] and "UNKNOWN" in hit[0]


def test_an_unmeasured_rate_still_states_the_rate_the_config_requires():
    """UNKNOWN is not the same as unquantifiable. With max_seconds 1200, startup 600 and
    upload 120 against MaxRuntime 2700, the final batch has 780s to finish 2 x 4096 =
    8192 tokens, so the configuration ASSUMES 8192/780 = 10.5 -> >= 11 tok/s. Stating the
    assumed rate is what makes the warning actionable without a measurement: the reader
    can compare 11 against anything they know, whereas "UNKNOWN" invites launching and
    hoping."""
    _, _, facts = check(payload(max_seconds=1200), tok_per_s=None)
    hit = [f for f in facts if "requires >=" in f]
    assert len(hit) == 1, f"expected one required-rate fact, got {hit}"
    assert "780s of slack" in hit[0], hit[0]
    assert "11 tok/s" in hit[0], hit[0]
    # It must say the number is an assumption, or it reads as a measurement.
    assert "NOT\nmeasured" in hit[0].replace(" ", "\n") or "NOT measured" in hit[0]


def test_the_required_rate_is_not_stated_when_a_rate_was_measured():
    """The other half. With --tok-per-s the overshoot is a FAIL or it is fine, and an
    'assumed rate' line beside a measured one would be two numbers for one quantity."""
    _, _, facts = check(payload(max_seconds=1200), tok_per_s=5000.0)
    assert not [f for f in facts if "requires >=" in f]


def test_the_measured_g5_configuration_is_refused_once_its_rate_is_known():
    """The regression case, with the numbers the job actually produced.

    arc2v2-gen-base-0823a-g5: A10G, batch 2, max_new_tokens 16384, max_seconds 2700,
    MaxRuntime 3600, startup 600. It decoded 2 x 16384 tokens in 2,686 s = 12.2 tok/s
    aggregate, both sequences hitting the ceiling without emitting EOS. The budget check
    before batch 2 therefore saw 2,686s against 2,700 -- it missed by FOURTEEN SECONDS --
    and the run committed to a second batch needing another 2,686s with 671s of wall
    clock left. It died at MaxRuntimeExceeded with no .done sidecar.

    With that rate in hand the gate must refuse the configuration outright.
    """
    fails, _, facts = check(payload(runtime=3600, max_seconds=2700,
                                    max_new_tokens=16384, batch_size=2),
                            n_rows=4, startup=600, tok_per_s=12.2)
    hit = [f for f in fails if "BETWEEN batches" in f]
    assert len(hit) == 1, f"the g5 configuration was not refused: {fails}"
    # 600 + 2700 + 2686 + 120 = 6106s against a 3600s hard kill.
    assert "6106s" in hit[0], hit[0]
    assert any("44.8 min of possible overshoot" in f for f in facts), facts


def test_the_g5_configuration_passed_the_headroom_check_it_was_launched_under():
    """Why the second check had to exist. The same payload, with only the FIRST check
    able to fire, clears it with 180s to spare -- so a gate holding just that check
    reports PASS on the run that lost 45 minutes of decode. Asserting the new FAIL
    without this would leave 'the old gate was insufficient' as an assertion."""
    _, _, facts = check(payload(runtime=3600, max_seconds=2700, max_new_tokens=16384),
                        n_rows=4, startup=600, tok_per_s=None)
    assert any("3420s vs MaxRuntime 3600s" in f for f in facts), facts
    # Same collision hazard as above: both the slack fact and the required-rate fact
    # say "180s of slack" here, so the slack fact is identified by its own wording.
    assert any(f.startswith("180s of slack beyond the accounted time") for f in facts), \
        facts
    # And the required-rate fact names the number the A10G missed by 15x.
    assert any("182 tok/s" in f for f in facts), \
        [f for f in facts if "requires" in f]


def test_a_payload_that_would_produce_nothing_is_refused():
    fails, _, _ = check(payload(), n_rows=0)
    assert any("no generations" in f for f in fails)


def test_k_greedy_samples_are_refused():
    """k samples at temperature 0 are k identical strings, so pass@k over them is
    pass@1 reported over a k-times larger denominator -- a metric that improves with
    k while the model does not change."""
    fails, _, _ = check(payload(n_samples=2, temperature=0.0))
    assert any("identical strings" in f for f in fails)


def test_k_samples_with_sampling_are_allowed():
    fails, _, _ = check(payload(n_samples=2, temperature=0.7))
    assert fails == []


def test_an_input_window_that_truncates_the_measured_maximum_prompt_is_refused():
    """14,513 is the measured maximum chat-wrapped eval120 prompt. `truncation_side`
    is "left", so the overflowing task loses its instruction header and is scored as a
    model failure -- at the training value 14,336 that is exactly one of the 120."""
    fails, _, _ = check(payload(input_window=14336))
    assert any("14513" in f and "instruction header" in f for f in fails)


def test_the_boundary_window_is_accepted():
    assert check(payload(input_window=14513))[0] == []


def test_an_unmeasured_throughput_is_launchable_but_flagged_as_unknown_coverage():
    """The first generation job on a new model has no measured tok/s and cannot get
    one without running. Requiring the measurement makes the measuring job
    unlaunchable; inventing a default makes the gate pass on a number nobody measured.
    So without --tok-per-s the gate checks only that the job CANNOT overrun, and says
    the coverage is unknown."""
    fails, warns, _ = check(payload(), tok_per_s=None)
    assert fails == []
    assert any("UNKNOWN" in w and "do not report its coverage as complete" in w
               for w in warns)


def test_a_measured_throughput_turns_coverage_into_a_checked_claim():
    fails, warns, facts = check(payload(), tok_per_s=1000.0)
    assert fails == []
    assert not any("UNKNOWN" in w for w in warns)
    assert any("tok/s" in f and "of decoding" in f for f in facts)


def test_work_that_exceeds_the_budget_warns_about_partial_coverage():
    """120 prompts x 4096 tokens at 35 tok/s is 3.9 hours against a 30-minute budget.
    Legitimate for a measurement and wrong for a lift comparison, which is what the
    warning has to say -- the two runs would have different denominators under one
    field name.

    MaxRuntime is raised to 3600 to isolate the coverage warning: at 2700 this payload
    also trips the final-batch overshoot FAIL, and `fails == []` would then be asserting
    two unrelated things at once."""
    fails, warns, _ = check(payload(runtime=3600), tok_per_s=35.0)
    assert fails == []
    hit = [w for w in warns if "exceeds max_seconds" in w]
    assert len(hit) == 1
    assert "stopped_early" in hit[0]


def test_a_limit_shrinks_the_work_the_coverage_check_sizes():
    """--limit is how the dry run cost $0.38 instead of $4. If the arithmetic ignored
    it, every probe payload would be sized as a full 120-task run and warn."""
    _, warns, facts = check(payload(limit=4), tok_per_s=35.0)
    assert any("4 generations" in f for f in facts)
    assert not any("exceeds max_seconds" in w for w in warns)


def test_a_limit_larger_than_the_corpus_does_not_invent_rows():
    """Both terms of the fact line, because they are computed separately: `limit` alone
    would print "500 generations = 120 prompts", and asserting only the prompt count
    passes for a sizing that thinks it has 380 rows nobody has."""
    _, _, facts = check(payload(limit=500), n_rows=120)
    line = [f for f in facts if "generations" in f]
    assert len(line) == 1
    assert "120 generations" in line[0] and "120 prompts" in line[0]


def test_the_exit_code_is_the_verdict(tmp_path, capsys, monkeypatch):
    """The launcher refuses to launch on a nonzero return, so a gate that printed
    FAIL and exited 0 would be decorative."""
    good, bad = tmp_path / "g.json", tmp_path / "b.json"
    good.write_text(json.dumps(payload()))
    bad.write_text(json.dumps(payload(max_seconds=0)))
    for path, want in ((good, 0), (bad, 1)):
        monkeypatch.setattr(sys, "argv", ["validate_gen_config.py", str(path),
                                          "--n-rows", "120",
                                          "--startup-seconds", "600"])
        assert vgc.main() == want
    out = capsys.readouterr().out
    assert "PASS — safe to launch" in out and "FAIL" in out


def test_startup_seconds_is_required(monkeypatch, capsys):
    """A guessed startup would make the headroom check agree with itself: the term
    that decides whether the budget leaves room to exit cleanly would be a constant
    chosen to fit. Asserted as argparse's usage error (rc 2) naming the flag, so this
    keeps failing if a default is added -- a plain `raises(SystemExit)` would pass for
    the file-not-found the same call also produces."""
    monkeypatch.setattr(sys, "argv",
                        ["validate_gen_config.py", "x.json", "--n-rows", "1"])
    with pytest.raises(SystemExit) as e:
        vgc.main()
    assert e.value.code == 2
    assert "--startup-seconds" in capsys.readouterr().err


# ------------------------------------------------- the SageMaker entry point

def _weights(d: pathlib.Path, name="model.safetensors", size=16):
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text('{"model_type": "qwen3"}')
    (d / name).write_bytes(b"\0" * size)
    return d


def test_a_directory_of_weights_resolves_to_itself(tmp_path):
    d = _weights(tmp_path / "ch")
    assert reg.find_weights(str(d)) == str(d)


def test_weights_nested_under_the_channel_are_found(tmp_path):
    d = _weights(tmp_path / "ch" / "merged")
    assert reg.find_weights(str(tmp_path / "ch")) == str(d)


def test_two_loadable_models_in_one_channel_is_an_error_not_a_preference(tmp_path):
    """"Prefer the one called merged" works today and picks the base model the day a
    directory is renamed, and that report looks completely normal. The merged export
    really does sit in `merged/` beside an `adapter/` sibling that also has a config,
    so this is the actual tree, not a hypothetical."""
    _weights(tmp_path / "ch" / "merged")
    _weights(tmp_path / "ch" / "base")
    with pytest.raises(SystemExit, match="ambiguous"):
        reg.find_weights(str(tmp_path / "ch"))


def test_a_config_without_weight_shards_is_not_a_model(tmp_path):
    """A LoRA checkpoint directory carries an adapter_config.json and no weights.
    Keying on config.json alone would resolve the model channel to a checkpoint and
    then load nothing -- or worse, load the base and report it as fine-tuned."""
    d = tmp_path / "ch" / "ckpt"
    d.mkdir(parents=True)
    (d / "config.json").write_text("{}")
    (d / "adapter_config.json").write_text("{}")
    with pytest.raises(SystemExit, match="no loadable weights"):
        reg.find_weights(str(tmp_path / "ch"))


def test_two_tarballs_in_a_channel_is_refused_before_anything_is_extracted(tmp_path):
    """The fine-tuned model's S3 prefix holds model.tar.gz AND output.tar.gz. A
    channel pointed at the prefix rather than the key gets both, and picking either
    is a guess about which one holds weights."""
    import tarfile
    ch = tmp_path / "ch"
    ch.mkdir()
    for name in ("model.tar.gz", "output.tar.gz"):
        with tarfile.open(ch / name, "w:gz") as tf:
            tf.add(__file__, arcname="x.py")
    with pytest.raises(SystemExit, match="refusing to guess"):
        reg.unpack(str(ch), str(tmp_path / "dest"))


def test_a_tarball_is_extracted_and_its_contents_resolve(tmp_path):
    import tarfile
    src = _weights(tmp_path / "src" / "merged")
    ch = tmp_path / "ch"
    ch.mkdir()
    with tarfile.open(ch / "model.tar.gz", "w:gz") as tf:
        tf.add(src, arcname="merged")
    root = reg.unpack(str(ch), str(tmp_path / "dest"))
    assert reg.find_weights(root).endswith("merged")


def test_a_missing_model_channel_does_not_fall_back_to_the_hub(tmp_path):
    """`from_pretrained("Qwen/...")` on a missing directory would download a model
    from the internet and score it as the fine-tuned one."""
    with pytest.raises(SystemExit, match="not a directory"):
        reg.unpack(str(tmp_path / "absent"), str(tmp_path / "d"))


def test_an_adapter_needs_both_of_its_files(tmp_path):
    root = tmp_path / "a"
    (root / "checkpoint-505").mkdir(parents=True)
    (root / "checkpoint-505" / "adapter_config.json").write_text("{}")
    with pytest.raises(SystemExit, match="expected exactly 1 adapter"):
        reg.find_adapter(str(root))
    (root / "checkpoint-505" / "adapter_model.safetensors").write_bytes(b"\0")
    assert reg.find_adapter(str(root)).endswith("checkpoint-505")


def test_two_adapters_are_refused(tmp_path):
    """The checkpoints prefix holds 15 of them (505 ... 7575). A channel pointed one
    level too high would merge whichever glob returned first, and the report would
    name a step nobody chose."""
    root = tmp_path / "a"
    for step in (505, 1010):
        d = root / f"checkpoint-{step}"
        d.mkdir(parents=True)
        (d / "adapter_config.json").write_text("{}")
        (d / "adapter_model.safetensors").write_bytes(b"\0")
    with pytest.raises(SystemExit, match="found 2"):
        reg.find_adapter(str(root))


def test_the_graceful_budget_is_passed_through_to_the_generator():
    """--max-seconds is the flag that makes this job's artifacts deliverable. Absent
    from PASSTHROUGH it would be rejected as an unknown hyperparameter, and a launcher
    that stripped it instead would produce a job bounded only by the hard kill."""
    assert "max-seconds" in reg.PASSTHROUGH


def test_every_passthrough_flag_is_one_the_generator_accepts():
    """Derived from the generator's own parser rather than listed twice. A flag in
    PASSTHROUGH that generate_student.py does not accept is a job that dies after
    paying for startup."""
    src = (REPO / "pipeline/v2/generate_student.py").read_text()
    for flag in sorted(reg.PASSTHROUGH):
        assert f'"--{flag}"' in src, f"--{flag} is passed through but not accepted"


def test_an_unknown_hyperparameter_is_fatal(tmp_path, monkeypatch):
    """A misspelled `max_new_token` would silently leave the default in place and
    every format failure would be attributed to the model."""
    hp = tmp_path / "hp.json"
    hp.write_text(json.dumps({"max_new_token": "4096"}))
    monkeypatch.setattr(reg, "HP_PATH", str(hp))
    with pytest.raises(SystemExit, match="unknown hyperparameters"):
        reg.main()


def test_sagemaker_internal_hyperparameters_are_not_flagged(tmp_path, monkeypatch):
    """SageMaker injects sagemaker_program / sagemaker_region / submit_directory into
    the same dict. Treating those as typos would make every real job fail the check."""
    hp = tmp_path / "hp.json"
    hp.write_text(json.dumps({"sagemaker_program": "run_eval_gen.py",
                              "sagemaker_region": "us-east-1"}))
    monkeypatch.setattr(reg, "HP_PATH", str(hp))
    monkeypatch.setattr(reg, "OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(reg, "MODEL_CH", str(tmp_path / "absent"))
    # Fails at channel resolution, NOT at the hyperparameter check -- which is the
    # assertion: it got past the check.
    with pytest.raises(SystemExit, match="not a directory"):
        reg.main()


def test_the_flags_are_recorded_before_the_generator_runs(tmp_path, monkeypatch):
    """A job killed by MaxRuntime still leaves behind what it was asked to do.
    Without argv.json that is only answerable from the console log, which expires --
    and "which flags did that job use" is the first question a surprising number
    raises."""
    model = _weights(tmp_path / "model")
    val = tmp_path / "val"
    val.mkdir()
    (val / "v.jsonl").write_text('{"task_id": "a", "prompt": "p"}\n')
    out = tmp_path / "out"
    hp = tmp_path / "hp.json"
    hp.write_text(json.dumps({"max_seconds": "1800", "batch_size": "2",
                              "sagemaker_program": "run_eval_gen.py"}))
    monkeypatch.setattr(reg, "HP_PATH", str(hp))
    monkeypatch.setattr(reg, "MODEL_CH", str(model))
    monkeypatch.setattr(reg, "VAL_CH", str(val))
    monkeypatch.setattr(reg, "ADAPTER_CH", str(tmp_path / "no-adapter"))
    monkeypatch.setattr(reg, "OUT_DIR", str(out))
    calls = []

    def fake_call(argv):
        # "Before" is the whole claim, so it is read AT call time. Asserting only that
        # both the file and the call happened would pass for a dump written afterwards,
        # which is exactly the version a MaxRuntime kill destroys.
        calls.append({"argv": argv, "argv_json_existed": (out / "argv.json").exists()})
        return 0

    monkeypatch.setattr(reg.subprocess, "call", fake_call)

    assert reg.main() == 0
    assert calls and calls[0]["argv_json_existed"], \
        "argv.json was written after the generator ran, so a killed job records nothing"
    rec = json.loads((out / "argv.json").read_text())
    assert rec["model_dir"] == str(model)
    assert rec["val"].endswith("v.jsonl")
    assert "--max-seconds" in rec["argv"] and "1800" in rec["argv"]
    # Underscores in, hyphens out: SageMaker cannot pass a hyphen in a
    # hyperparameter name, and the generator accepts nothing else.
    assert not any(a.startswith("--max_") for a in rec["argv"])
    assert calls[0]["argv"] == rec["argv"], \
        "the recorded flags are not the flags that ran"


def test_more_than_one_val_file_is_refused(tmp_path, monkeypatch):
    """Two jsonl files in the val channel means two question sets, and picking the
    first alphabetically would silently decide which experiment ran."""
    model = _weights(tmp_path / "model")
    val = tmp_path / "val"
    val.mkdir()
    (val / "a.jsonl").write_text("{}\n")
    (val / "b.jsonl").write_text("{}\n")
    monkeypatch.setattr(reg, "HP_PATH", str(tmp_path / "none.json"))
    monkeypatch.setattr(reg, "MODEL_CH", str(model))
    monkeypatch.setattr(reg, "VAL_CH", str(val))
    monkeypatch.setattr(reg, "ADAPTER_CH", str(tmp_path / "no-adapter"))
    monkeypatch.setattr(reg, "OUT_DIR", str(tmp_path / "out"))
    with pytest.raises(SystemExit, match="exactly 1"):
        reg.main()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
