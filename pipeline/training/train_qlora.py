#!/usr/bin/env python3
"""QLoRA SFT entry point — runs INSIDE a SageMaker training job.

Distills teacher (DeepSeek-R1 via Bedrock) knowledge into the student
(Qwen3-1.7B) by supervised fine-tuning on the curated distillation set
produced by the data-prep agent (sequence-level KD; for ARC-AGI-2 the
targets keep the teacher's <think> reasoning chains — reasoning distillation).

SageMaker contract (set by the finetune agent when it calls CreateTrainingJob):
  - Channel "train":      /opt/ml/input/data/train/curated-train.jsonl
  - Channel "validation": /opt/ml/input/data/validation/curated-val.jsonl
  - Hyperparameters arrive as CLI args (SageMaker script mode)
  - Model artifacts written to /opt/ml/model  ->  S3 OutputDataConfig
  - Metrics printed as "METRIC name=value" lines for CloudWatch regex capture

Data format (one JSON object per line):
  {"messages": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]}

The base model is downloaded from the official Hugging Face hub (Qwen org)
inside the job — the training container needs outbound HTTPS (or a
HF_HUB-mirrored S3 path via --model_id pointing at a local dir for VPC mode).
"""
import argparse
import json
import os


def parse_args():
    p = argparse.ArgumentParser()
    # model / data
    p.add_argument("--model_id", default="Qwen/Qwen3-1.7B",
                   help="HF hub id (official Qwen org) or local dir for VPC mode")
    p.add_argument("--train_dir", default=os.environ.get("SM_CHANNEL_TRAIN", "/opt/ml/input/data/train"))
    p.add_argument("--val_dir", default=os.environ.get("SM_CHANNEL_VALIDATION", "/opt/ml/input/data/validation"))
    p.add_argument("--output_dir", default=os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
    # QLoRA
    p.add_argument("--lora_r", type=int, default=16)
    p.add_argument("--lora_alpha", type=int, default=32)
    p.add_argument("--lora_dropout", type=float, default=0.05)
    # training
    p.add_argument("--epochs", type=float, default=3.0)
    p.add_argument("--learning_rate", type=float, default=2e-4)
    p.add_argument("--per_device_batch_size", type=int, default=2)
    p.add_argument("--gradient_accumulation", type=int, default=8)
    p.add_argument("--max_length", type=int, default=4096,
                   help="TRL 1.x: SFTConfig.max_length (NOT max_seq_length)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--merge_adapters", action="store_true", default=True,
                   help="merge LoRA into base weights for TGI/vLLM serving")
    return p.parse_args()


def load_jsonl(path_dir, name_hint):
    """Load the first *.jsonl in a channel dir."""
    files = [f for f in os.listdir(path_dir) if f.endswith(".jsonl")]
    if not files:
        raise FileNotFoundError(f"no .jsonl in {path_dir} ({name_hint})")
    rows = []
    with open(os.path.join(path_dir, files[0])) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    args = parse_args()

    # Heavy imports deferred so --help works on any machine (repo CI runs it).
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, set_seed
    from trl import SFTConfig, SFTTrainer

    set_seed(args.seed)

    train_rows = load_jsonl(args.train_dir, "train")
    val_rows = load_jsonl(args.val_dir, "validation")
    print(f"METRIC train_samples={len(train_rows)}")
    print(f"METRIC val_samples={len(val_rows)}")

    tokenizer = AutoTokenizer.from_pretrained(args.model_id)

    # 4-bit NF4 quantized base — QLoRA
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.model_id,
        quantization_config=bnb,
        dtype=torch.bfloat16,
        device_map="auto",
    )

    peft_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    )

    sft_config = SFTConfig(
        output_dir=os.path.join(args.output_dir, "checkpoints"),
        num_train_epochs=args.epochs,
        learning_rate=args.learning_rate,
        per_device_train_batch_size=args.per_device_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation,
        max_length=args.max_length,          # TRL 1.x name
        bf16=True,
        logging_steps=10,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=1,
        gradient_checkpointing=True,
        report_to=[],                         # metrics go via stdout regex
        seed=args.seed,
    )

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=Dataset.from_list(train_rows),
        eval_dataset=Dataset.from_list(val_rows),
        peft_config=peft_config,
        args=sft_config,
    )

    result = trainer.train()
    print(f"METRIC train_loss={result.training_loss:.4f}")
    eval_metrics = trainer.evaluate()
    print(f"METRIC eval_loss={eval_metrics.get('eval_loss', float('nan')):.4f}")

    # Persist adapters (always) …
    adapter_dir = os.path.join(args.output_dir, "adapter")
    trainer.model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)

    # … and a merged full model for direct TGI/vLLM serving.
    if args.merge_adapters:
        merged = trainer.model.merge_and_unload()
        merged_dir = os.path.join(args.output_dir, "merged")
        merged.save_pretrained(merged_dir, safe_serialization=True)
        tokenizer.save_pretrained(merged_dir)
        print(f"METRIC merged=1")

    print("TRAINING_COMPLETE")


if __name__ == "__main__":
    main()
