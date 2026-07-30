"""Tests for the GPU-side generator, run with stub torch/transformers.

Why bother stubbing: `generate_student.py` only ever runs after a multi-hour
training job finishes, on a machine this suite can't reach. A bug in its prompt
slicing or its output plumbing would therefore surface at the worst possible
moment, after the GPU cost has already been paid. The two things worth pinning
are cheap to pin without a GPU:

  1. the completion is sliced from the prompt correctly (an off-by-one here
     silently prepends prompt text to every generation, or eats the first token)
  2. results are flushed incrementally, so an interrupted run stays scorable

Run: .venv/bin/python -m pytest tests/test_generate_student.py -q
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- stub the GPU stack

class _StubTensorDict(dict):
    """Mimics a BatchEncoding: subscriptable, and .to(device) returns itself."""
    def to(self, _device):
        return self


class _StubSeq(list):
    pass


class _StubTokenizer:
    """Token id N stands for the string f"t{N} ". Prompts are ids 100..100+n."""
    pad_token = None
    eos_token = "<eos>"
    pad_token_id = 0
    padding_side = "right"

    def __init__(self):
        self.chat_template_calls = []

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False,
                            **kw):
        self.chat_template_calls.append((messages, add_generation_prompt, kw))
        return f"<|user|>{messages[0]['content']}<|assistant|>"

    def __call__(self, texts, return_tensors=None, padding=None, truncation=None,
                 max_length=None):
        # Uniform prompt length so the slice offset is unambiguous.
        ids = [[100 + i for i in range(5)] for _ in texts]
        return _StubTensorDict(input_ids=_StubShape(ids))

    def decode(self, seq, skip_special_tokens=True):
        return " ".join(f"t{int(i)}" for i in seq)


class _StubShape(list):
    @property
    def shape(self):
        return (len(self), len(self[0]) if self else 0)


class _StubModel:
    device = "cpu"

    def __init__(self):
        self.generate_kwargs = []

    def eval(self):
        return self

    def generate(self, **kw):
        self.generate_kwargs.append(kw)
        n = kw["input_ids"].shape[0]
        # Echo the 5 prompt ids, then 3 "generated" ids 900,901,902.
        return [_StubSeq([100, 101, 102, 103, 104, 900, 901, 902]) for _ in range(n)]


@pytest.fixture
def gs(monkeypatch):
    """Import generate_student.py with torch/transformers stubbed out."""
    tok, model = _StubTokenizer(), _StubModel()

    torch = types.ModuleType("torch")
    torch.bfloat16 = "bfloat16"

    class _NoGrad:
        def __enter__(self): return None
        def __exit__(self, *a): return False
    torch.no_grad = _NoGrad
    monkeypatch.setitem(sys.modules, "torch", torch)

    tf = types.ModuleType("transformers")
    tf.AutoTokenizer = types.SimpleNamespace(from_pretrained=lambda *a, **k: tok)
    tf.AutoModelForCausalLM = types.SimpleNamespace(from_pretrained=lambda *a, **k: model)
    monkeypatch.setitem(sys.modules, "transformers", tf)

    spec = importlib.util.spec_from_file_location(
        "generate_student", REPO / "pipeline/v2/generate_student.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._stub_tokenizer, mod._stub_model = tok, model
    return mod


def _run(gs, tmp_path, rows, *extra):
    val = tmp_path / "val.jsonl"
    val.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = tmp_path / "gen.jsonl"
    sys.argv = ["generate_student.py", "--model-dir", "/fake", "--val", str(val),
                "--out", str(out), *extra]
    assert gs.main() == 0
    return [json.loads(l) for l in out.read_text().splitlines() if l.strip()]


ROWS = [{"task_id": f"t{i}", "variant": "orig", "prompt": f"puzzle {i}"} for i in range(4)]


def test_completion_excludes_the_prompt(gs, tmp_path):
    """The 5 prompt ids must be sliced off; only ids 900-902 are the answer.

    Getting this wrong feeds the prompt back into the scorer as if the model had
    written it — the prompt contains the expected output grids, so the eval would
    score against text the model never generated.
    """
    got = _run(gs, tmp_path, ROWS[:1])
    assert got[0]["generation"] == "t900 t901 t902"
    assert "t100" not in got[0]["generation"]


def test_every_row_is_emitted_with_its_identity(gs, tmp_path):
    got = _run(gs, tmp_path, ROWS)
    assert [g["task_id"] for g in got] == ["t0", "t1", "t2", "t3"]
    assert all(g["variant"] == "orig" for g in got)


def test_limit_truncates_the_workload(gs, tmp_path):
    assert len(_run(gs, tmp_path, ROWS, "--limit", "2")) == 2


def test_batching_covers_all_rows_including_a_ragged_final_batch(gs, tmp_path):
    got = _run(gs, tmp_path, ROWS, "--batch-size", "3")   # 4 rows -> 3 + 1
    assert len(got) == 4
    assert len(gs._stub_model.generate_kwargs) == 2


def test_greedy_by_default_and_sampling_only_when_asked(gs, tmp_path):
    _run(gs, tmp_path, ROWS[:1])
    kw = gs._stub_model.generate_kwargs[-1]
    assert kw["do_sample"] is False
    assert kw["temperature"] is None, "a temperature with do_sample=False warns and misleads"

    _run(gs, tmp_path, ROWS[:1], "--temperature", "0.7")
    kw = gs._stub_model.generate_kwargs[-1]
    assert kw["do_sample"] is True and kw["temperature"] == 0.7


def test_left_padding_is_set_before_generation(gs, tmp_path):
    """Right padding puts pad tokens between the prompt and the generation, which
    corrupts batched decoding. This is silent — the output is just worse."""
    _run(gs, tmp_path, ROWS, "--batch-size", "2")
    assert gs._stub_tokenizer.padding_side == "left"


def test_pad_token_falls_back_to_eos(gs, tmp_path):
    _run(gs, tmp_path, ROWS[:1])
    assert gs._stub_tokenizer.pad_token == "<eos>"


def test_prompt_goes_through_the_chat_template_with_a_generation_prompt(gs, tmp_path):
    """Training used apply_chat_template; inference must too, or the student never
    sees the format it was trained on and the solve rate collapses for no reason."""
    _run(gs, tmp_path, ROWS[:1])
    messages, add_gen, _ = gs._stub_tokenizer.chat_template_calls[-1]
    assert messages == [{"role": "user", "content": "puzzle 0"}]
    assert add_gen is True


# ---------------------------------- thinking mode + truncation accounting

def test_thinking_defaults_to_the_templates_own_behaviour():
    """"auto" must not pass enable_thinking at all — passing False by default would
    silently change how every model is prompted."""
    tok = _StubTokenizer()
    spec = importlib.util.spec_from_file_location(
        "gs_bare", REPO / "pipeline/v2/generate_student.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.build_prompt(tok, "puzzle", "auto")
    assert tok.chat_template_calls[-1][2] == {}


@pytest.mark.parametrize("mode,expected", [("on", True), ("off", False)])
def test_thinking_can_be_forced_so_both_sides_of_a_lift_run_match(gs, tmp_path,
                                                                 mode, expected):
    _run(gs, tmp_path, ROWS[:1], "--thinking", mode)
    assert gs._stub_tokenizer.chat_template_calls[-1][2] == {"enable_thinking": expected}


def test_forcing_thinking_on_an_old_template_fails_loudly(gs):
    """Silently rendering the OTHER mode would make a lift comparison measure the
    template instead of the fine-tuning."""
    class _OldTokenizer:
        def apply_chat_template(self, messages, tokenize=False,
                                add_generation_prompt=False):
            return "rendered"
    with pytest.raises(SystemExit, match="does not accept enable_thinking"):
        gs.build_prompt(_OldTokenizer(), "puzzle", "off")
    assert gs.build_prompt(_OldTokenizer(), "puzzle", "auto") == "rendered"


def test_truncated_generations_are_flagged_for_the_scorer(gs, tmp_path):
    """The stub emits 3 new tokens; a ceiling of 3 means it stopped because it ran
    out, which the scorer must be able to tell apart from an unwilling model."""
    got = _run(gs, tmp_path, ROWS[:1], "--max-new-tokens", "3")
    assert got[0]["truncated"] is True and got[0]["n_new_tokens"] == 3

    got = _run(gs, tmp_path, ROWS[:1], "--max-new-tokens", "64")
    assert got[0]["truncated"] is False


def test_done_marker_records_the_settings_a_lift_comparison_must_match(gs, tmp_path):
    val = tmp_path / "v.jsonl"
    val.write_text(json.dumps(ROWS[0]) + "\n")
    out = tmp_path / "g.jsonl"
    sys.argv = ["generate_student.py", "--model-dir", "/fake/base", "--val", str(val),
                "--out", str(out), "--thinking", "off"]
    assert gs.main() == 0
    meta = json.loads((tmp_path / "g.jsonl.done").read_text())
    assert meta["thinking"] == "off" and meta["model_dir"] == "/fake/base"
    assert meta["n_written"] == 1 and meta["temperature"] == 0.0


def test_output_is_scorable_by_the_eval_module(gs, tmp_path):
    """The two stages must actually agree on a schema, not just look like they do."""
    spec = importlib.util.spec_from_file_location(
        "eval_student", REPO / "pipeline/v2/eval_student.py")
    es = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(es)

    got = _run(gs, tmp_path, ROWS[:1])
    rep = es.score_generations(got, [{"task_id": "t0", "variant": "orig",
                                     "prompt": "Input (1x1):\n1\nOutput (1x1):\n2\n"}])
    # The stub emits gibberish, so it must score 0 — but it must SCORE, not crash.
    assert rep["n_generations"] == 1
    assert rep["results"][0]["status"] == "no_transform_emitted"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
