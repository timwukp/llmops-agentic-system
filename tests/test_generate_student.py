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
    # Distinct from pad_token_id on purpose: Qwen3's pad and eos differ (151643 vs
    # 151645), and code that conflates them mis-trims batched output.
    eos_token_id = 999
    padding_side = "right"

    def __init__(self):
        self.chat_template_calls = []

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False,
                            **kw):
        self.chat_template_calls.append((messages, add_generation_prompt, kw))
        out = f"<|user|>{messages[0]['content']}<|assistant|>"
        # Mirror the real Qwen3 template: only enable_thinking=False changes the
        # rendering. A stub that ignored the flag would trip the no-op guard and
        # stop modelling the model it stands in for.
        if kw.get("enable_thinking") is False:
            out += "<think>\n\n</think>\n\n"
        return out

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


class _StubSeqTensor(_StubSeq):
    """A returned sequence, mimicking the torch tensor API the code uses.

    Slicing a torch tensor yields a tensor, so the stub's slice must stay a stub —
    a plain list back from `__getitem__` would lose `.tolist()` and only the real
    torch path would be exercised.
    """
    def tolist(self):
        return list(self)

    def __getitem__(self, item):
        got = super().__getitem__(item)
        return _StubSeqTensor(got) if isinstance(item, slice) else got


class _StubModel:
    device = "cpu"
    # The real model carries one; the code reads it to learn every stop id.
    generation_config = types.SimpleNamespace(eos_token_id=None)

    def __init__(self):
        self.generate_kwargs = []
        self.moved_to = None

    def eval(self):
        return self

    def to(self, device):
        self.moved_to = device
        return self

    def generate(self, **kw):
        self.generate_kwargs.append(kw)
        n = kw["input_ids"].shape[0]
        # Echo the 5 prompt ids, then 3 "generated" ids 900,901,902.
        return [_StubSeqTensor([100, 101, 102, 103, 104, 900, 901, 902])
                for _ in range(n)]


@pytest.fixture
def gs(monkeypatch):
    """Import generate_student.py with torch/transformers stubbed out."""
    tok, model = _StubTokenizer(), _StubModel()

    torch = types.ModuleType("torch")
    torch.__version__ = "2.6.0"
    torch.bfloat16 = "bfloat16"
    torch.float32 = "float32"
    # No GPU in the test env — the code must ask rather than assume, so the stub
    # has to answer. `device_map="auto"` used to hide this question inside
    # accelerate; a real CPU run proved accelerate is not installed there.
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)

    class _NoGrad:
        def __enter__(self): return None
        def __exit__(self, *a): return False
    torch.no_grad = _NoGrad
    monkeypatch.setitem(sys.modules, "torch", torch)

    tf = types.ModuleType("transformers")
    tf.__version__ = "4.52.0"
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

