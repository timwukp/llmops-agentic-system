"""Unit tests for training-job deliverability — no AWS calls, no GPU.

The regression these guard is expensive and silent: run v2-code-distill-0001-e1g6
trained healthily for 43 GPU-minutes and delivered zero artifacts, because its only
save point was arithmetically unreachable inside its own MaxRuntimeInSeconds. Nothing
crashed, so nothing warned. These tests encode the arithmetic that should have.

Run: .venv/bin/python -m pytest tests/test_training_deliverability.py -q
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mirrored_sourcedir() -> pathlib.Path:
    """The directory deploy/03_storage.py ensure_code() actually uploads.

    DERIVED, not written down, because writing it down is the bug this whole file was
    green through. Every test below guarded `pipeline/training/train_qlora.py` while the
    trainer that ran was a second copy under distill/ -- mirrored to s3://<bucket>/code/
    distill/, named by the finetune prompt, granted to the harness role -- which carried
    `save_strategy="no"` and none of the three deliverability rules. A hardcoded path let
    the guards stay green about a file no run could reach: it was mirrored nowhere and
    named by no prompt. Reading the path out of the function that does the uploading means
    a trainer nothing deploys cannot be green, and a future move of the sourcedir breaks
    this line loudly instead of quietly pointing the tests at an abandoned copy.
    """
    src = (REPO / "deploy/03_storage.py").read_text()
    body = src.split("def ensure_code(")[1].split("\ndef ")[0]
    parts = re.findall(r'"([a-z_0-9]+)"', body.split("src_dir =")[1].split("\n")[0])
    assert parts, "ensure_code no longer builds its source dir from literal path parts"
    d = REPO.joinpath(*parts)
    assert d.is_dir(), f"ensure_code mirrors {d}, which does not exist"
    return d


MIRRORED = _mirrored_sourcedir()
validator = _load("validate_job_config", str((MIRRORED / "validate_job_config.py")
                                             .relative_to(REPO)))
# train_qlora imports torch/trl at module scope, so pull the two pure helpers out of
# the source text instead of importing the module (these tests must run without a GPU).
trainer_src = (MIRRORED / "train_qlora.py").read_text()


def _extract(func_name: str):
    """Exec a single top-level function out of train_qlora.py in a bare namespace."""
    lines = trainer_src.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"def {func_name}("))
    end = start + 1
    while end < len(lines) and (not lines[end] or lines[end][0].isspace()):
        end += 1
    ns = {}
    exec("import glob, os, re\n" + "\n".join(lines[start:end]), ns)
    return ns[func_name]


truthy = _extract("truthy")
newest_checkpoint = _extract("newest_checkpoint")


# ---------------------------------------------------------------- job config gate

def _job(**over):
    """A known-good CreateTrainingJob payload; override keys to break it."""
    hp = {
        "epochs": "1", "max_length": "4096", "per_device_batch_size": "2",
        "gradient_accumulation": "8", "save_steps": "50",
        "max_train_seconds": "40500", "drop_overlong": "true",
    }
    hp.update(over.pop("hp", {}))
    job = {
        "TrainingJobName": "unit-test",
        "HyperParameters": hp,
        "StoppingCondition": {"MaxRuntimeInSeconds": 43200},
        "CheckpointConfig": {"S3Uri": "s3://bucket/ckpt/"},
    }
    job.update(over)
    return job


ROWS, SEC = 20225, 26.6


def test_good_config_passes():
    fails, _, facts = validator.check(_job(), SEC, ROWS)
    assert fails == []
    assert any("headroom" in f for f in facts)


def test_rejects_the_e1g6_config_that_wasted_43_gpu_minutes():
    """The actual regression: 4h runtime, save only at end of epoch, no S3 checkpoints."""
    e1g6 = _job(
        hp={"save_steps": "0", "max_train_seconds": "0", "gradient_accumulation": "16",
            "per_device_batch_size": "1"},
        StoppingCondition={"MaxRuntimeInSeconds": 14400},
        CheckpointConfig={},
    )
    fails, _, _ = validator.check(e1g6, 29.0, ROWS)
    assert fails, "the config that produced zero artifacts must not pass validation"
    joined = " ".join(fails)
    assert "save_steps" in joined            # no periodic saves
    assert "CheckpointConfig" in joined      # nothing syncs to S3
    # and the time limit reaches only 39% of the run — the number from the real incident
    assert "reaches only 39% of the configured run" in joined


def test_budget_at_or_above_max_runtime_is_fatal():
    """A budget >= MaxRuntime means SageMaker kills the job before it can save."""
    for budget in ("43200", "50000"):
        fails, _, _ = validator.check(_job(hp={"max_train_seconds": budget}), SEC, ROWS)
        assert any("MaxRuntimeInSeconds" in f for f in fails), budget


def test_thin_headroom_warns_but_does_not_block():
    # 43200 - 42600 = 600s: enough to save, too thin to be comfortable.
    fails, warns, _ = validator.check(_job(hp={"max_train_seconds": "42600"}), SEC, ROWS)
    assert fails == []
    assert any("headroom" in w for w in warns)


def test_partial_run_is_a_warning_when_it_can_save_gracefully():
    """Reaching 40% of an epoch is fine *if* checkpoints and a graceful budget exist."""
    fails, warns, _ = validator.check(_job(hp={"max_train_seconds": "13000"}), SEC, ROWS)
    assert fails == []
    assert any("stop gracefully" in w for w in warns)


def test_missing_max_runtime_is_fatal():
    fails, _, _ = validator.check(_job(StoppingCondition={}), SEC, ROWS)
    assert any("MaxRuntimeInSeconds" in f for f in fails)


def test_unset_budget_warns_about_hard_kill():
    fails, warns, _ = validator.check(_job(hp={"max_train_seconds": "0"}), SEC, ROWS)
    assert any("hard kill" in w for w in warns)


def test_sagemaker_quotes_survive_parsing():
    """SageMaker round-trips hyperparameters as JSON strings: 50 arrives as '"50"'."""
    quoted = _job(hp={"save_steps": '"50"', "max_train_seconds": '"40500"'})
    fails, _, _ = validator.check(quoted, SEC, ROWS)
    assert fails == []


# ---------------------------------------------------------------- trainer helpers

def test_truthy_handles_sagemaker_quoted_booleans():
    for v in ("true", "True", '"true"', "1", "yes", True):
        assert truthy(v) is True, v
    for v in ("false", '"false"', "0", "no", "", None, False):
        assert truthy(v) is False, v


def test_newest_checkpoint_sorts_numerically_not_lexicographically(tmp_path):
    """checkpoint-100 must beat checkpoint-99; string sort would resume 89 steps back."""
    for step in (9, 20, 99, 100):
        (tmp_path / f"checkpoint-{step}").mkdir()
    assert newest_checkpoint(str(tmp_path)).endswith("checkpoint-100")


def test_newest_checkpoint_returns_none_when_nothing_to_resume(tmp_path):
    assert newest_checkpoint(str(tmp_path)) is None
    assert newest_checkpoint(str(tmp_path / "never-created")) is None
    (tmp_path / "not-a-checkpoint").mkdir()
    assert newest_checkpoint(str(tmp_path)) is None


# ------------------------------------------------------- the mirrored trainer's own rules
#
# The tests above prove the helpers work. They do NOT prove the trainer that gets deployed
# still USES them -- and that is exactly the gap the second trainer walked through: it had
# a truthy `truthy`, a working argparse and `save_strategy="no"`, so a suite full of helper
# tests stayed green on a trainer that could not deliver. These three read the mechanism at
# the mirrored path, one per numbered rule in its own docstring, so reverting a rule is a
# red test rather than a discovery in a post-mortem.

def test_rule_1_the_mirrored_trainer_checkpoints_periodically_to_the_synced_dir():
    assert 'save_strategy="steps"' in trainer_src, (
        "the deployed trainer no longer saves on a step schedule -- its only save point is "
        "then the end of training, which is what produced zero artifacts on run e1g6")
    assert "save_steps=args.save_steps" in trainer_src, "save_steps is declared but not wired"
    # SageMaker syncs THIS dir to CheckpointConfig.S3Uri; anywhere else dies with the container.
    assert '"--checkpoint_dir", type=str, default="/opt/ml/checkpoints"' in trainer_src
    assert "output_dir=args.checkpoint_dir" in trainer_src, (
        "checkpoints must be written where SageMaker syncs from, not into a scratch dir")


def test_rule_2_the_mirrored_trainer_stops_gracefully_on_a_wall_clock_budget():
    assert "TimeBudgetCallback" in trainer_src and "if args.max_train_seconds:" in trainer_src, (
        "without the budget callback the job is killed mid-step at MaxRuntime and never "
        "reaches save/merge/upload -- an adapter trained on 60% of the data is a deliverable")
    assert "control.should_training_stop = True" in trainer_src, (
        "the budget callback must ask the Trainer to stop, not raise: raising skips the "
        "save path, which is the failure it exists to prevent")


def test_rule_3_the_mirrored_trainer_resumes_from_the_newest_checkpoint():
    assert "resume = newest_checkpoint(args.checkpoint_dir)" in trainer_src
    assert "trainer.train(resume_from_checkpoint=resume)" in trainer_src, (
        "a checkpoint nothing resumes from is storage, not deliverability")


def test_the_preflight_ships_in_the_same_directory_the_trainer_is_mirrored_from():
    """The gate above is only reachable if the deploy carries it to where the agent looks.

    Measured before this was true: validate_job_config.py lived in pipeline/training/, which
    ensure_code() does not read, so it was mirrored nowhere, named by no prompt, and had ZERO
    callers -- while 4 of 4 real training jobs launched with save_steps unset, max_train_seconds
    unset and no CheckpointConfig at all, i.e. both of its hard FAILs, on every single one.
    """
    assert (MIRRORED / "validate_job_config.py").is_file()
    assert (MIRRORED / "requirements.txt").is_file(), (
        "the trainer's pinned floors must travel with it: liger-kernel and trl versions are "
        "why the fused-kernel path works, and a stale requirements.txt is a silent downgrade")
    text = json.loads((REPO / "agents/finetune/harness.json").read_text()
                      )["systemPrompt"][0]["text"]
    # BOTH halves, because either one alone is still zero callers: a prompt that says "run the
    # preflight" without naming the object to download tells the agent to run a file it does
    # not have, and a download nothing runs is a wasted GetObject.
    assert "code/distill/validate_job_config.py" in text, (
        "the launch prompt must name the preflight's exact S3 key -- listing is not granted "
        "to the harness role, so a key it cannot name is a key it cannot fetch")
    assert re.search(r"python[3]? validate_job_config\.py", text), (
        "a preflight the launch prompt never tells the agent to RUN is a preflight with no "
        "callers, which is exactly what it had for the entire life of the previous trainer")


# ------------------------------------------------- the mirror integrity check must be able to fail
#
# Measured on the real in-account mirror of the student model
# (runs/run-arc2v2-experiments/models/qwen3-4b-thinking/): the trainer read digests from
# `files_sha256`, that manifest names them under `files`, so the loop ran over ZERO files
# and the trainer printed "mirror verified: 0 files" and trained. Nothing was compared.
# Every test below pins the difference between "the bytes match what a human signed" and
# "there was nothing to compare", because a log line cannot tell them apart.

verify_mirror_manifest = _extract("verify_mirror_manifest")


def _mirror(tmp_path, files: dict, key="files_sha256", extra=None):
    """Write files into a fake synced mirror and return (dir, manifest dict)."""
    import hashlib
    digests = {}
    for name, body in files.items():
        (tmp_path / name).write_bytes(body)
        digests[name] = hashlib.sha256(body).hexdigest()
    mm = {"hf_repo": "Qwen/Qwen3-4B-Thinking-2507", key: digests}
    mm.update(extra or {})
    return str(tmp_path), mm


def test_a_manifest_with_no_digests_at_all_is_fatal(tmp_path):
    """The regression, reduced: nothing to check must never read as checked."""
    with pytest.raises(SystemExit) as e:
        verify_mirror_manifest(str(tmp_path), {"hf_repo": "x", "sha256": {"a": "b"}},
                               "/m/MODEL_MANIFEST.json")
    msg = str(e.value)
    assert "files_sha256" in msg and "'files'" in msg, \
        "the error must name the keys it looked for, or the next producer guesses again"
    assert "must not pass" in msg


def test_an_empty_digest_map_is_fatal(tmp_path):
    """`{"files_sha256": {}}` is the shape a half-written mirror leaves behind, and it is
    the one input for which the old code's `bad` list was empty for the right reason."""
    d, mm = _mirror(tmp_path, {})
    with pytest.raises(SystemExit):
        verify_mirror_manifest(d, mm, "/m/MODEL_MANIFEST.json")


