#!/usr/bin/env python
"""QLoRA SFT distillation trainer for SageMaker script mode (TRL + PEFT, 4-bit NF4).

Trains a small student (default Qwen/Qwen3-1.7B) on teacher reasoning traces in
TRL "messages" JSONL format (reasoning kept as <think>...</think>).

Contract (SageMaker hyperparameters -> argparse):
  --model_id --epochs --learning_rate --max_length --per_device_batch_size
  --gradient_accumulation --merge_adapters [--lora_r --lora_alpha --lora_dropout --seed]
  --save_steps --max_train_seconds --drop_overlong

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
"""
import argparse, gc, glob, inspect, json, os, re, time


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model_id", type=str, default="Qwen/Qwen3-1.7B")
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
    # deliverability controls
    p.add_argument("--save_steps", type=int, default=100,
                   help="checkpoint cadence; 0 disables periodic saves (NOT recommended)")
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
            "pipeline/training/requirements.txt and rebuild the sourcedir tarball."
        )

    set_seed(args.seed)
    t0 = time.time()
    train_ds, val_ds = load_jsonl(args.train_dir), load_jsonl(args.val_dir)

    tokenizer = AutoTokenizer.from_pretrained(args.model_id)
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
        per_device_train_batch_size=args.per_device_batch_size,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.gradient_accumulation,
        learning_rate=args.learning_rate,
        lr_scheduler_type="cosine",
        warmup_ratio=0.1,
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

    resume = newest_checkpoint(args.checkpoint_dir)
    if resume:
        print(f"[resume] continuing from {resume}")
    train_result = trainer.train(resume_from_checkpoint=resume)

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
    completed_frac = (trainer.state.global_step / trainer.state.max_steps) if trainer.state.max_steps else None
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
        "budget_stopped": bool(budget_cb and budget_cb.tripped),
        "resumed_from": resume,
        "rows_dropped_overlong": dropped,
        "learning_rate": args.learning_rate,
        "max_length": args.max_length,
        "effective_batch_size": args.per_device_batch_size * args.gradient_accumulation,
        "lora": {"r": args.lora_r, "alpha": args.lora_alpha, "dropout": args.lora_dropout},
        "model_id": args.model_id,
        "train_rows": len(train_ds),
        "val_rows": len(val_ds),
        "log_history": history,
        "wall_seconds": time.time() - t0,
    }
    print(f"METRIC train_loss={metrics['final_train_loss']}")
    print(f"METRIC eval_loss={metrics['final_eval_loss']}")
    print(f"METRIC completed_fraction={completed_frac}")

    if truthy(args.merge_adapters):
        print("[merge] merging LoRA adapters into base model (bf16, CPU)")
        del trainer, model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        base = AutoModelForCausalLM.from_pretrained(args.model_id, torch_dtype=torch.bfloat16, device_map="cpu")
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
