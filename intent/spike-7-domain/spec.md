# Spec: S7 domain part

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `LLMOPS_S7_PART=domain`: project in domain `demo` (profile "Data analytics and AI-ML model
   development"), tags when the profile allows them; wait for every ON_CREATE environment.
2. Glue 5 write with the documented OpenLineage Spark configuration and `datazone:PostLineageEvent`.
3. DataZone Glue data source on `s3tablescatalog/<bucket>` through the project's connection; a
   read-only project-role grant removed before the project; READY wait; one retry.
4. Price from the Price List API (global entries).
5. `RUNNER_EXEMPT_SERVICES` with reasons; the grant guard reads it and rejects non-literal clients.
