# Spec: spike harness

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `tools/spikes/_spike.py`: `Spike` base (declared `PASS_CRITERIA`, offline `plan()`, `track()`
   registering a deleter at creation, `teardown()` newest-first and failure-tolerant, `execute()`
   with teardown in `finally`, tag `sweep`, redacted evidence + `.local.json` raw output) and a CLI.
2. Seven spike modules `s1`–`s7` with question, gate, criteria and planned resources.
3. S7 substrate `run()`: table bucket/namespace/table, format-version check, Glue integration check
   (stop, do not enable), IAM-only Glue 5 write, snapshot id, two UNLOADs hashed, non-empty-prefix
   UNLOAD expected to fail. The Unified Studio part is a follow-up.
4. Offline tests; no committed fixture contains a 12-digit run.
