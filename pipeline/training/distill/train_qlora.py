#!/usr/bin/env python
"""QLoRA SFT distillation trainer for SageMaker script mode (TRL + PEFT, 4-bit NF4).

Trains a small student (default Qwen/Qwen3-1.7B) on teacher reasoning traces in
TRL "messages" JSONL format (reasoning kept as <think>...</think>).

Contract (SageMaker hyperparameters -> argparse):
  --model_id --model_revision --epochs --learning_rate --max_length
  --per_device_batch_size --gradient_accumulation --merge_adapters --warmup_ratio
  [--lora_r --lora_alpha --lora_dropout --seed]
  --save_steps --max_train_seconds --drop_overlong --max_val_rows --checkpoint_dir

Channels: SM_CHANNEL_TRAIN, SM_CHANNEL_VAL (dirs containing *.jsonl).
Outputs:  SM_MODEL_DIR/adapter, SM_MODEL_DIR/merged (if merge_adapters),
          metrics.json in SM_MODEL_DIR and SM_OUTPUT_DATA_DIR.

DELIVERABILITY RULES (learned the hard way — v2 run e1g6 was killed at 39% of
step 1 of 1265 with save_strategy="no" and produced ZERO artifacts):
  1. Never leave the only save point beyond the job's own MaxRuntime. Checkpoint
     every --save_steps into /opt/ml/checkpoints, which SageMaker CheckpointConfig
     syncs to S3 continuously — surviving a stop, a spot reclaim, or a hard kill.
  2. Stop GRACEFULLY on a wall-clock budget (--max_train_seconds) so the run always
     reaches the save/merge path instead of being killed mid-step. An adapter
     trained on 60% of the data is a deliverable; a killed job is not.
  3. Resume automatically from the newest checkpoint if one is present.

CANONICAL, and the ONLY trainer. There were two: this one -- fixed, unit-tested, and
verified on five real ml.g5.2xlarge jobs (checkpoints reaching S3 mid-run, a budget trip
that ended Completed, resume-from-newest proven end to end) -- and a second copy under
distill/ that the FINETUNE agent authored on run-20260811T165529Z-ce628817 and that was
promoted to "canonical" because it trained Qwen3-8B in 929s. Promoting those bytes
silently reverted all three rules above (it carried save_strategy="no" -- the exact line
this docstring names as the cause of the e1g6 zero-artifact loss), plus the liger-kernel
preflight and the model-mirror integrity check. Everything guarding deliverability read
THIS file, which was mirrored nowhere and named by no prompt, so the guards stayed green
about a file no run could reach. One file now, at the path deploy/03_storage.py mirrors:
the tests read whatever that function uploads, so a trainer nothing deploys cannot be
green again. --model_revision is the one thing the agent's copy had that this did not.
"""
import argparse, gc, glob, inspect, json, math, os, re, time


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model_id", type=str, default="Qwen/Qwen3-1.7B")
    p.add_argument("--model_revision", type=str, default="main",
                   help="pinned HF revision; what the plan named is what trains. "
                        "Ignored for an s3:// mirror, which is pinned by its manifest")
    p.add_argument("--epochs", type=float, default=3)
    p.add_argument("--learning_rate", type=float, default=2e-4)
    p.add_argument("--max_length", type=int, default=14336)
    p.add_argument("--per_device_batch_size", type=int, default=1)
    p.add_argument("--gradient_accumulation", type=int, default=4)
    p.add_argument("--merge_adapters", type=str, default="true")
    p.add_argument("--lora_r", type=int, default=16)
    p.add_argument("--lora_alpha", type=int, default=32)
    p.add_argument("--lora_dropout", type=float, default=0.05)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--warmup_ratio", type=float, default=0.1,
                   help="was hardcoded at this value; a declared knob because the agent "
                        "passed --warmup_ratio 0.1 on run-20260812T035446Z-dedaa965 and "
                        "argparse rejects what it does not declare")
    # deliverability controls
    p.add_argument("--save_steps", type=int, default=100,
                   help="checkpoint cadence; 0 disables periodic saves (NOT recommended)")
    p.add_argument("--max_steps", type=int, default=0,
                   help="stop after N optimizer steps. 0 = run the epochs. This exists for "
                        "the preflight the v2 plan requires: measure s/step and peak VRAM on "
                        "the target instance at the real max_length BEFORE signing a "
                        "multi-hour run. --max_train_seconds cannot do it -- a wall-clock "
                        "budget gives you whatever step count it gives you, and the step "
                        "count is the thing being measured. A run capped this way reports "
                        "completed_fraction against the FULL run, not against the cap")
    p.add_argument("--max_train_seconds", type=float, default=0,
                   help="graceful stop budget for the TRAINING loop only; leave headroom "
                        "under MaxRuntimeInSeconds for eval+merge+upload. 0 = unlimited")
    p.add_argument("--drop_overlong", type=str, default="true",
                   help="drop rows longer than max_length instead of truncating them "
                        "(a truncated target teaches the model to emit unfinished code)")
    p.add_argument("--max_val_rows", type=int, default=250,
                   help="cap the eval set; eval runs at batch size 1 so a full 1k split "
                        "costs ~25 GPU-minutes for a number we only read as a trend")
    p.add_argument("--train_dir", type=str, default=os.environ.get("SM_CHANNEL_TRAIN", "/opt/ml/input/data/train"))
    p.add_argument("--val_dir", type=str, default=os.environ.get("SM_CHANNEL_VAL", "/opt/ml/input/data/val"))
    p.add_argument("--model_dir", type=str, default=os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
    p.add_argument("--output_data_dir", type=str, default=os.environ.get("SM_OUTPUT_DATA_DIR", "/opt/ml/output/data"))
    p.add_argument("--checkpoint_dir", type=str, default="/opt/ml/checkpoints",
                   help="SageMaker CheckpointConfig LocalPath — synced to S3 during the run")
    return p.parse_args()


def truthy(v):
    return str(v).strip().strip('"').lower() in ("1", "true", "yes", "y")


def load_jsonl(dir_path):
    from datasets import load_dataset
    files = sorted(glob.glob(os.path.join(dir_path, "**", "*.jsonl"), recursive=True))
    if not files:
        raise FileNotFoundError(f"no *.jsonl under {dir_path}")
    ds = load_dataset("json", data_files=files, split="train")
    print(f"[data] {dir_path}: {len(ds)} rows from {files}")
    return ds


def drop_overlong_rows(ds, tokenizer, max_length, tag):
    """Remove rows whose templated length exceeds max_length.

    Truncation is worse than exclusion here: the tail of every ARC sample is the
    transform() code, so a truncated target trains the student to stop mid-function.
    """
    def fits(row):
        text = tokenizer.apply_chat_template(row["messages"], tokenize=False)
        return len(tokenizer(text, add_special_tokens=False)["input_ids"]) <= max_length

    before = len(ds)
    ds = ds.filter(fits)
    dropped = before - len(ds)
    print(f"[data] {tag}: dropped {dropped}/{before} rows over {max_length} tokens "
          f"({100.0 * dropped / max(before, 1):.2f}%)")
    return ds, dropped


def verify_mirror_manifest(local_dir, mm, manifest_path):
    """SHA-256 every file MODEL_MANIFEST.json names. Returns the count verified.

    Raises SystemExit unless at least one digest was found and every file it names is
    present and matches, because the two failures this has to separate look identical in
    a log: "the weights match what a human signed" and "there was nothing to compare".

    Two producers write this manifest and they do NOT agree on the key -- the data-prep
    `mirror_model` agent task writes `files_sha256`, the experiment-side mirror script
    writes `files`. Reading one name only, this check ran over ZERO files against a real
    in-account mirror and printed "mirror verified: 0 files".
    """
    import hashlib
    digests = mm.get("files_sha256") or mm.get("files") or {}
    if not digests:
        raise SystemExit(
            f"FATAL: {manifest_path} names no file digests -- looked for 'files_sha256' "
            f"and 'files', found keys {sorted(mm)}. A mirror integrity check with nothing "
            f"to check must not pass: either the manifest comes from a producer this "
            f"trainer does not understand, or it is empty, and both mean the weights are "
            f"unverified.")
    bad = []
    for fname, want in digests.items():
        fpath = os.path.join(local_dir, fname)
        if not os.path.exists(fpath):
            bad.append(f"{fname}: missing")
            continue
        h = hashlib.sha256()
        with open(fpath, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            bad.append(f"{fname}: sha256 mismatch")
    if bad:
        raise SystemExit(f"FATAL: mirror integrity check failed: {bad} -- "
                         "the mirrored model does not match its manifest.")
    return len(digests)


def intended_steps(n_rows, epochs, per_device_batch_size, gradient_accumulation):
    """Optimizer steps a FULL pass over n_rows would take. Two callers need this number.

    The preflight (`validate_job_config.py --sec-per-it`) multiplies steps by seconds per
    step, so both sides have to count the same unit. One optimizer step consumes
    per_device_batch_size * gradient_accumulation rows -- NOT per_device_batch_size.
    Multiplying the accumulation in on both sides is how a 9-hour estimate becomes a
    36-hour one and a launch gets refused for a limit it actually clears.

    It is also what makes a step-capped probe label itself honestly; see
    completed_fraction below.
    """
    eff = max(1, int(per_device_batch_size) * int(gradient_accumulation))
    total = n_rows * float(epochs) / eff
    return max(1, math.ceil(total))


def completed_fraction(global_step, trainer_max_steps, n_intended, step_capped):
    """How much of the run the operator asked for actually happened.

    `--max_steps 20` sets trainer.state.max_steps to 20, so the obvious
    global_step / max_steps is 20/20 = 1.0 and a 20-step probe writes
    "completed_fraction": 1.0 into metrics.json. Every downstream gate in this repo reads
    that field to tell a partial run from a finished one -- the whole point of the
    graceful-budget work -- so a probe that reports 1.0 is a lie in the one field built to
    prevent exactly this confusion. A capped run is measured against the full run instead.
    """
    if step_capped:
        return global_step / n_intended if n_intended else None
    return global_step / trainer_max_steps if trainer_max_steps else None


WARMUP_STEPS_EXCLUDED = 3


def step_timing(step_seconds, warmup_excluded=WARMUP_STEPS_EXCLUDED):
    """Seconds per optimizer step, with the steady state separated from the warmup.

    train_runtime / global_step folds in the first steps' CUDA autotune and allocator
    growth. Over 3,000 steps that is noise; over the 20 a preflight can afford it IS the
    measurement -- a first step of 90s against a steady 11s puts the mean at 15s, 36% high,
    and a 36%-high s/step is a runtime estimate that asks for hours the run will not use.
    So report both and name which is which, rather than picking one and hoping the reader
    guesses. `first_step` is kept because a first step wildly above the rest is itself the
    signal that the excluded window was too small.
    """
    def pct(vals, q):
        if not vals:
            return None
        s = sorted(vals)
        return s[min(len(s) - 1, int(q * len(s)))]

    steady = step_seconds[warmup_excluded:] or step_seconds
    return {
        "unit": "seconds per OPTIMIZER step (gradient-accumulation micro-batches included)",
        "n_steps_timed": len(step_seconds),
        "mean": (sum(step_seconds) / len(step_seconds)) if step_seconds else None,
        "p50": pct(step_seconds, 0.5),
        "p90": pct(step_seconds, 0.9),
        "warmup_steps_excluded": warmup_excluded,
        "p50_steady": pct(steady, 0.5),
        "p90_steady": pct(steady, 0.9),
        "first_step": step_seconds[0] if step_seconds else None,
    }


def vram_snapshot(torch_mod, device=0):
    """Peak GPU memory, or None with a reason. Never silently absent.

    The question a preflight has to answer is not "did it run" but "how close to the edge
    did it run" -- max_length 14336 either fits with headroom or OOMs on some longer row
    later in the epoch, and those two look identical for the first 20 steps. reserved is
    the number that OOMs (the allocator's pool), allocated is what the tensors needed;
    reporting only the second understates the risk.
    """
    try:
        if not torch_mod.cuda.is_available():
            return {"available": False, "reason": "no CUDA device visible to this process"}
        gib = float(1 << 30)
        total = torch_mod.cuda.get_device_properties(device).total_memory
        reserved = torch_mod.cuda.max_memory_reserved(device)
        allocated = torch_mod.cuda.max_memory_allocated(device)
        return {
            "available": True,
            "device_name": torch_mod.cuda.get_device_name(device),
            "max_allocated_gib": round(allocated / gib, 2),
            "max_reserved_gib": round(reserved / gib, 2),
            "device_total_gib": round(total / gib, 2),
            "reserved_fraction_of_device": round(reserved / total, 3) if total else None,
        }
    except Exception as exc:                      # noqa: BLE001 -- a probe must not lose the run
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def newest_checkpoint(ckpt_dir):
    if not os.path.isdir(ckpt_dir):
        return None
    cands = [d for d in glob.glob(os.path.join(ckpt_dir, "checkpoint-*")) if os.path.isdir(d)]
    if not cands:
        return None
    return max(cands, key=lambda d: int(re.search(r"checkpoint-(\d+)$", d).group(1)))


def filtered_kwargs(cls, kw):
    sig = inspect.signature(cls.__init__).parameters
    out = {}
    for k, v in kw.items():
        if k in sig:
            out[k] = v
        elif k == "max_length" and "max_seq_length" in sig:
            out["max_seq_length"] = v
        elif k == "eval_strategy" and "evaluation_strategy" in sig:
            out["evaluation_strategy"] = v
        else:
            print(f"[cfg] dropping unsupported kwarg: {k}")
    return out


def main():
    args = parse_args()
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, set_seed
    from transformers import TrainerCallback
    from peft import LoraConfig, PeftModel
    from trl import SFTConfig, SFTTrainer

    class TimeBudgetCallback(TrainerCallback):
        """Ask the Trainer to stop cleanly before SageMaker kills the job.

        should_training_stop lets train() return normally, so the run still saves an
        adapter, evaluates, merges, and uploads. Without this, hitting MaxRuntime
        means a hard kill and no model artifact at all.
        """

        def __init__(self, budget_sec):
            self.budget = budget_sec
            self.t0 = time.time()
            self.tripped = False

        def on_step_end(self, cfg, state, control, **kw):
            if self.budget and not self.tripped and (time.time() - self.t0) > self.budget:
                self.tripped = True
                control.should_training_stop = True
                control.should_save = True
                print(f"[budget] {self.budget:.0f}s training budget reached at step "
                      f"{state.global_step}/{state.max_steps} — stopping gracefully to save artifacts")
            return control

    class StepTimerCallback(TrainerCallback):
        """Wall-clock per OPTIMIZER step, so a 20-step probe can size a 3,000-step run.

        on_step_end fires once per optimizer step, after all gradient-accumulation
        micro-batches -- the same unit validate_job_config.py multiplies. Timing
        micro-batches instead would report a number 8x too small at
        gradient_accumulation=8 and pass every sanity check on the way to a runtime
        estimate an eighth of the truth.
        """

        def __init__(self):
            self.step_seconds = []
            self._last = None

        def on_train_begin(self, cfg, state, control, **kw):
            self._last = time.time()
            return control

        def on_step_end(self, cfg, state, control, **kw):
            now = time.time()
            if self._last is not None:
                self.step_seconds.append(now - self._last)
            self._last = now
            return control


    # Preflight: liger-kernel is load-bearing (it is what keeps the loss head from
    # OOM-ing at 4k context). Check it now — before the multi-minute base-model
    # download — so a missing dependency fails in seconds with an actionable message
    # instead of a stack trace ten minutes in.
    import importlib.util
    if importlib.util.find_spec("liger_kernel") is None:
        raise SystemExit(
            "FATAL: liger-kernel is not installed, but this trainer requires it "
            "(use_liger_kernel=True prevents a CUDA OOM from materializing "
            "[seq, 151936] logits). Add 'liger-kernel>=0.5.8' to "
            "pipeline/training/distill/requirements.txt and rebuild the sourcedir tarball."
        )

    set_seed(args.seed)
    t0 = time.time()
    train_ds, val_ds = load_jsonl(args.train_dir), load_jsonl(args.val_dir)

    # Supply-chain path: a model_id of s3://.../models-mirror/<repo>@<revision>/ is a
    # verified in-account mirror (pinned HF revision, safetensors, per-file SHA-256 in
    # MODEL_MANIFEST.json — written by the data-prep mirror_model task). Training from
    # the mirror rather than hub-at-job-start means the bytes a human signed off on in
    # plan.json are the bytes that train, and the job no longer needs HF egress.
    # A local mirror directory is pinned by its manifest, not by an HF revision, and
    # from_pretrained has no revision to resolve for a path. Recorded either way, so
    # metrics.json always says WHICH bytes trained.
    revision = args.model_revision
    if args.model_id.startswith("s3://"):
        import subprocess
        local_dir = "/tmp/model-mirror"
        print(f"[model] syncing mirror {args.model_id} -> {local_dir}")
        subprocess.run(["aws", "s3", "sync", args.model_id, local_dir,
                        "--only-show-errors"], check=True)
        manifest_path = os.path.join(local_dir, "MODEL_MANIFEST.json")
        if os.path.exists(manifest_path):
            mm = json.load(open(manifest_path))
            n_verified = verify_mirror_manifest(local_dir, mm, manifest_path)
            print(f"[model] mirror verified: {n_verified} files, "
                  f"{mm.get('hf_repo')}@{mm.get('revision', '')[:12]}")
        else:
            print("[model] WARNING: mirror has no MODEL_MANIFEST.json — loading "
                  "unverified (legacy mirror?)")
        args.model_id = local_dir
        revision = None

    tokenizer = AutoTokenizer.from_pretrained(args.model_id, revision=revision)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    dropped = {"train": 0, "val": 0}
    if truthy(args.drop_overlong):
        train_ds, dropped["train"] = drop_overlong_rows(train_ds, tokenizer, args.max_length, "train")
        val_ds, dropped["val"] = drop_overlong_rows(val_ds, tokenizer, args.max_length, "val")

    if args.max_val_rows and len(val_ds) > args.max_val_rows:
        print(f"[data] val: capping {len(val_ds)} -> {args.max_val_rows} rows for eval cost")
        val_ds = val_ds.select(range(args.max_val_rows))

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.model_id,
        revision=revision,
        quantization_config=bnb,
        torch_dtype=torch.bfloat16,
        attn_implementation="sdpa",
        device_map={"": 0} if torch.cuda.is_available() else None,
    )
    model.config.use_cache = False

    lora = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )

    # Checkpoints go to the SageMaker CheckpointConfig LocalPath so they reach S3
    # DURING the run — /tmp would be lost with the container.
    os.makedirs(args.checkpoint_dir, exist_ok=True)
    save_kw = (dict(save_strategy="steps", save_steps=args.save_steps, save_total_limit=2)
               if args.save_steps > 0 else dict(save_strategy="no"))

    cfg = SFTConfig(**filtered_kwargs(SFTConfig, dict(
        output_dir=args.checkpoint_dir,
        num_train_epochs=args.epochs,
        max_steps=args.max_steps if args.max_steps > 0 else -1,   # -1 = honour epochs

        per_device_train_batch_size=args.per_device_batch_size,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.gradient_accumulation,
        learning_rate=args.learning_rate,
        lr_scheduler_type="cosine",
        warmup_ratio=args.warmup_ratio,
        bf16=True,
        max_length=args.max_length,
        packing=False,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        use_liger_kernel=True,  # r2 remediation: fused linear CE avoids materializing [seq,151936] logits (OOM fix)
        optim="paged_adamw_8bit",
        logging_steps=1,
        eval_strategy="no",     # epoch-end eval never fires on a budget-stopped run; we evaluate explicitly below
        report_to=[],
        seed=args.seed,
        **save_kw,
    )))

    tr_kw = dict(model=model, args=cfg, train_dataset=train_ds, eval_dataset=val_ds, peft_config=lora)
    if "processing_class" in inspect.signature(SFTTrainer.__init__).parameters:
        tr_kw["processing_class"] = tokenizer
    else:
        tr_kw["tokenizer"] = tokenizer
    trainer = SFTTrainer(**tr_kw)

    budget_cb = None
    if args.max_train_seconds:
        budget_cb = TimeBudgetCallback(args.max_train_seconds)
        trainer.add_callback(budget_cb)

    # Always on. Timing costs one time.time() per step, and the reason the v2 relaunch
    # arithmetic had to be reconstructed from a tqdm line someone happened to screenshot
    # is that no run recorded it.
    timer_cb = StepTimerCallback()
    trainer.add_callback(timer_cb)

    resume = newest_checkpoint(args.checkpoint_dir)
    if resume:
        print(f"[resume] continuing from {resume}")
    train_result = trainer.train(resume_from_checkpoint=resume)
    # Read before eval and merge: max_memory_reserved is a high-water mark that never
    # falls, so asking after eval answers a different question than "does training fit".
    vram_training = vram_snapshot(torch)

    # Save the adapter BEFORE evaluating: eval can OOM on a long val row, and an
    # unsaved adapter after a full training run is the worst possible outcome.
    adapter_dir = os.path.join(args.model_dir, "adapter")
    trainer.save_model(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    print(f"[save] adapter -> {adapter_dir}")

    try:
        final_eval = trainer.evaluate()
    except Exception as exc:                      # noqa: BLE001 — eval must never cost us the model
        print(f"[eval] FAILED (adapter already saved): {type(exc).__name__}: {exc}")
        final_eval = {}

    history = list(trainer.state.log_history)
    train_losses = [h["loss"] for h in history if "loss" in h]
    eval_losses = [h["eval_loss"] for h in history if "eval_loss" in h]
    step_capped = args.max_steps > 0
    n_intended = intended_steps(len(train_ds), args.epochs,
                                args.per_device_batch_size, args.gradient_accumulation)
    completed_frac = completed_fraction(trainer.state.global_step, trainer.state.max_steps,
                                        n_intended, step_capped)
    metrics = {
        "final_train_loss": train_losses[-1] if train_losses else None,
        "mean_train_loss": (sum(train_losses) / len(train_losses)) if train_losses else None,
        "final_eval_loss": final_eval.get("eval_loss"),
        "eval_loss_by_epoch": eval_losses,
        "train_runtime_seconds": train_result.metrics.get("train_runtime"),
        "epochs": args.epochs,
        "epochs_completed": trainer.state.epoch,
        "global_step": trainer.state.global_step,
        "max_steps": trainer.state.max_steps,
        "completed_fraction": completed_frac,
        "step_capped": step_capped,
        "max_steps_requested": args.max_steps,
        "intended_steps_full_run": n_intended,
        "sec_per_step": step_timing(timer_cb.step_seconds),
        "peak_vram": {"training": vram_training, "including_eval": vram_snapshot(torch)},
        "budget_stopped": bool(budget_cb and budget_cb.tripped),
        "resumed_from": resume,
        "rows_dropped_overlong": dropped,
        "learning_rate": args.learning_rate,
        "max_length": args.max_length,
        "effective_batch_size": args.per_device_batch_size * args.gradient_accumulation,
        "lora": {"r": args.lora_r, "alpha": args.lora_alpha, "dropout": args.lora_dropout},
        "model_id": args.model_id,
        "model_revision": revision,
        "warmup_ratio": args.warmup_ratio,
        "train_rows": len(train_ds),
        "val_rows": len(val_ds),
        "log_history": history,
        "wall_seconds": time.time() - t0,
    }
    print(f"METRIC train_loss={metrics['final_train_loss']}")
    print(f"METRIC eval_loss={metrics['final_eval_loss']}")
    print(f"METRIC completed_fraction={completed_frac}")
    _t = metrics["sec_per_step"]
    print(f"METRIC sec_per_step_p50_steady={_t['p50_steady']} (first_step={_t['first_step']}, "
          f"n={_t['n_steps_timed']})")
    print(f"METRIC peak_vram_reserved_gib={(vram_training or {}).get('max_reserved_gib')}")
    if step_capped:
        print(f"[probe] STEP-CAPPED RUN: {trainer.state.global_step} of {n_intended} intended "
              f"steps ({completed_frac:.2%}). This is a MEASUREMENT, not a trained adapter — "
              f"do not promote it, and do not read TRAINING_COMPLETE below as a finished run.")

    if truthy(args.merge_adapters):
        print("[merge] merging LoRA adapters into base model (bf16, CPU)")
        del trainer, model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        base = AutoModelForCausalLM.from_pretrained(args.model_id, revision=revision, torch_dtype=torch.bfloat16, device_map="cpu")
        merged = PeftModel.from_pretrained(base, adapter_dir).merge_and_unload()
        merged_dir = os.path.join(args.model_dir, "merged")
        merged.save_pretrained(merged_dir, safe_serialization=True)
        tokenizer.save_pretrained(merged_dir)
        metrics["merged_model_saved"] = True

    os.makedirs(args.output_data_dir, exist_ok=True)
    for d in (args.model_dir, args.output_data_dir):
        with open(os.path.join(d, "metrics.json"), "w") as f:
            json.dump(metrics, f, indent=2, default=str)
    print("[done] metrics:", json.dumps({k: v for k, v in metrics.items() if k != "log_history"}, default=str))
    print("TRAINING_COMPLETE")


if __name__ == "__main__":
    main()
