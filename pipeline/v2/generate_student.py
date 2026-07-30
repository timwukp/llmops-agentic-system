"""Generate ARC `transform` code from a fine-tuned student, for eval scoring.

Runs where the GPU is (SageMaker processing/training job, or any CUDA box); writes
`generations.jsonl` that `eval_student.py score` consumes on CPU. Keeping generation
and scoring in separate processes means a scoring bug never costs GPU time to re-fix,
and the scorer stays testable without torch installed.

Greedy decoding by default: the eval gate must be reproducible, and sampling would
make the solve rate a distribution rather than a number.

Usage (on the GPU host):
    python generate_student.py --model-dir /opt/ml/model/merged \
        --val val_raw.jsonl --out generations.jsonl --limit 200

Writes incrementally, so an interrupted run still yields a scorable partial file.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def build_prompt(tokenizer, prompt_text: str, thinking: str = "auto") -> str:
    """Render the eval prompt through the SAME chat template used in training.

    A mismatch here silently tanks the solve rate while the model is fine — the
    student never sees the format it was trained on. Training called
    apply_chat_template over [user, assistant]; inference renders the user turn
    plus the generation prompt, so the model resumes exactly where its targets began.

    Training targets are BARE code (no ``` fence, no <think>), so the scorer's
    unfenced-extraction path is the expected one — `eval_student.extract_code`
    handles fenced output too, in case the student picks the habit up anyway.

    `thinking` controls Qwen3's `enable_thinking` template switch, and it matters
    most for the UN-fine-tuned base model in a lift comparison: a base Qwen3 opens
    with a long <think> chain and can spend the whole token budget there, emitting
    no `transform` at all. Scored naively that reads as "the base model can't write
    code", when the real cause is a decoding budget. "auto" leaves the template
    default alone; "on"/"off" pass the flag explicitly.

    Verified against the real Qwen3-1.7B template (2026-07-31): the flag arrives as
    a Jinja variable, and the template only acts on it to SUPPRESS thinking —
    `enable_thinking=False` prefills an empty `<think></think>` pair, while True and
    absent render byte-identically. So `off` is the only mode that changes anything
    on this model, and a template that ignores the flag entirely accepts it in
    silence. `check_thinking_effect` is what catches that; see its docstring.
    """
    messages = [{"role": "user", "content": prompt_text}]
    kwargs = {"tokenize": False, "add_generation_prompt": True}
    if thinking != "auto":
        kwargs["enable_thinking"] = thinking == "on"
    return tokenizer.apply_chat_template(messages, **kwargs)


def check_thinking_effect(tokenizer, thinking: str) -> dict | None:
    """Confirm `--thinking` actually changes the rendering before spending GPU time.

    An unsupported flag does NOT raise: `apply_chat_template(**kwargs)` forwards
    extras into the Jinja context, so a template with no `enable_thinking` in it
    renders exactly as if the flag were absent and reports nothing. Asking the
    template a question is therefore the only honest check — render a probe both
    ways and compare.

    `off` that changes nothing is fatal: the suppression the caller asked for did
    not happen, so a lift comparison would silently be measuring two identically
    prompted runs. `on` that changes nothing is merely informational, because on
    Qwen3 explicit-True and absent are the same string — the caller's intent is
    satisfied, just not by the flag.
    """
    if thinking == "auto":
        return None
    probe = [{"role": "user", "content": "probe"}]
    kw = {"tokenize": False, "add_generation_prompt": True}
    default = tokenizer.apply_chat_template(probe, **kw)
    forced = tokenizer.apply_chat_template(probe, enable_thinking=thinking == "on", **kw)
    changed = forced != default

    if not changed and thinking == "off":
        raise SystemExit(
            "--thinking off did not change the rendered prompt: this chat template "
            "ignores enable_thinking, so thinking is NOT suppressed. Comparing this "
            "run against another would measure nothing. Re-run with --thinking auto "
            "and raise --max-new-tokens instead.")
    if not changed:
        print(f"[gen] note: --thinking {thinking} renders identically to the "
              f"template default, so the flag is a no-op here (expected on Qwen3, "
              f"whose template only acts on enable_thinking=False)", flush=True)
    return {"thinking": thinking, "changed_rendering": changed}


def trim_new_tokens(new_ids: list[int], stop_ids: set[int], pad_id: int | None,
                    max_new_tokens: int) -> tuple[int, bool]:
    """Return this row's real new-token count and whether it hit the ceiling.

    With `--batch-size > 1`, `generate` returns a rectangle: every row is padded out
    to the LONGEST row's length, so `len(new_ids)` is the batch maximum, not the
    row's own output length. Measured on a real batched run (2026-07-31): a row that
    stopped after 3 tokens came back with 10 ids, i.e. `len(new_ids) >= max_new_tokens`
    reported `truncated=True` for a row that had finished cleanly. Every such row
    would then be counted into the scorer's "ran out of tokens" caveat and excuse a
    format failure that the token budget did not cause.

    Two signals give the true length, in this order:
      1. a stop token in the row — everything after it is filler, so the row ended
         on its own terms and is by definition NOT truncated;
      2. otherwise a trailing run of pad ids — stripped, since the model emitted
         nothing there. A pad id that is ALSO a stop id is handled by (1) first.

    Only after that does `>= max_new_tokens` mean the ceiling was hit.
    """
    for i, tid in enumerate(new_ids):
        if tid in stop_ids:
            return i + 1, False
    end = len(new_ids)
    if pad_id is not None:
        while end > 0 and new_ids[end - 1] == pad_id:
            end -= 1
    return end, end >= max_new_tokens


def resolve_device_and_dtype(torch, transformers, requested: str) -> tuple[str, object]:
    """Pick the device and load dtype, without dragging in `accelerate`.

    `device_map="auto"` buys nothing for a 1.7B model on one GPU and turns
    `accelerate` into a hard requirement (transformers raises if it is missing),
    so the model is loaded plainly and moved with `.to(device)` instead. That also
    makes a CPU dry-run possible, which is how the two bugs in this function's
    first version were found before they could cost GPU time.

    bfloat16 on CPU is unusably slow and unsupported for some kernels, so CPU gets
    float32 — a dry-run checks the code path, never the numbers.

    `dtype=` replaced `torch_dtype=` in transformers 5; passing the old name only
    warns today, but the DLC installs `transformers>=4.52` with no upper pin, so
    which name is correct is a runtime fact, not a build-time one.
    """
    device = requested
    if requested == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float32 if device == "cpu" else torch.bfloat16
    major = int(transformers.__version__.split(".")[0])
    return device, {"dtype" if major >= 5 else "torch_dtype": dtype}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True, help="merged model dir (or HF id)")
    ap.add_argument("--val", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = all rows")
    ap.add_argument("--max-new-tokens", type=int, default=1024)
    ap.add_argument("--temperature", type=float, default=0.0,
                    help="0 = greedy; keep it 0 for a reproducible gate")
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--thinking", choices=["auto", "on", "off"], default="auto",
                    help="Qwen3 enable_thinking. Use the SAME value for the "
                         "fine-tuned and base runs of a lift comparison")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto",
                    help="cpu is for dry-running this script's code path only; "
                         "the numbers from a CPU run are not an eval")
    args = ap.parse_args()

    import torch
    import transformers
    from transformers import AutoModelForCausalLM, AutoTokenizer

    rows = [json.loads(l) for l in open(args.val) if l.strip()]
    if args.limit:
        rows = rows[:args.limit]
    print(f"[gen] {len(rows)} prompts from {args.val}", flush=True)

    tokenizer = AutoTokenizer.from_pretrained(args.model_dir)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"          # required for correct batched decoding
    # Before loading weights: a --thinking flag the template ignores would make a
    # lift comparison meaningless, and that is cheaper to learn now than after the
    # model is resident and the run is hours in.
    thinking_effect = check_thinking_effect(tokenizer, args.thinking)

    device, dtype_kw = resolve_device_and_dtype(torch, transformers, args.device)
    print(f"[gen] device={device} {list(dtype_kw)[0]}={list(dtype_kw.values())[0]} "
          f"torch={torch.__version__} transformers={transformers.__version__}",
          flush=True)
    model = AutoModelForCausalLM.from_pretrained(args.model_dir, **dtype_kw)
    model.to(device)
    model.eval()

    # Qwen3 stops on <|im_end|> but ships several special ids; a stop token the
    # trimmer does not know about looks like real output, so collect every id the
    # model could legitimately halt on (generation_config wins where they differ).
    stop_ids = {i for i in [tokenizer.eos_token_id] if i is not None}
    gen_eos = getattr(model.generation_config, "eos_token_id", None)
    if isinstance(gen_eos, int):
        stop_ids.add(gen_eos)
    elif isinstance(gen_eos, (list, tuple)):
        stop_ids.update(i for i in gen_eos if isinstance(i, int))
    print(f"[gen] stop ids {sorted(stop_ids)}, pad {tokenizer.pad_token_id}", flush=True)

    started = time.time()
    written = n_truncated = 0
    with open(args.out, "w") as fh:
        for start in range(0, len(rows), args.batch_size):
            batch = rows[start:start + args.batch_size]
            texts = [build_prompt(tokenizer, r["prompt"], args.thinking) for r in batch]
            enc = tokenizer(texts, return_tensors="pt", padding=True,
                            truncation=True, max_length=8192).to(model.device)

            with torch.no_grad():
                out = model.generate(
                    **enc, max_new_tokens=args.max_new_tokens,
                    do_sample=args.temperature > 0,
                    temperature=args.temperature if args.temperature > 0 else None,
                    pad_token_id=tokenizer.pad_token_id)

            for row, seq in zip(batch, out):
                # Slice off the prompt so only the completion is scored.
                new_ids = seq[enc["input_ids"].shape[1]:].tolist()
                completion = tokenizer.decode(new_ids, skip_special_tokens=True)
                # A run that hit the token ceiling looks identical to a model that
                # cannot write code, so record it: the scorer's format failures can
                # then be attributed to budget rather than ability. Batched rows are
                # padded to the batch maximum, so the count has to be measured, not
                # taken from the tensor width.
                n_new, truncated = trim_new_tokens(
                    new_ids, stop_ids, tokenizer.pad_token_id, args.max_new_tokens)
                n_truncated += truncated
                fh.write(json.dumps({"task_id": row["task_id"],
                                     "variant": row.get("variant", ""),
                                     "generation": completion,
                                     "n_new_tokens": n_new,
                                     "truncated": bool(truncated)}) + "\n")
                written += 1
            fh.flush()                        # partial file stays scorable

            if written % 20 == 0 or written == len(rows):
                rate = written / max(time.time() - started, 1e-9)
                print(f"[gen] {written}/{len(rows)} ({rate:.2f}/s)", flush=True)

    print(f"[gen] wrote {written} generations to {args.out} "
          f"in {(time.time() - started) / 60:.1f} min", flush=True)
    if n_truncated:
        print(f"[gen] WARNING {n_truncated}/{written} hit the "
              f"{args.max_new_tokens}-token ceiling; their format failures are a "
              f"budget artefact, not evidence the model cannot write code",
              flush=True)
    # Metadata a lift comparison must agree on: comparing a --thinking off run
    # against a --thinking auto one measures the template, not the fine-tuning.
    Path(args.out + ".done").write_text(json.dumps({
        "n_written": written, "n_truncated": n_truncated,
        "model_dir": args.model_dir, "thinking": args.thinking,
        "thinking_changed_rendering": (thinking_effect or {}).get("changed_rendering"),
        "max_new_tokens": args.max_new_tokens, "temperature": args.temperature,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
