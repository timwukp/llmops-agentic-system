"""Glue 5 job: landing JSONL → Bronze (Phase 1 PR-07).

Reads the generator's canonical files under s3://<DATA_BUCKET>/lakehouse/landing/<dataset>/ and
INSERT OVERWRITEs each Bronze table, so a demo dataset replaces the previous one whole instead of
accumulating beside it (a second load would otherwise double every resend the Silver rules count).
The schema comes from the table itself, not from this file.

Arguments: --account_id --raw_bucket --landing_uri
"""
import sys

from awsglue.utils import getResolvedOptions
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

TABLES = ("conversation_events", "crm_cdc", "erp_cdc", "web_events")

args = getResolvedOptions(sys.argv, ["account_id", "raw_bucket", "landing_uri"])
spark = (SparkSession.builder
         .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
         .config("spark.sql.catalog.raw", "org.apache.iceberg.spark.SparkCatalog")
         .config("spark.sql.catalog.raw.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
         .config("spark.sql.catalog.raw.glue.id", f"{args['account_id']}:s3tablescatalog/{args['raw_bucket']}")
         .getOrCreate())

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
    print(f"landed {table}: {spark.table(f'raw.bronze.{table}').count()} rows")
