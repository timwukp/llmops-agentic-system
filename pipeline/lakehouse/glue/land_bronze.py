"""Glue 5 job: landing JSONL → Bronze (Phase 1 PR-07).

Reads the generator's canonical files under s3://<DATA_BUCKET>/lakehouse/landing/<dataset>/ and
INSERT OVERWRITEs each Bronze table, so a demo dataset replaces the previous one whole instead of
accumulating beside it (a second load would otherwise double every resend the Silver rules count).
The schema comes from the table itself, not from this file.

The report (rows and current snapshot per table) is what the console's Data tab reads: the console
never reads the tables themselves, which are SSE-KMS and, in Bronze, hold PII.

Arguments: --account_id --raw_bucket --landing_uri --report_uri
"""
import json
import sys

import boto3

from awsglue.utils import getResolvedOptions
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

TABLES = ("conversation_events", "crm_cdc", "erp_cdc", "web_events")

args = getResolvedOptions(sys.argv, ["account_id", "raw_bucket", "landing_uri", "report_uri"])
spark = (SparkSession.builder
         .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
         .config("spark.sql.catalog.raw", "org.apache.iceberg.spark.SparkCatalog")
         .config("spark.sql.catalog.raw.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
         .config("spark.sql.catalog.raw.glue.id", f"{args['account_id']}:s3tablescatalog/{args['raw_bucket']}")
         .getOrCreate())

report = {"tables": {}}
for table in TABLES:
    target = spark.table(f"raw.bronze.{table}")
    df = spark.read.json(f"{args['landing_uri'].rstrip('/')}/bronze_{table}.jsonl")
    cols = []
    for field in target.schema.fields:
        c = F.col(field.name) if field.name in df.columns else F.lit(None)
        cols.append((F.to_timestamp(c) if field.dataType.simpleString() == "timestamp" else c)
                    .cast(field.dataType).alias(field.name))
    df.select(*cols).createOrReplaceTempView(f"src_{table}")
    spark.sql(f"INSERT OVERWRITE raw.bronze.{table} SELECT * FROM src_{table}")
    snap = spark.sql(f"SELECT snapshot_id FROM raw.bronze.{table}.snapshots "
                     "ORDER BY committed_at DESC LIMIT 1").collect()
    report["tables"][f"bronze.{table}"] = {"rows": spark.table(f"raw.bronze.{table}").count(),
                                           "snapshot_id": str(snap[0][0]) if snap else None}
    print(f"landed {table}: {report['tables'][f'bronze.{table}']}")

bucket, _, key = args["report_uri"][len("s3://"):].partition("/")
boto3.client("s3").put_object(Bucket=bucket, Key=key, ContentType="application/json",
                              Body=json.dumps(report, indent=2).encode())
