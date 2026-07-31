"""Unit tests for training-job deliverability — no AWS calls, no GPU.

The regression these guard is expensive and silent: run v2-code-distill-0001-e1g6
trained healthily for 43 GPU-minutes and delivered zero artifacts, because its only
save point was arithmetically unreachable inside its own MaxRuntimeInSeconds. Nothing
crashed, so nothing warned. These tests encode the arithmetic that should have.

Run: .venv/bin/python -m pytest tests/test_training_deliverability.py -q
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


validator = _load("validate_job_config", "pipeline/training/validate_job_config.py")
# train_qlora imports torch/trl at module scope, so pull the two pure helpers out of
# the source text instead of importing the module (these tests must run without a GPU).
trainer_src = (REPO / "pipeline/training/train_qlora.py").read_text()


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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
