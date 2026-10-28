# Intent: the eval gate's verdict has one declared shape

- **Slug:** gate-contract
- **Author:** Claude (AI agent)
- **Date:** 2026-10-28
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's chat decision of 2026-10-28 ("好，開始做 gate.json"), from the queue agreed
during the provenance-card work: gate.json declared schema (four shapes from one prompt).

## Problem

The eval prompt never names `evaluation/gate.json` or its shape; the gate task is told only to call
`stage_complete` with `metrics.gate_passed`. Four live runs wrote four shapes: the metrics under
`gate_results`, `decisions`, `results` or `gates_applied`; verdicts `decisive_pass`,
`pass_decisive`, `fail_decisive`; counts as `"87/120"` under `count` or `counts`, or `n` alone. Two
readers depend on the file, the provenance card on the registered model and the console's Outcome
tab, and both read it by guessing among the four (`provenance.GATE_SHAPES`). A fifth spelling
would blank the scores on both with no error. Nothing checks that the file's verdicts follow from
its own numbers, that its bars are the signed ones, or that it agrees with the `gate_passed` the
state machine branches on.

## Outcome wanted

- One shape, `gate/1`, stated in the prompt and enforced by the driver at `stage_complete`, as the
  sweep's file is.
- A verdict a reviewer can re-derive from the file: the signed bars, each verdict from its rule (the
  Wilson interval recomputed), no unresolved borderline, and `gate_passed` consistent with the
  verdicts and with the state machine's input.
- The four earlier shapes still read, for the runs already on record.

## Acceptance

- `tests/test_gate_contract.py` (new) and the driver tests pass; negative controls.
- Live: a pipeline run's gate writes `gate/1`, the driver accepts it, and the provenance card and
  the Outcome tab show its scores.