def test_both_producers_key_names_are_understood(tmp_path):
    """`files_sha256` from the data-prep `mirror_model` task, `files` from the
    experiment-side mirror script. Two producers, one consumer; the consumer knowing only
    one of them is what made the check vacuous on a mirror that was actually intact."""
    for key in ("files_sha256", "files"):
        sub = tmp_path / key
        sub.mkdir()
        d, mm = _mirror(sub, {"tokenizer.json": b"{}", "config.json": b"{}"}, key=key)
        assert verify_mirror_manifest(d, mm, "/m/MODEL_MANIFEST.json") == 2


def test_the_count_it_reports_is_the_count_it_checked(tmp_path):
    """The printed number is the only evidence a human sees, so it has to come from the
    digests that were compared -- not from `len(mm)` or a key that may be absent."""
    d, mm = _mirror(tmp_path, {"a.json": b"1", "b.json": b"2", "c.json": b"3"})
    assert verify_mirror_manifest(d, mm, "/m") == 3


def test_a_file_the_manifest_names_but_the_sync_skipped_is_fatal(tmp_path):
    """`aws s3 sync` dropping one shard leaves every file that IS present matching."""
    d, mm = _mirror(tmp_path, {"model-00001.safetensors": b"weights"})
    mm["files_sha256"]["model-00002.safetensors"] = "0" * 64
    with pytest.raises(SystemExit) as e:
        verify_mirror_manifest(d, mm, "/m")
    assert "missing" in str(e.value)


