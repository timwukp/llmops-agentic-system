#!/usr/bin/env python
"""QLoRA SFT distillation trainer for SageMaker script mode (TRL + PEFT, 4-bit NF4).

Trains a small student (default Qwen/Qwen3-1.7B) on teacher reasoning traces in
TRL "messages" JSONL format (reasoning kept as <think>...</think>).

Contract (SageMaker hyperparameters -> argparse):
  --model_id --epochs --learning_rate --max_length --per_device_batch_size
  --gradient_accumulation --merge_adapters [--lora_r --lora_alpha --lora_dropout --seed]

Channels: SM_CHANNEL_TRAIN, SM_CHANNEL_VAL (dirs containing *.jsonl).
Outputs:  SM_MODEL_DIR/adapter, SM_MODEL_DIR/merged (if merge_adapters),
          metrics.json in SM_MODEL_DIR and SM_OUTPUT_DATA_DIR.

CANONICAL distillation trainer. Provenance: authored by the FINETUNE agent on
run-20260811T165529Z-ce628817 (its docstring then read "canonical
code/train_qlora.py unreadable by harness role" -- the read was IAM-denied, so
each run re-authored its own script until one came out with an UnboundLocalError
and killed r6a). These are the bytes that trained Qwen3-8B in 929s to
eval_loss 0.089; deploy/03_storage.py mirrors them to s3://<bucket>/code/distill/
and the finetune prompt now reads them instead of improvising. Follows the
llm-fine-tuning skill QLoRA methodology.
"""
import argparse, gc, glob, inspect, json, os, time


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model_id", type=str, default="Qwen/Qwen3-1.7B")
    p.add_argument("--model_revision", type=str, default="main")
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
    p.add_argument("--train_dir", type=str, default=os.environ.get("SM_CHANNEL_TRAIN", "/opt/ml/input/data/train"))
    p.add_argument("--val_dir", type=str, default=os.environ.get("SM_CHANNEL_VAL", "/opt/ml/input/data/val"))
    p.add_argument("--model_dir", type=str, default=os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
    p.add_argument("--output_data_dir", type=str, default=os.environ.get("SM_OUTPUT_DATA_DIR", "/opt/ml/output/data"))
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
    from peft import LoraConfig, PeftModel
    from trl import SFTConfig, SFTTrainer

    set_seed(args.seed)
    t0 = time.time()
    train_ds, val_ds = load_jsonl(args.train_dir), load_jsonl(args.val_dir)

    tokenizer = AutoTokenizer.from_pretrained(args.model_id, revision=args.model_revision)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.model_id,
        revision=args.model_revision,
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

    cfg = SFTConfig(**filtered_kwargs(SFTConfig, dict(
        output_dir="/tmp/sft-checkpoints",
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
        eval_strategy="epoch",
        save_strategy="no",
        report_to=[],
        seed=args.seed,
    )))

    tr_kw = dict(model=model, args=cfg, train_dataset=train_ds, eval_dataset=val_ds, peft_config=lora)
    if "processing_class" in inspect.signature(SFTTrainer.__init__).parameters:
        tr_kw["processing_class"] = tokenizer
    else:
        tr_kw["tokenizer"] = tokenizer
    trainer = SFTTrainer(**tr_kw)

    train_result = trainer.train()
    final_eval = trainer.evaluate()

    adapter_dir = os.path.join(args.model_dir, "adapter")
    trainer.save_model(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)

    history = list(trainer.state.log_history)
    train_losses = [h["loss"] for h in history if "loss" in h]
    eval_losses = [h["eval_loss"] for h in history if "eval_loss" in h]
    metrics = {
        "final_train_loss": train_losses[-1] if train_losses else None,
        "mean_train_loss": (sum(train_losses) / len(train_losses)) if train_losses else None,
        "final_eval_loss": final_eval.get("eval_loss"),
        "eval_loss_by_epoch": eval_losses,
        "train_runtime_seconds": train_result.metrics.get("train_runtime"),
        "epochs": args.epochs,
        "learning_rate": args.learning_rate,
        "max_length": args.max_length,
        "effective_batch_size": args.per_device_batch_size * args.gradient_accumulation,
        "lora": {"r": args.lora_r, "alpha": args.lora_alpha, "dropout": args.lora_dropout},
        "model_id": args.model_id,
        "model_revision": args.model_revision,
        "train_rows": len(train_ds),
        "val_rows": len(val_ds),
        "log_history": history,
        "wall_seconds": time.time() - t0,
    }

    if truthy(args.merge_adapters):
        print("[merge] merging LoRA adapters into base model (bf16, CPU)")
        del trainer, model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        base = AutoModelForCausalLM.from_pretrained(args.model_id, revision=args.model_revision, torch_dtype=torch.bfloat16, device_map="cpu")
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


if __name__ == "__main__":
    main()
