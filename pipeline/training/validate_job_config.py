#!/usr/bin/env python3
"""Preflight a SageMaker CreateTrainingJob payload before spending GPU hours on it.

Exists because run v2-code-distill-0001-e1g6 burned 43 GPU-minutes and produced zero
artifacts: its only save point was at step 1265, but MaxRuntimeInSeconds only reached
step ~496. That was catchable with multiplication. This script does the multiplication.

Usage:
    python validate_job_config.py job.json [--sec-per-it 27] [--rows 20225]

Exits non-zero on any FAIL, so a launcher (agent or human) can gate on it.
"""
import argparse
import json
import sys


def check(payload, sec_per_it, rows):
    """Return (failures, warnings, facts) for a CreateTrainingJob payload."""
    fails, warns, facts = [], [], []

    hp = {k: str(v).strip().strip('"') for k, v in payload.get("HyperParameters", {}).items()}
    max_runtime = payload.get("StoppingCondition", {}).get("MaxRuntimeInSeconds")
    ckpt = payload.get("CheckpointConfig", {}).get("S3Uri")

    def num(key, default=None):
        try:
            return float(hp[key])
        except (KeyError, ValueError):
            return default

    budget = num("max_train_seconds", 0) or 0
    save_steps = num("save_steps", 0) or 0
    bs = num("per_device_batch_size", 1) or 1
    ga = num("gradient_accumulation", 1) or 1
    epochs = num("epochs", 1) or 1

    # 1. A graceful budget must leave room for save + eval + merge + upload.
    if not max_runtime:
        fails.append("StoppingCondition.MaxRuntimeInSeconds is not set")
    elif budget:
        if budget >= max_runtime:
            fails.append(
                f"max_train_seconds ({budget:.0f}s) >= MaxRuntimeInSeconds ({max_runtime}s): "
                "SageMaker will hard-kill the job before it can save. Leave headroom."
            )
        else:
            head = max_runtime - budget
            facts.append(f"headroom after training budget: {head / 60:.0f} min")
            if head < 1800:
                warns.append(
                    f"only {head / 60:.0f} min of headroom for save+eval+merge+upload; "
                    "30+ min is safer for a merged multi-GB model"
                )
    else:
        warns.append(
            "max_train_seconds is unset, so the job relies on finishing before "
            "MaxRuntimeInSeconds — a hard kill would lose everything not checkpointed"
        )

    # 2. Checkpoints must reach S3 during the run, or a kill is unrecoverable.
    if save_steps <= 0:
        fails.append("save_steps <= 0: no periodic checkpoints, so a stop or kill loses all progress")
    if not ckpt:
        fails.append(
            "CheckpointConfig.S3Uri is not set: local checkpoints die with the container "
            "and there is nothing to resume from"
        )

    # 3. The core e1g6 defect: is a full epoch even reachable in the time allowed?
    if rows and sec_per_it and max_runtime:
        eff_batch = bs * ga
        steps = int(rows * epochs / eff_batch)
        need = steps * sec_per_it
        limit = budget or max_runtime
        reach = int(limit / sec_per_it)
        pct = 100.0 * reach / steps if steps else 0.0
        facts.append(
            f"~{steps} steps at {sec_per_it:.1f}s/it needs {need / 3600:.1f}h; "
            f"limit {limit / 3600:.1f}h reaches step ~{reach} ({pct:.0f}% of the run)"
        )
        if pct < 100:
            # Not fatal now that checkpoints + graceful stop exist, but say it plainly.
            msg = (f"the time limit reaches only {pct:.0f}% of the configured run")
            if save_steps > 0 and ckpt and budget:
                warns.append(msg + " — it will stop gracefully and save a partial adapter")
            else:
                fails.append(msg + " — and it cannot save partial progress")
        if save_steps > 0:
            facts.append(f"~{int(steps / save_steps)} checkpoints planned; worst-case loss {save_steps} steps")

    return fails, warns, facts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job_json")
    ap.add_argument("--sec-per-it", type=float, default=27.0,
                    help="measured seconds per step (g5/g6.2xlarge Qwen3-1.7B QLoRA 4k ctx ~27)")
    ap.add_argument("--rows", type=int, default=0, help="training rows, for the step-count estimate")
    args = ap.parse_args()

    with open(args.job_json) as fh:
        payload = json.load(fh)

    fails, warns, facts = check(payload, args.sec_per_it, args.rows)

    print(f"job: {payload.get('TrainingJobName', '(unnamed)')}")
    for f in facts:
        print(f"  fact  {f}")
    for w in warns:
        print(f"  WARN  {w}")
    for f in fails:
        print(f"  FAIL  {f}")
    print("PASS — safe to launch" if not fails else f"{len(fails)} FAIL — do not launch")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
