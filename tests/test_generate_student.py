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
        # A single string is the un-padded measuring call, and the real tokenizer
        # answers it with that row's OWN length -- which is the only way to learn
        # whether a row was over the window, since the batched encoding below is
        # padded to the batch maximum and truncated to max_length. Modelled as one
        # token per character: crude, but it makes length a property of the text
        # instead of a constant, and a constant cannot express "this row was cut".
        if isinstance(texts, str):
            return _StubTensorDict(input_ids=[100 + i for i in range(len(texts))])
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
        k = kw.get("num_return_sequences") or 1
        # Row-major, like the real generate: all k samples of row 0, then row 1's.
        # A stub that returned one sequence per row regardless of k would leave the
        # i // k index arithmetic untested -- which is exactly the mapping that,
        # wrong, attaches one task's id to another task's generation.
        # The first generated id varies per sample so the samples are telling apart.
        return [_StubSeqTensor([100, 101, 102, 103, 104, 900 + s, 901, 902])
                for _ in range(n) for s in range(k)]


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


# ------------------------------------------------- input-window truncation reporting

def test_prompts_are_truncated_from_the_left_not_the_right(gs, tmp_path):
    """Right truncation deletes the generation prompt, which scores every long row 0.

    Qwen3 renders `<|im_start|>assistant\\n` at the END of the string, so cutting
    from the default right end removes exactly the tokens telling the model to
    start answering — it continues the grid instead of transforming it. Measured on
    the real Qwen3-1.7B tokenizer: a 43,898-token prompt right-truncated to 8192
    ends mid-word with no assistant turn at all. Nothing else in the pipeline can
    detect this: `truncated` measures the OUTPUT ceiling, so the run looks clean and
    simply reports the model cannot solve long tasks.
    """
    _run(gs, tmp_path, ROWS[:1])
    assert gs._stub_tokenizer.truncation_side == "left"


# The stub counts one token per character, so a prompt longer than the window
# passed alongside it is a cut row. Written relative to the flag rather than to a
# constant: the window moved from 8192 to 14336 once the real tokenizer was
# measured, and a test pinning the old number would have gone green by asserting
# that a row which now fits was still cut.
_WINDOW = 500
_LONG = "x" * (_WINDOW + 100)


def test_a_row_whose_prompt_exceeded_the_window_says_so(gs, tmp_path):
    """Left truncation keeps the row scorable but still drops its oldest context.

    That row was scored on an incomplete task description, so its failure is a data
    gap rather than model ability — and the scorer cannot tell the two apart unless
    the row carries the flag.
    """
    long_row = {"task_id": "big", "variant": "orig", "prompt": _LONG}
    got = _run(gs, tmp_path, [ROWS[0], long_row],
               "--input-window", str(_WINDOW))
    by_id = {g["task_id"]: g for g in got}
    assert by_id["big"]["prompt_truncated"] is True
    assert by_id["t0"]["prompt_truncated"] is False, "a short prompt must not be flagged"
    assert by_id["big"]["input_window"] == _WINDOW, \
        "the scorer's truncation caveat names this number; it must be the one used"


def test_the_same_row_is_not_cut_by_a_window_that_fits_it(gs, tmp_path):
    """The other half: a flag that is read but never compared against would report
    every long row cut, and the caveat would be permanent and meaningless."""
    got = _run(gs, tmp_path, [{"task_id": "big", "variant": "orig", "prompt": _LONG}],
               "--input-window", str(_WINDOW + 500))
    assert got[0]["prompt_truncated"] is False


def test_the_default_window_matches_what_the_trainer_can_hold(gs, tmp_path):
    """A generation window shorter than the training window scores rows on a prompt
    the trained model would have been given in full — measured at 8192 against a
    trainer default of 14336, roughly 10% of val rows lost context and came back
    looking like wrong programs."""
    import re
    trainer = (REPO / "pipeline/training/distill/train_qlora.py").read_text()
    m = re.search(r'"--max_length", type=int, default=(\d+)', trainer)
    assert m, "the trainer no longer declares --max_length; the pair is unpinned"
    assert gs.DEFAULT_INPUT_WINDOW == int(m.group(1))
    got = _run(gs, tmp_path, ROWS[:1])
    assert got[0]["input_window"] == gs.DEFAULT_INPUT_WINDOW


