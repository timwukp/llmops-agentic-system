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
    default alone; "on"/"off" force it so both sides of a lift comparison are
    rendered identically and the choice is recorded in the run metadata.
    """
    messages = [{"role": "user", "content": prompt_text}]
    kwargs = {"tokenize": False, "add_generation_prompt": True}
    if thinking != "auto":
        kwargs["enable_thinking"] = thinking == "on"
    try:
        return tokenizer.apply_chat_template(messages, **kwargs)
    except TypeError:
        # Template predates enable_thinking; the caller's intent is unachievable
        # here, so fail loudly rather than silently rendering the other mode.
        raise SystemExit(
            f"--thinking {thinking} requested but this chat template does not "
            f"accept enable_thinking; re-run with --thinking auto")


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
    args = ap.parse_args()

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    rows = [json.loads(l) for l in open(args.val) if l.strip()]
    if args.limit:
        rows = rows[:args.limit]
    print(f"[gen] {len(rows)} prompts from {args.val}", flush=True)

    tokenizer = AutoTokenizer.from_pretrained(args.model_dir)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"          # required for correct batched decoding
    model = AutoModelForCausalLM.from_pretrained(
        args.model_dir, torch_dtype=torch.bfloat16, device_map="auto")
    model.eval()

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
                new_ids = seq[enc["input_ids"].shape[1]:]
                completion = tokenizer.decode(new_ids, skip_special_tokens=True)
                # A run that hit the token ceiling looks identical to a model that
                # cannot write code, so record it: the scorer's format failures can
                # then be attributed to budget rather than ability.
                truncated = len(new_ids) >= args.max_new_tokens
                n_truncated += truncated
                fh.write(json.dumps({"task_id": row["task_id"],
                                     "variant": row.get("variant", ""),
                                     "generation": completion,
                                     "n_new_tokens": len(new_ids),
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
        "max_new_tokens": args.max_new_tokens, "temperature": args.temperature,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
