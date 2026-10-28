# Verification: the eval gate's verdict has one declared shape (gate/1)

Intent chain: `intent/gate-contract/`. Date: 2026-10-28.

## The four shapes on record

Read from the four most recent `runs/*/evaluation/gate.json` (read-only, 2026-10-27):

| Run | Metrics under | Verdict spelling | Counts |
|---|---|---|---|
| 2026-10-16 trained | `gate_results` | not recorded here | `n`, `wilson95_ci_low/high` |
| 2026-10-23 untrained baseline | `decisions` | `fail_decisive` | `counts` `"47/120"` |
| 2026-10-23 demo | `results` | `decisive_pass` | `count` `"120/120"` |
| 2026-10-26 identifier grounding | `gates_applied` | `pass_decisive` | `n` and a `detail` string |

The eval prompt's gate clause named neither the file nor a shape; it asked only for
`metrics.gate_passed`. The provenance card and the Outcome tab read the file through
`provenance.gate_view`, which tried the four keys in turn.

## Offline

- **Instrument agrees with the files.** `gate_contract.wilson` reproduces the intervals all four
  files report, to six places: 87/120 → [0.63907, 0.796971], 120/120 → [0.968981, 1.0], 0/120 →
  [0.0, 0.031019], 47/120 → [0.308992, 0.481062], 85/120 → [0.621558, 0.782184].
- **Each legacy shape is rejected by name**, each legacy verdict spelling is rejected, and the
  2026-10-26 run's numbers in `gate/1` are accepted (`tests/test_gate_contract.py`, 49 tests).
- **Driver:** a conforming file is accepted; a legacy, absent or unreadable file is a rejection; the
  bars are read from the manifest's signed params; a `stage_complete` claiming a pass over a file
  that records a fail is rejected and the token settles on the agent's corrected call, with no
  `QualityGatePassed` emitted (`tests/test_orchestration.py`). The four existing eval-gate driver
  tests now carry a conforming file; a null `gate_passed` is refused and re-sent rather than read
  as false.
- **Negative controls:** 15, each caught (7 added with the hardening above) (interval not recomputed; signed bar not compared;
  borderline without directive; null claim let through; driver not checking; prompt dropping a
  rule; bundle without the schema; reader ignoring `gate/1`).
- `deploy/validate_config.py --config agents/eval/harness.json`: OK. `05_harnesses.py --agent eval
  --dry-run`: OK.

## Hardened before merge (2026-10-28 security audit)

Four of five audit reports independently named the first version of this contract as bypassable,
each with a working construction, before it was pushed:

- **Bars from an agent-writable file.** The harness role has `s3:PutObject` on `runs/*`, so an agent
  could lower `manifest.params.gates` and then write a `gate.json` consistent with the lowered bar.
  Now start-pipeline puts `signed_gates` on the run's DynamoDB row (the harness role has no access
  to that table) and the driver reads them there; a tampered manifest is ignored
  (`test_the_bars_come_from_the_run_row_not_the_agent_writable_manifest`).
- **NaN.** `json.loads` accepts `NaN`; `abs(x - nan) > 1e-9` is False, so the bar check passed, and
  both interval comparisons are False, so the verdict read as borderline. Refused at parse and in
  the document.
- **Rule choice.** A proportion relabelled `band` passed on a bare value. The four proportion
  metrics are `wilson95` or `missing`.
- **Invented directive.** Any non-empty string resolved a borderline. It must now equal a directive
  the stage-events table records as delivered to this run, and a borderline counted as passed must
  rest on a person's directive, not the conductor agent's.
- **Unbounded read.** gate.json was read whole into the 512 MB driver; now at most 256 KB.

Residual, stated: `k` and `n` themselves are the agent's report. The contract checks that the
verdict follows from them and from the run's bars; the truth of the counts rests on the canonical
scorers (`identifier_grounding` today) and is not re-counted by the driver.

## Not yet verified live

- Deploy the eval prompt (`05_harnesses.py --agent eval`) and the driver (`07_lambdas.py --only
  driver`), then a pipeline run whose gate writes `gate/1`, is accepted by the driver, and shows on
  the provenance card and the Outcome tab.
