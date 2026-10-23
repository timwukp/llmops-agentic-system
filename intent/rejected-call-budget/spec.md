# Spec: refused-call budget

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `REJECTED_CALL_BUDGET = 5`, `REJECTED_CALLS_ERROR = "RejectedCallsExhausted"`,
   `REFUSAL_STATUSES = ("rejected", "undeliverable", "unsupported")` in the driver.
2. Every non-terminal reply to a serviced call goes through `_answer()`, which counts a refusal and
   resets on anything else. `_rejected_calls` rides the continuation.
3. At the loop top, before the next turn: at the budget, settle the token (if any) as
   `RejectedCallsExhausted`, record a stage event, emit `PipelineFailed`, return `failed`. A triage
   then reaches `_backstop_page`.
4. Tests: the live triage loop replayed against a runtime that refuses toolResults; a stage refused
   every turn; an accepted call between refusals resets the count; the count rides the continuation.
   Negative controls for the check, the reset and the continuation key.
5. AGENTS.md gotcha sentence; CHANGELOG; counts.