def test_a_single_flipped_byte_is_fatal(tmp_path):
    """The check exists for tampering, so the control is one byte, not a truncated file."""
    d, mm = _mirror(tmp_path, {"config.json": b'{"a": 1}'})
    (tmp_path / "config.json").write_bytes(b'{"a": 2}')
    with pytest.raises(SystemExit) as e:
        verify_mirror_manifest(d, mm, "/m")
    assert "sha256 mismatch" in str(e.value)


def test_the_deployed_trainer_calls_the_guard_rather_than_hashing_inline():
    """A helper the trainer stopped using is the gap this file already documents once."""
    assert "verify_mirror_manifest(local_dir, mm, manifest_path)" in trainer_src
    assert "_hl" not in trainer_src, \
        "an inline hashing loop next to the guard means two checks with one tested"


def test_every_copy_of_the_trainer_in_the_repo_carries_the_guard():
    """There are two trainers in this repo and only one is deployed -- which is how the
    deliverability rules stayed unimplemented in the copy that ran. A guard applied to the
    mirrored copy alone is one `ensure_code()` edit away from being applied to none."""
    copies = sorted(REPO.glob("pipeline/**/train_qlora.py"))
    assert len(copies) >= 2, f"expected the known copies, found {copies}"
    for path in copies:
        src = path.read_text()
        assert "def verify_mirror_manifest(" in src, f"{path} has no integrity guard"
        assert 'mm.get("files") or {}' in src, f"{path} understands one producer only"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