def _fresh():
    """Import the module without the torch stubs — build_prompt/check_thinking_effect
    are pure and need no GPU stack."""
    spec = importlib.util.spec_from_file_location(
        "gs_bare", REPO / "pipeline/v2/generate_student.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_thinking_defaults_to_the_templates_own_behaviour():
    """"auto" must not pass enable_thinking at all — passing False by default would
    silently change how every model is prompted."""
    tok = _StubTokenizer()
    _fresh().build_prompt(tok, "puzzle", "auto")
    assert tok.chat_template_calls[-1][2] == {}


@pytest.mark.parametrize("mode,expected", [("on", True), ("off", False)])
def test_thinking_can_be_forced_so_both_sides_of_a_lift_run_match(gs, tmp_path,
                                                                 mode, expected):
    _run(gs, tmp_path, ROWS[:1], "--thinking", mode)
    assert gs._stub_tokenizer.chat_template_calls[-1][2] == {"enable_thinking": expected}


class _RealQwen3Template:
    """The actual Qwen3-1.7B chat template behaviour, verified 2026-07-31 against
    huggingface.co/Qwen/Qwen3-1.7B tokenizer_config.json.

    The template acts on enable_thinking ONLY to suppress thinking:
        {%- if enable_thinking is defined and enable_thinking is false %}
            {{- '<think>\\n\\n</think>\\n\\n' }}
    So False prefills an empty think block, while True and absent are byte-identical.
    Pinning it here means a wrong assumption about the flag fails in this suite
    rather than after a multi-hour GPU run.
    """
    def apply_chat_template(self, messages, tokenize=False,
                            add_generation_prompt=False, **kw):
        out = f"<|im_start|>user\n{messages[0]['content']}<|im_end|>\n"
        if add_generation_prompt:
            out += "<|im_start|>assistant\n"
            if kw.get("enable_thinking") is False:
                out += "<think>\n\n</think>\n\n"
        return out


class _IgnoresTheFlag:
    """A template with no enable_thinking in it. Crucially it does NOT raise —
    apply_chat_template forwards unknown kwargs into the Jinja context, so an
    unsupported flag is accepted in total silence."""
    def apply_chat_template(self, messages, tokenize=False,
                            add_generation_prompt=False, **kw):
        return f"<|user|>{messages[0]['content']}<|assistant|>"


def test_thinking_off_actually_suppresses_thinking_on_the_real_qwen3_template():
    gs = _fresh()
    tok = _RealQwen3Template()
    assert "<think>\n\n</think>" in gs.build_prompt(tok, "puzzle", "off")
    assert "<think>" not in gs.build_prompt(tok, "puzzle", "auto")


def test_thinking_on_is_a_no_op_on_qwen3_and_is_reported_as_such(capsys):
    """Explicit True and absent render identically, so `on` cannot be presented as
    having forced anything. It is legal — the intent is met — but it must be said."""
    gs = _fresh()
    tok = _RealQwen3Template()
    assert gs.build_prompt(tok, "puzzle", "on") == gs.build_prompt(tok, "puzzle", "auto")
    effect = gs.check_thinking_effect(tok, "on")
    assert effect == {"thinking": "on", "changed_rendering": False}
    assert "no-op" in capsys.readouterr().out


def test_thinking_off_against_a_template_that_ignores_the_flag_is_fatal():
    """The dangerous case: the flag is accepted silently, thinking is NOT suppressed,
    and a lift comparison would be measuring two identically prompted runs."""
    gs = _fresh()
    with pytest.raises(SystemExit, match="ignores enable_thinking"):
        gs.check_thinking_effect(_IgnoresTheFlag(), "off")


def test_thinking_effect_check_is_skipped_for_auto():
    assert _fresh().check_thinking_effect(_IgnoresTheFlag(), "auto") is None


def test_the_effect_check_runs_before_the_model_is_loaded(gs, tmp_path, monkeypatch):
    """Learning the flag is a no-op after weights are resident wastes the load."""
    order = []
    real_check = gs.check_thinking_effect
    monkeypatch.setattr(gs, "check_thinking_effect",
                        lambda *a, **k: (order.append("check"), real_check(*a, **k))[1])
    import transformers
    monkeypatch.setattr(transformers.AutoModelForCausalLM, "from_pretrained",
                        lambda *a, **k: (order.append("load"), gs._stub_model)[1])
    _run(gs, tmp_path, ROWS[:1], "--thinking", "on")
    assert order == ["check", "load"]


def test_truncated_generations_are_flagged_for_the_scorer(gs, tmp_path):
    """The stub emits 3 new tokens; a ceiling of 3 means it stopped because it ran
    out, which the scorer must be able to tell apart from an unwilling model."""
    got = _run(gs, tmp_path, ROWS[:1], "--max-new-tokens", "3")
    assert got[0]["truncated"] is True and got[0]["n_new_tokens"] == 3

    got = _run(gs, tmp_path, ROWS[:1], "--max-new-tokens", "64")
    assert got[0]["truncated"] is False


# ------------------------------------------- batched output is a padded rectangle

def test_a_row_that_stopped_on_eos_is_not_called_truncated():
    """`generate` returns a rectangle: every row is padded out to the longest row's
    length. Measured on a real batched run (2026-07-31) a row that stopped after 3
    tokens came back with 10 ids, so a naive `len(new_ids) >= max_new_tokens` marked
    a finished row as truncated — and the scorer would then excuse its format failure
    as a token-budget artefact when the model simply wrote something unparseable."""
    gs = _fresh()
    # 3 real tokens, EOS, then pad filler out to the batch width of 10.
    ids = [900, 901, 902, 999] + [0] * 6
    assert gs.trim_new_tokens(ids, {999}, 0, 10) == (4, False)


def test_a_row_that_genuinely_hit_the_ceiling_is_still_flagged():
    """The fix must not silence the real signal it exists to report."""
    gs = _fresh()
    assert gs.trim_new_tokens(list(range(800, 810)), {999}, 0, 10) == (10, True)


def test_trailing_pad_is_stripped_even_without_a_stop_token():
    """A row can be padded without an EOS in it (another row stopped later and the
    stop token got cut by the ceiling), so pad-stripping is a second, independent
    signal — length comes from what the model actually emitted."""
    gs = _fresh()
    assert gs.trim_new_tokens([900, 901, 0, 0, 0], {999}, 0, 5) == (2, False)


def test_pad_inside_real_output_is_not_stripped():
    """Only a TRAILING run is filler; a pad id the model emitted mid-answer is
    output, and eating it would shorten the count of a row that never stopped."""
    gs = _fresh()
    assert gs.trim_new_tokens([900, 0, 901, 0, 0], {999}, 0, 5) == (3, False)


def test_stop_token_wins_over_pad_when_they_are_the_same_id():
    """Some models reuse eos as pad. The stop signal must be read first, otherwise
    every stopped row is stripped to length 0 and looks like it emitted nothing."""
    gs = _fresh()
    assert gs.trim_new_tokens([900, 901, 7, 7, 7], {7}, 7, 5) == (3, False)


def test_batched_generation_flags_only_the_row_that_ran_out(gs, tmp_path,
                                                            monkeypatch):
    """End-to-end over main(): two rows in one batch, one stopping early. Only the
    row that used the whole budget may be counted into the truncation total."""
    def ragged(**kw):
        gs._stub_model.generate_kwargs.append(kw)
        prompt = [100, 101, 102, 103, 104]
        return [_StubSeqTensor(prompt + [900, 901, 999, 0, 0]),   # stopped at 3
                _StubSeqTensor(prompt + [910, 911, 912, 913, 914])]  # hit ceiling
    monkeypatch.setattr(gs._stub_model, "generate", ragged)
    got = _run(gs, tmp_path, ROWS[:2], "--batch-size", "2", "--max-new-tokens", "5")
    assert [g["truncated"] for g in got] == [False, True]
    assert [g["n_new_tokens"] for g in got] == [3, 5]
    meta = json.loads((tmp_path / "gen.jsonl.done").read_text())
    assert meta["n_truncated"] == 1


@pytest.mark.parametrize("cfg_eos", [901, [901, 42]])
def test_stop_ids_include_the_generation_configs_own_eos(gs, tmp_path, monkeypatch,
                                                         cfg_eos):
    """Qwen3's generation_config carries the halt token, as an int or a LIST, and it
    can differ from the tokenizer's. A stop id the trimmer does not know about looks
    like real output, so the row is measured longer than it is.

    Id 901 is reachable ONLY through generation_config here (the stub tokenizer's eos
    is 999 and never appears), so deleting that lookup fails this test — which is what
    a mutation run confirmed the earlier version of it did not do."""
    monkeypatch.setattr(gs._stub_model, "generation_config",
                        types.SimpleNamespace(eos_token_id=cfg_eos))

    def ragged(**kw):
        gs._stub_model.generate_kwargs.append(kw)
        # 900, then the config's stop id, then filler that is NOT pad (so pad
        # stripping cannot rescue the count either).
        return [_StubSeqTensor([100, 101, 102, 103, 104, 900, 901, 5, 5, 5])]
    monkeypatch.setattr(gs._stub_model, "generate", ragged)
    got = _run(gs, tmp_path, ROWS[:1], "--max-new-tokens", "5")
    assert got[0]["n_new_tokens"] == 2 and got[0]["truncated"] is False


# --------------------------------------------------- device and dtype resolution

def test_cpu_gets_float32_because_bfloat16_on_cpu_is_unusable(gs):
    torch = sys.modules["torch"]
    device, kw = gs.resolve_device_and_dtype(torch, sys.modules["transformers"], "auto")
    assert device == "cpu" and list(kw.values()) == ["float32"]


def test_cuda_gets_bfloat16(gs, monkeypatch):
    torch = sys.modules["torch"]
    monkeypatch.setattr(torch, "cuda", types.SimpleNamespace(is_available=lambda: True))
    device, kw = gs.resolve_device_and_dtype(torch, sys.modules["transformers"], "auto")
    assert device == "cuda" and list(kw.values()) == ["bfloat16"]


def test_the_dtype_kwarg_follows_the_installed_transformers_major(gs):
    """`torch_dtype` was renamed to `dtype` in transformers 5, and the training
    requirements pin only a FLOOR (>=4.52), so which name is correct is decided at
    runtime by whatever the DLC installed."""
    torch = sys.modules["torch"]
    tf4 = types.SimpleNamespace(__version__="4.52.0")
    tf5 = types.SimpleNamespace(__version__="5.14.1")
    assert list(gs.resolve_device_and_dtype(torch, tf4, "cpu")[1]) == ["torch_dtype"]
    assert list(gs.resolve_device_and_dtype(torch, tf5, "cpu")[1]) == ["dtype"]


def test_the_model_is_moved_without_accelerate(gs, tmp_path):
    """`device_map="auto"` makes accelerate a hard requirement — transformers raises
    ValueError if it is missing, which is how a real CPU run failed on 2026-07-31.
    A 1.7B model on one GPU gains nothing from it, so the model is moved explicitly."""
    _run(gs, tmp_path, ROWS[:1])
    assert gs._stub_model.moved_to == "cpu"
    load_kw = getattr(gs._stub_model, "load_kwargs", None)
    assert load_kw is None or "device_map" not in load_kw


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