def test_the_done_marker_reports_truncation_coverage_not_just_volume(gs, tmp_path):
    """A run where half the prompts lost context is a different measurement from one
    where none did; `n_written` alone implies a completeness the run did not have."""
    val = tmp_path / "v.jsonl"
    val.write_text("".join(json.dumps(r) + "\n" for r in
                            [ROWS[0], {"task_id": "big", "variant": "orig",
                                       "prompt": _LONG}]))
    out = tmp_path / "g.jsonl"
    sys.argv = ["generate_student.py", "--model-dir", "/fake", "--val", str(val),
                "--out", str(out), "--input-window", str(_WINDOW)]
    assert gs.main() == 0
    meta = json.loads((tmp_path / "g.jsonl.done").read_text())
    assert meta["n_prompt_truncated"] == 1 and meta["n_written"] == 2
    assert meta["input_window"] == _WINDOW
    assert meta["truncation_side"] == "left", "the done marker must record which end was cut"
    assert meta["n_samples"] == 1 and meta["n_prompts"] == 2, \
        "a lift comparison must be able to see that both runs made the same attempts"


def test_truncation_is_counted_per_row_not_per_batch(gs, tmp_path):
    """Batched rows are padded to the batch maximum, so the encoding's width says
    nothing about any individual row — one long row must not flag its neighbours."""
    rows = [ROWS[0], {"task_id": "big", "variant": "orig", "prompt": _LONG},
            ROWS[1]]
    got = _run(gs, tmp_path, rows, "--batch-size", "3",   # all three in one batch
               "--input-window", str(_WINDOW))
    assert [g["prompt_truncated"] for g in got] == [False, True, False]


# ------------------------------------------------------- k attempts per task

def test_greedy_multi_sampling_is_refused_rather_than_silently_duplicated():
    """`do_sample` is `temperature > 0`, so two greedy samples are one answer twice.
    pass@2 over them equals pass@1 while the report claims two attempts — plausible
    numbers, and the second attempt never existed."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gs_nostub", REPO / "pipeline/v2/generate_student.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # no torch needed: the guard runs first
    with pytest.raises(SystemExit) as e:
        mod.check_sampling(2, 0.0)
    msg = str(e.value)
    assert "--n-samples" in msg and "--temperature" in msg
    assert mod.check_sampling(2, 0.7) is None
    assert mod.check_sampling(1, 0.0) is None, "the reproducible gate must still run"
    with pytest.raises(SystemExit):
        mod.check_sampling(0, 0.7)


def test_the_guard_runs_before_the_model_is_loaded(gs, tmp_path):
    """A rejected run must cost a second, not a model load — and the check has to be
    wired into main(), not merely defined."""
    val = tmp_path / "v.jsonl"
    val.write_text(json.dumps(ROWS[0]) + "\n")
    sys.argv = ["generate_student.py", "--model-dir", "/fake", "--val", str(val),
                "--out", str(tmp_path / "g.jsonl"), "--n-samples", "2"]
    with pytest.raises(SystemExit):
        gs.main()
    assert gs._stub_model.generate_kwargs == []


def test_each_sample_is_attributed_to_its_own_task(gs, tmp_path):
    """`num_return_sequences=k` returns row 0's k samples first, so the row index is
    i // k. Zipping the batch against the output directly would label row 0's second
    sample with row 1's task_id, and every generation after the first would then be
    scored against the wrong task's pairs — a shifted, plausible, wrong eval."""
    got = _run(gs, tmp_path, ROWS[:3], "--n-samples", "2", "--temperature", "0.7",
               "--batch-size", "3")
    assert [(g["task_id"], g["sample_idx"]) for g in got] == [
        ("t0", 0), ("t0", 1), ("t1", 0), ("t1", 1), ("t2", 0), ("t2", 1)]
    # The stub varies the first generated id per sample, so identical text here
    # would mean the same sequence was written twice under two labels.
    assert got[0]["generation"] != got[1]["generation"]
    assert gs._stub_model.generate_kwargs[-1]["num_return_sequences"] == 2


def test_a_multi_sample_run_records_its_k(gs, tmp_path):
    val = tmp_path / "v.jsonl"
    val.write_text(json.dumps(ROWS[0]) + "\n")
    out = tmp_path / "g.jsonl"
    sys.argv = ["generate_student.py", "--model-dir", "/fake", "--val", str(val),
                "--out", str(out), "--n-samples", "2", "--temperature", "0.7"]
    assert gs.main() == 0
    meta = json.loads((out.parent / "g.jsonl.done").read_text())
    assert meta["n_samples"] == 2 and meta["n_written"] == 2 and meta["n_prompts"] == 1


def test_the_output_ceiling_leaves_room_for_the_wrapped_targets(gs, tmp_path):
    """Measured with the real Qwen3 tokenizer over the 849-row corpus: solver code
    is p99 715 tokens and 983 at maximum once the augmentation wrapper is added, and
    a base model's <think> preamble sits on top of that in a lift comparison. At
    1024 a ceiling hit is indistinguishable from inability except via the flag."""
    _run(gs, tmp_path, ROWS[:1])
    assert gs._stub_model.generate_kwargs[-1]["max_new_tokens"] == 1536


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
