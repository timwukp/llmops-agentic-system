# Intent: one run from raw customer data to a judged, deployed model, and a pipeline you can see

- **Slug:** m1-lakehouse-e2e
- **Author:** Claude (AI agent)
- **Date:** 2026-10-23
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's chat decision of 2026-10-23: M1 first ("從 M1 開始"), with the synthetic
customer data ("模擬的客戶數據"). The goal, in the owner's words: "完全 end to end from customer raw
data to the results/outcome", and a visible pipeline.

## Problem

Every piece of the batch chain already runs, but as two runs and with the data half invisible:
- **Two runs:** a `lakehouse_ingest` run builds Bronze → Silver → Gold and stops, and a separate
  `lakehouse` run trains on a dataset id someone copies from the Data tab.
- **Invisible data stages:** the console's pipeline view draws no card for the five lakehouse Glue
  states, so the data engineering half of the pipeline does not appear in it at all.

## Outcome wanted

- **One dispatch, raw data → outcome:** a `lakehouse` run given a generator seed and scale (instead
  of an existing dataset id) builds its own dataset, then exports, reviews, trains, judges, deploys,
  monitors and tears down, in a single execution.
- **One button:** the console starts that run with the demo settings.
- **A pipeline you can see:** the pipeline view draws every stage the run actually has, Raw →
  Bronze → Silver → Gold → Extract → … → Complete, with live status.

## Acceptance

- Offline guards and negative controls.
- A live run started from the console button reaches a gate verdict, with every stage shown on the
  pipeline view.
