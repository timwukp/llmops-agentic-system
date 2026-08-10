"""What "this Step Functions task token is dead" looks like, in one place.

Two Lambdas settle task tokens they did not park, and both can arrive after the
execution they belong to has already ended:

  * ``resume_pipeline`` — an EventBridge SageMaker delivery for a job whose run has since
    timed out, been aborted, or been settled by another route.
  * ``harness_driver`` — every ``send_task_success`` / ``send_task_failure`` site,
    including the crash-path settle in ``handler()`` and the re-asks-exhausted settle in
    ``_run_stage``.

``resume_pipeline`` learned this first (its ``TASK_GONE_CODES`` predates this module) and
the driver did not, which cost four invocations: ``TaskTimedOut: 'Provided task does not
exist anymore'`` raised out of ``_run_stage``'s ``MissingStageComplete`` settle, was
re-raised by the ``handler()`` wrapper, and Lambda then retried the whole asynchronous
invocation twice -- 05:50:48Z, 05:52:03Z and 05:54:28Z on 2026-08-09, each retry a fresh
billed AgentCore turn re-running an agent whose stage had already been decided, against a
token that could never be settled by any of them.

So the knowledge lives here rather than being copied a second time. A second copy is the
defect this module exists to prevent: the driver's four settle sites and resume's one must
agree about what "gone" means, and two constants in two files agree only until someone
edits one of them.

Only stdlib (Lambda-safe, no external deps).
"""
from __future__ import annotations

#: The two ways Step Functions says a token is dead. ``TaskTimedOut`` carries the message
#: 'Provided task does not exist anymore' (the execution ended while the token was parked);
#: ``TaskDoesNotExist`` is the never-existed case.
#:
#: Matched by botocore error CODE, never by exception class: the typed classes hang off a
#: live client instance, so referencing ``sfn.exceptions.TaskTimedOut`` would make the
#: importing module unusable under an injected test double. And never by catching bare
#: ``Exception``, which would swallow the throttles and 5xx that genuinely MUST be retried
#: -- on those the settle may still be achievable, and giving up would strand the token
#: for its full ``TimeoutSeconds`` (86400s on every long-work state).
TASK_GONE_CODES = ("TaskTimedOut", "TaskDoesNotExist")


def is_task_gone(exc) -> bool:
    """True when `exc` says the token is dead, so retrying cannot ever succeed.

    Deliberately tolerant of a non-botocore exception: ``getattr`` chains to ``{}`` rather
    than raising, because this is called from inside ``except`` blocks whose whole job is
    to decide what to do next. A helper that can itself throw while classifying an error
    turns one failure into two.
    """
    return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "") \
        in TASK_GONE_CODES
