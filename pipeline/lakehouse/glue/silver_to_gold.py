"""Glue 5 job: Silver → Gold (Phase 1 PR-07).

Splits silver.instruction_pairs into gold.qlora_train / gold.qlora_eval with transform.py's
deterministic pair_id split (a row never changes side between runs), INSERT OVERWRITEs both, and
evaluates GOLD_DQDL on each. The report lands beside the Silver one.

Arguments: --account_id --curated_bucket --report_uri
"""
import json
import sys

import boto3
from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.utils import getResolvedOptions
from awsgluedq.transforms import EvaluateDataQuality
from pyspark.sql import SparkSession

import transform

args = getResolvedOptions(sys.argv, ["account_id", "curated_bucket", "report_uri"])
spark = (SparkSession.builder
         .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
         .config("spark.sql.catalog.cur", "org.apache.iceberg.spark.SparkCatalog")
         .config("spark.sql.catalog.cur.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
         .config("spark.sql.catalog.cur.glue.id", f"{args['account_id']}:s3tablescatalog/{args['curated_bucket']}")
         .getOrCreate())
glue_ctx = GlueContext(spark.sparkContext)

pairs = [r.asDict() for r in spark.table("cur.silver.instruction_pairs").collect()]
train, ev = transform.silver_to_gold(pairs)
report = {"silver_pairs": len(pairs), "train": len(train), "eval": len(ev), "dq": {}, "tables": {}}
for table, rows in (("qlora_train", train), ("qlora_eval", ev)):
    target = spark.table(f"cur.gold.{table}")
    df = spark.createDataFrame(rows, schema=None if rows else target.schema)
    df.select(*[df[f.name].cast(f.dataType) for f in target.schema.fields]).createOrReplaceTempView(f"src_{table}")
    spark.sql(f"INSERT OVERWRITE cur.gold.{table} SELECT * FROM src_{table}")
    dq = EvaluateDataQuality.apply(
        frame=DynamicFrame.fromDF(spark.table(f"cur.gold.{table}"), glue_ctx, table),
        ruleset=transform.GOLD_DQDL,
        publishing_options={"dataQualityEvaluationContext": f"gold_{table}",
                            "enableDataQualityResultsPublishing": True})
    report["dq"][table] = [{"rule": o["Rule"], "outcome": o["Outcome"]} for o in
                           (r.asDict() for r in dq.toDF().collect())]
    snap = spark.sql(f"SELECT snapshot_id FROM cur.gold.{table}.snapshots "
                     "ORDER BY committed_at DESC LIMIT 1").collect()
    report["tables"][f"gold.{table}"] = {"rows": len(rows), "snapshot_id": str(snap[0][0]) if snap else None}

bucket, _, key = args["report_uri"][len("s3://"):].partition("/")
boto3.client("s3").put_object(Bucket=bucket, Key=key, ContentType="application/json",
                              Body=json.dumps(report, indent=2).encode())
print(json.dumps(report))
if any(o["outcome"] != "Passed" for outs in report["dq"].values() for o in outs):
    raise SystemExit("Gold data-quality rules failed: see the report")
