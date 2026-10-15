"""Glue Python-shell job: the seeded source generator → lakehouse/landing/<dataset>/ (Phase 1 PR-09).

The pipeline's `lakehouse_ingest` mode starts here. It runs pipeline/lakehouse/generate.py (shipped with
--extra-py-files) and writes exactly the files `generate.write` produces: bronze_<table>.jsonl,
eval_prompts.jsonl, expected_defects.json, and manifest.json last. A reader that finds manifest.json
therefore finds every file it names. The same seed and scale give the same `dataset_sha256` on
Python 3.9 (this job) and 3.12 (the deploy smoke's local run); the smoke compares the two.

It runs as llmops-lakehouse-generator, which may write landing and nothing else. The transforms' role
cannot write landing, so neither job can rewrite the other's input. A landing prefix that already
holds objects is refused rather than overwritten: a dataset id names one generation, and two runs
writing one prefix would make every report under it describe a mixture.

Arguments: --data_bucket (job default) --dataset --seed --scale
"""
import json
import pathlib
import re
import sys
import tempfile

import boto3
from awsglue.utils import getResolvedOptions

import generate

args = getResolvedOptions(sys.argv, ["data_bucket", "dataset", "seed", "scale"])
if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", args["dataset"]):
    raise SystemExit(f"--dataset must be a lowercase id, got {args['dataset']!r}")
if args["scale"] not in generate.SCALES:
    raise SystemExit(f"--scale must be one of {sorted(generate.SCALES)}, got {args['scale']!r}")
bucket, prefix = args["data_bucket"], f"lakehouse/landing/{args['dataset']}/"
s3 = boto3.client("s3")
if s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("KeyCount"):
    raise SystemExit(f"s3://{bucket}/{prefix} already holds objects: refusing to overwrite a dataset")

with tempfile.TemporaryDirectory() as tmp:
    out = pathlib.Path(tmp)
    manifest = generate.write(generate.generate(int(args["seed"]), args["scale"]), out)
    names = sorted(p.name for p in out.iterdir() if p.name != "manifest.json") + ["manifest.json"]
    for name in names:
        s3.put_object(Bucket=bucket, Key=prefix + name, Body=(out / name).read_bytes(),
                      ContentType="application/json" if name.endswith(".json") else "application/x-ndjson")
print(json.dumps({"landing": f"s3://{bucket}/{prefix}", "files": names,
                  "dataset_sha256": manifest["dataset_sha256"], "seed": manifest.get("seed"),
                  "scale": manifest.get("scale")}))
