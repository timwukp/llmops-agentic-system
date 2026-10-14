"""Glue 5 job: Bronze → Silver (Phase 1 PR-07).

Thin by design: it reads bronze.conversation_events, hands the rows to
pipeline/lakehouse/transform.py (shipped with --extra-py-files), and writes what comes back.
Every decision about the data is made there, where it is tested against the generator's plants.

Names are detected by the Bedrock guardrail (spike S4: regex + Guardrails is the layering that
reached recall 0.999 on names); structured identifiers by transform.py's checksum layer. The job
then evaluates SILVER_DQDL with Glue Data Quality and writes the rule report to S3, where the
console's Data tab and the smoke check read it.

Arguments: --account_id --raw_bucket --curated_bucket --eval_prompts_uri --report_uri
           --guardrail_id --guardrail_version --dq_run_id --org_entities_uri
"""
import datetime
import json
import sys

import boto3
from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.utils import getResolvedOptions
from awsgluedq.transforms import EvaluateDataQuality
from pyspark.sql import SparkSession

import transform

args = getResolvedOptions(sys.argv, ["account_id", "raw_bucket", "curated_bucket", "eval_prompts_uri",
                                     "report_uri", "guardrail_id", "guardrail_version", "dq_run_id",
                                     "org_entities_uri"])
spark = SparkSession.builder.config(
    "spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
for name, bucket in (("raw", args["raw_bucket"]), ("cur", args["curated_bucket"])):
    spark = (spark.config(f"spark.sql.catalog.{name}", "org.apache.iceberg.spark.SparkCatalog")
             .config(f"spark.sql.catalog.{name}.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
             .config(f"spark.sql.catalog.{name}.glue.id", f"{args['account_id']}:s3tablescatalog/{bucket}"))
spark = spark.getOrCreate()
glue_ctx = GlueContext(spark.sparkContext)
rt = boto3.client("bedrock-runtime")


def names_in(text):
    """NAME entities the guardrail finds; everything else is transform.py's checksum layer."""
    if not text:
        return []
    resp = rt.apply_guardrail(guardrailIdentifier=args["guardrail_id"],
                              guardrailVersion=args["guardrail_version"], source="INPUT",
                              content=[{"text": {"text": text}}])
    found = []
    for a in resp.get("assessments", []):
        for e in (a.get("sensitiveInformationPolicy") or {}).get("piiEntities", []):
            if e.get("type") == "NAME" and e.get("match"):
                found.append(e["match"])
    return found


events = [r.asDict() for r in spark.table("raw.bronze.conversation_events").collect()]
for e in events:
    e["ingest_ts"] = e["ingest_ts"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
prompts = [json.loads(line)["prompt"] for line in
           spark.sparkContext.textFile(args["eval_prompts_uri"]).collect() if line.strip()]
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
ref = json.loads("".join(spark.sparkContext.textFile(args["org_entities_uri"]).collect()))
rows, report = transform.bronze_to_silver(events, prompts, name_detector=names_in,
                                          keep_entities=transform.org_entities(ref),
                                          dq_run_id=args["dq_run_id"], updated_ts=now)

target = spark.table("cur.silver.instruction_pairs")
df = spark.createDataFrame(rows, schema=None if rows else target.schema)
df = df.select(*[df[f.name].cast(f.dataType) for f in target.schema.fields])
df.createOrReplaceTempView("src_pairs")
spark.sql("INSERT OVERWRITE cur.silver.instruction_pairs SELECT * FROM src_pairs")

dq = EvaluateDataQuality.apply(
    frame=DynamicFrame.fromDF(spark.table("cur.silver.instruction_pairs"), glue_ctx, "silver"),
    ruleset=transform.SILVER_DQDL,
    publishing_options={"dataQualityEvaluationContext": "silver_instruction_pairs",
                        "enableDataQualityResultsPublishing": True})
outcomes = [r.asDict() for r in dq.toDF().collect()]
report["dq"] = {"ruleset": transform.SILVER_DQDL,
                "outcomes": [{"rule": o.get("Rule"), "outcome": o.get("Outcome")} for o in outcomes]}
report["dq_run_id"] = args["dq_run_id"]

bucket, _, key = args["report_uri"][len("s3://"):].partition("/")
boto3.client("s3").put_object(Bucket=bucket, Key=key, ContentType="application/json",
                              Body=json.dumps(report, ensure_ascii=False, indent=2).encode())
print(json.dumps({"pairs": report["pairs"], "rules": {k: v["count"] for k, v in report["rules"].items()},
                  "dq": [(o["rule"], o["outcome"]) for o in report["dq"]["outcomes"]]}, ensure_ascii=False))
if any(o["outcome"] != "Passed" for o in report["dq"]["outcomes"]):
    raise SystemExit("Silver data-quality rules failed: see the report")
