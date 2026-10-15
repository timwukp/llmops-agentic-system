# Spec: console Data tab (read-only)

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `GET /api/data`: datasets under `lakehouse/reports/` (newest first) and the tables of both lakehouse
   buckets via `s3tables:ListTables`.
2. `GET /api/data/run?dataset=`: the land/silver/gold reports and the landing manifest; the id matches
   `^[a-z0-9][a-z0-9-]{0,79}$` or the route answers 400.
3. The jobs' reports carry `tables` (rows, current snapshot); `land_bronze` writes one; `bronze_to_silver`
   adds `plant_check` when the landing holds `expected_defects.json`.
4. IAM: `LakehouseTablesMetadata` only — no `GetTableData`, no KMS.
5. Frontend: Data tab with summary cards, tables, rules vs plants, DQ, dataset picker; Exports/Lineage
   placeholders.
