#!/usr/bin/env python3
"""SageMaker entry point: resolve a model from an input channel, then generate.

`generate_student.py` takes `--model-dir`, a directory. SageMaker hands over an
input channel, which may be a directory of weights OR a `model.tar.gz` it does not
unpack (only `sagemaker_submit_directory` is unpacked automatically). This wrapper
is the adapter between those two facts, and nothing more: it locates or extracts a
model, optionally applies a LoRA adapter checkpoint, and calls the real generator
with argv it prints first.

It exists as a separate file rather than as a branch inside `generate_student.py`
because that script has to stay runnable on a laptop with no SageMaker paths, and
because the resolution below is exactly the kind of "find the thing in the
directory" logic that must fail loudly rather than pick a plausible wrong answer.

WHY IT ASSERTS SO MUCH. A generation job that silently evaluates the WRONG weights
produces a complete, plausible report -- the failure has no symptom. The base model
and the fine-tuned merge have identical architecture, identical tokenizer and
identical file names; the only thing distinguishing them is which directory got
loaded. So every resolution step here either proves what it found or exits non-zero.

Channels:
    /opt/ml/input/data/model     weights dir, or a *.tar.gz containing one
    /opt/ml/input/data/adapter   optional: a LoRA checkpoint dir (or tar.gz) to
                                 apply on top of the model channel
    /opt/ml/input/data/val       *.jsonl to generate from
Output:
    /opt/ml/output/data/         generations.jsonl + .done + argv.json

Hyperparameters map 1:1 onto `generate_student.py` flags, with underscores (which
is what SageMaker passes) translated to hyphens.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import time

MODEL_CH = "/opt/ml/input/data/model"
ADAPTER_CH = "/opt/ml/input/data/adapter"
VAL_CH = "/opt/ml/input/data/val"
# Module constants rather than literals inside main() so the whole wrapper can be
# exercised off a fake channel tree on a laptop. The alternative -- discovering a
# resolution bug after an instance has spun up and pulled 6.47 GiB -- costs real
# money to learn something a $0 test answers.
HP_PATH = "/opt/ml/input/config/hyperparameters.json"
OUT_DIR = os.environ.get("SM_OUTPUT_DATA_DIR", "/opt/ml/output/data")
HERE = os.path.dirname(os.path.abspath(__file__))

# Flags `generate_student.py` accepts. Anything else in the hyperparameters is a
# SageMaker-internal key (sagemaker_program, sagemaker_region, ...) or a typo, and
# a typo must not be silently dropped: a run that ignored `--max_new_tokens`
# because it was spelled `--max-new-token` would quietly use the 1536 default and
# every format failure would be attributed to the model.
PASSTHROUGH = {
    "val", "out", "limit", "max-new-tokens", "temperature", "batch-size",
    "input-window", "n-samples", "thinking", "device", "max-seconds",
}
SM_INTERNAL_PREFIX = "sagemaker_"


def unpack(channel: str, dest: str) -> str:
    """Return a directory holding the channel's payload, extracting a tarball if needed.

    Extraction is filtered (`filter="data"`) because a tarball is untrusted input in
    the general case and the 3.12 default is a deprecation warning, not safety.
    """
    if not os.path.isdir(channel):
        raise SystemExit(f"channel {channel} is not a directory")
        # (no fallback: a missing model channel must not resolve to "use the HF hub")
    tars = sorted(glob.glob(os.path.join(channel, "**", "*.tar.gz"), recursive=True))
    if not tars:
        return channel
    if len(tars) > 1:
        raise SystemExit(f"{channel} holds {len(tars)} tarballs, refusing to guess: "
                         f"{[os.path.basename(t) for t in tars]}")
    os.makedirs(dest, exist_ok=True)
    started = time.time()
    with tarfile.open(tars[0]) as tf:
        tf.extractall(dest, filter="data")
    print(f"[wrap] extracted {os.path.basename(tars[0])} "
          f"({os.path.getsize(tars[0]) / 2**30:.2f} GiB) in "
          f"{time.time() - started:.0f}s", flush=True)
    return dest


def find_weights(root: str) -> str:
    """Find the one directory under `root` that is a loadable causal-LM.

    `config.json` alone is not enough: a LoRA checkpoint directory also carries an
    `adapter_config.json` and no weights, and the merged export sits in a `merged/`
    subdirectory beside an `adapter/` sibling that ALSO has a config. Requiring
    real weight shards is what separates them.

    Ambiguity is an error rather than a preference order. "Prefer the one called
    merged" would work today and pick the base model the day a directory is renamed,
    and the resulting report would look completely normal.
    """
    cands = []
    for cfg in glob.glob(os.path.join(root, "**", "config.json"), recursive=True):
        d = os.path.dirname(cfg)
        shards = (glob.glob(os.path.join(d, "*.safetensors"))
                  + glob.glob(os.path.join(d, "*.bin")))
        if shards:
            cands.append((d, sum(os.path.getsize(s) for s in shards)))
    if not cands:
        listing = sorted(os.listdir(root))[:20]
        raise SystemExit(f"no loadable weights under {root}; top level: {listing}")
    if len(cands) > 1:
        raise SystemExit("ambiguous model channel -- "
                         + ", ".join(f"{d} ({n / 2**30:.2f} GiB)" for d, n in cands))
    d, nbytes = cands[0]
    print(f"[wrap] model = {d} ({nbytes / 2**30:.2f} GiB of weights)", flush=True)
    return d


def find_adapter(root: str) -> str:
    """Find the one LoRA checkpoint directory: `adapter_config.json` + a safetensors."""
    cands = [os.path.dirname(c) for c in
             glob.glob(os.path.join(root, "**", "adapter_config.json"), recursive=True)
             if glob.glob(os.path.join(os.path.dirname(c), "adapter_model.safetensors"))]
    if len(cands) != 1:
        raise SystemExit(f"expected exactly 1 adapter under {root}, found "
                         f"{len(cands)}: {cands[:5]}")
    print(f"[wrap] adapter = {cands[0]}", flush=True)
    return cands[0]


def merge_adapter(model_dir: str, adapter_dir: str, dest: str) -> str:
    """Merge a LoRA checkpoint into the base weights and return the merged dir.

    Done here rather than by passing the adapter to `generate_student.py` so that
    the generator keeps exactly one way to load a model. That matters for the
    comparison the whole run exists for: a base pass and a fine-tuned pass must go
    through the same `from_pretrained` call, or a difference in the loading path
    becomes indistinguishable from a difference in the weights.

    The merge is verified to have CHANGED something. `merge_and_unload()` on a
    mis-targeted adapter -- wrong module names, wrong base -- can return the base
    weights untouched and raise nothing, and the resulting "fine-tuned" run would
    be a second base run reported as a lift measurement of exactly 0.
    """
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    base = AutoModelForCausalLM.from_pretrained(model_dir, dtype=torch.bfloat16)
    before = _weight_digest(base)
    merged = PeftModel.from_pretrained(base, adapter_dir).merge_and_unload()
    after = _weight_digest(merged)
    if before == after:
        raise SystemExit(
            f"adapter {adapter_dir} changed no weights (digest {before[:16]} both "
            f"before and after merge). A no-op merge would report the base model's "
            f"score as the fine-tuned model's.")
    print(f"[wrap] merge changed weights: {before[:16]} -> {after[:16]}", flush=True)
    merged.save_pretrained(dest, safe_serialization=True)
    AutoTokenizer.from_pretrained(model_dir).save_pretrained(dest)
    return dest


def _weight_digest(model, n_tensors: int = 24) -> str:
    """Digest a deterministic sample of parameters -- enough to detect a no-op merge.

    Sampled rather than complete because hashing 8 GiB twice costs minutes of GPU
    time to answer a yes/no question. Sorted by name so the sample is the same on
    both sides of the merge; a random sample would make the two digests differ even
    when the weights did not, which is the failure this check is meant to exclude.
    """
    h = hashlib.sha256()
    for name, p in sorted(model.named_parameters())[:n_tensors]:
        h.update(name.encode())
        h.update(p.detach().float().flatten()[:4096].numpy().tobytes())
    return h.hexdigest()


def main() -> int:
    hp = {}
    if os.path.exists(HP_PATH):
        hp = json.load(open(HP_PATH))

    unknown = [k for k in hp
               if not k.startswith(SM_INTERNAL_PREFIX)
               and k.replace("_", "-") not in PASSTHROUGH]
    if unknown:
        raise SystemExit(f"unknown hyperparameters {unknown}; a misspelled flag "
                         f"would silently fall back to a default and the resulting "
                         f"failures would be blamed on the model. Known: "
                         f"{sorted(PASSTHROUGH)}")

    os.makedirs(OUT_DIR, exist_ok=True)
    model_dir = find_weights(unpack(MODEL_CH, "/tmp/model"))

    if os.path.isdir(ADAPTER_CH) and os.listdir(ADAPTER_CH):
        adapter_dir = find_adapter(unpack(ADAPTER_CH, "/tmp/adapter"))
        model_dir = merge_adapter(model_dir, adapter_dir, "/tmp/merged")

    vals = sorted(glob.glob(os.path.join(VAL_CH, "**", "*.jsonl"), recursive=True))
    if len(vals) != 1:
        raise SystemExit(f"expected exactly 1 *.jsonl in {VAL_CH}, found {len(vals)}")

    argv = [sys.executable, os.path.join(HERE, "generate_student.py"),
            "--model-dir", model_dir,
            "--val", vals[0],
            "--out", os.path.join(OUT_DIR, "generations.jsonl")]
    for k, v in sorted(hp.items()):
        flag = k.replace("_", "-")
        if flag in PASSTHROUGH and flag not in ("val", "out"):
            argv += [f"--{flag}", str(v).strip('"')]

    # Recorded before the run, not after: a job killed by MaxRuntime still leaves
    # behind what it was asked to do, and "which flags did that job actually use"
    # is otherwise only answerable from the console log, which expires.
    json.dump({"argv": argv, "hyperparameters": hp, "model_dir": model_dir,
               "val": vals[0]}, open(os.path.join(OUT_DIR, "argv.json"), "w"), indent=2)
    print("[wrap] " + " ".join(argv), flush=True)
    return subprocess.call(argv)


if __name__ == "__main__":
    sys.exit(main())
