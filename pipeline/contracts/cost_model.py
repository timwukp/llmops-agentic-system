"""Cost estimation, approval gating, attribution and reconciliation — pure functions.

This is the single place cost arithmetic lives, mirroring ``report.py``'s role as the
one canonical report writer. Nothing here touches AWS: the caller fetches rates and
realized usage, this module decides what they mean. That split is what makes the
$2000 approval gate testable without a bill.

The design rule, from which most of the odd-looking details below follow:

    an ESTIMATE may be a guess, but an ACTUAL may never be one.

So every rate carries where it came from (``source``) and when (``as_of``); every line
item carries the formula that produced it (``basis``); a SKU with no rate is *listed*
in ``unpriced`` rather than silently contributing $0; and a Cost Explorer period still
marked ``Estimated`` settles as ``provisional``, never as ``settled``. A confidently
wrong cost figure is worse than an admitted unknown, because a human approves a
$2000 run on the strength of it.

Measured facts this module is calibrated against (all verified on the live account
2026-07-31, see docs/COST.md):

* ml.g5.2xlarge on-demand training = $1.515/hr (Price List API, us-east-1).
* The 2026-07-31 QLoRA run: 16,550 train rows, 24,924 s of training, 25,592 s billed,
  **$10.77**. That gives throughput 0.664 rows/s and ~670 s of fixed setup overhead
  (image pull, data download, model upload) — both used as defaults below, so the
  estimator reproduces a run whose actual cost is known.
* The Price List API cannot price Claude Fable 5 or Opus 5 — every ``provider=Anthropic``
  entry for us-east-1 is Claude 3 or older. Those are the models the seven harnesses
  themselves run on, i.e. the largest AgentCore line, so realized unit rates derived from
  our own bill outrank it. Hence RATE_PRECEDENCE. (It *does* price DeepSeek-R1, to within
  <0.001% of realized; an earlier note here said otherwise because the ``model`` attribute
  value is bare ``R1`` with ``provider=DeepSeek``, which reading the model list misses.)

Only stdlib (Lambda-safe, importable by the console zip and by pytest alike).
"""
from __future__ import annotations

import datetime
from typing import Any, Iterable

# ── rate provenance ───────────────────────────────────────────────────────────
#: Rate sources, best first. ``ce_realized`` (cost ÷ quantity from our OWN bill) beats
#: the Price List API because the Price List API is demonstrably stale for this
#: pipeline's models; ``fallback_static`` is a hardcoded guess and is never silently
#: trusted — it downgrades the estimate's confidence.
RATE_PRECEDENCE = ("ce_realized", "price_list", "fallback_static")

#: Confidence a rate source can support.
_SOURCE_CONFIDENCE = {"ce_realized": "measured", "price_list": "modelled",
                      "fallback_static": "guessed"}
_CONFIDENCE_ORDER = ("measured", "modelled", "guessed")

# ── calibration constants (measured, not guessed — see module docstring) ──────
#: Train rows per second for QLoRA on ml.g5.2xlarge, from the 2026-07-31 run.
MEASURED_ROWS_PER_SEC = 16_550 / 24_924  # = 0.6640
#: Fixed per-job overhead billed outside the training loop (image pull, S3 down/upload).
MEASURED_SETUP_OVERHEAD_S = 670.0
#: Where the throughput number came from, carried into every estimate's assumptions.
THROUGHPUT_PROVENANCE = ("throughput 0.664 rows/s and 670 s setup overhead measured on "
                         "the 2026-07-31 ml.g5.2xlarge run (16,550 rows / 24,924 s, "
                         "$10.77 billed)")

#: Categories the remediation loop can re-run. The state machine's
#: RemediateFinetune → FinetuneAnalyze cycle repeats training and the finetune agent's
#: turns, but NOT data generation (the dataset already exists) — so applying
#: max_iterations to the teacher line would overstate as badly as ignoring it
#: understates the training line.
REMEDIABLE_CATEGORIES = frozenset({"sagemaker_training", "agentcore_runtime",
                                   "agentcore_model"})

#: Every category an estimate can carry, in report order.
CATEGORIES = ("sagemaker_training", "sagemaker_inference", "bedrock_teacher",
              "agentcore_runtime", "agentcore_model", "agentcore_memory",
              "agentcore_evaluations", "support")

#: Default approval thresholds (USD). Dual gate: a single expensive run trips the
#: first, a drip of cheap runs against the same project trips the second.
DEFAULT_SINGLE_RUN_LIMIT_USD = 2000.0
DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD = 2000.0

#: Estimate lifecycle. ``draft`` never launches; only ``approved`` does.
ESTIMATE_STATUSES = ("draft", "pending_approval", "approved", "rejected",
                     "launched", "reconciled")

#: Resource-id substrings that ARE this project's. Attribution is an ALLOWLIST, never
#: "everything in this AWS service": the same account carries SageMaker Canvas
#: sessions ($296) and a JumpStart Whisper endpoint ($18/day) that have nothing to do
#: with this pipeline, and a service-level rollup would bill them to us.
PROJECT_RESOURCE_PATTERNS = ("training-job/llmops-", "endpoint/llmops-",
                             "endpoint-config/llmops-", "runtime/harness_llmops_")


# ── rate card ─────────────────────────────────────────────────────────────────
class RateCard:
    """A priced SKU table that remembers where each price came from.

    Deliberately not a bare ``dict[str, float]``. A $0 rate from a stale pricing feed
    and a $0 rate from a genuinely free tier are indistinguishable once the provenance
    is dropped, and the first one silently zeroes a line item that really costs money
    (measured: the Price List API returns nothing for Claude Fable 5 or Opus 5, the
    models these harnesses themselves run on). ``get()`` returning ``None`` for an
    unknown SKU — rather than 0.0 — is the mechanism that forces the caller into
    ``unpriced``.
    """

    def __init__(self, rates: dict | None = None):
        self.rates: dict[str, dict] = {}
        for sku, entry in (rates or {}).items():
            self.put(sku, entry)

    def put(self, sku: str, entry: dict) -> None:
        """Insert a rate, keeping the better source if the SKU is already priced."""
        entry = dict(entry or {})
        source = str(entry.get("source", "fallback_static"))
        if source not in RATE_PRECEDENCE:
            source = "fallback_static"
        entry["source"] = source
        entry["unit_price"] = float(entry.get("unit_price", 0.0))
        entry["unit"] = str(entry.get("unit", "unit"))
        existing = self.rates.get(sku)
        if existing and RATE_PRECEDENCE.index(existing["source"]) <= RATE_PRECEDENCE.index(source):
            return  # already have an equal-or-better source; first writer of a tier wins
        self.rates[sku] = entry

    def get(self, sku: str) -> dict | None:
        """The rate entry, or None. Never a zero — see the class docstring."""
        return self.rates.get(sku)

    def price(self, sku: str) -> float | None:
        e = self.get(sku)
        return None if e is None else float(e["unit_price"])

    def source(self, sku: str) -> str | None:
        e = self.get(sku)
        return None if e is None else e["source"]

    def stale_skus(self, now: datetime.datetime | None = None, max_age_days: int = 30) -> list[str]:
        """SKUs whose ``as_of`` is missing or older than ``max_age_days``.

        Powers the console's rate-card health panel: a rate nobody has refreshed is
        still a number on the screen, and the operator has no way to know unless the
        age is shown.
        """
        now = now or datetime.datetime.now(datetime.timezone.utc)
        out = []
        for sku, e in self.rates.items():
            as_of = e.get("as_of")
            if not as_of:
                out.append(sku)
                continue
            try:
                ts = datetime.datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                out.append(sku)
                continue
            if (now - ts).days > max_age_days:
                out.append(sku)
        return sorted(out)

    def to_dict(self) -> dict:
        return {"rates": dict(self.rates), "n_rates": len(self.rates)}


# ── SKU naming ────────────────────────────────────────────────────────────────
def sku_training(instance: str) -> str:
    return f"sagemaker:training:{instance}"


def sku_inference(instance: str) -> str:
    return f"sagemaker:inference:{instance}"


def sku_tokens(model: str, direction: str) -> str:
    return f"bedrock:{model}:{direction}-tokens"


SKU_AGENTCORE_VCPU = "agentcore:runtime:vcpu-hours"
SKU_AGENTCORE_GB = "agentcore:runtime:gb-hours"
SKU_AGENTCORE_MEMORY = "agentcore:memory:short-term-events"
SKU_AGENTCORE_EVALS = "agentcore:evaluations:builtin-input-tokens"
SKU_SUPPORT = "support:s3-ddb-lambda-sfn"


# ── estimation ────────────────────────────────────────────────────────────────
def _line(category: str, sku: str, quantity: float, unit: str, basis: str,
          rates: RateCard, unpriced: list[str], remediable: bool | None = None) -> dict:
    """Build one line item, routing an unpriced SKU into ``unpriced``.

    An unpriced line is still EMITTED (with ``cost_usd: None``) rather than dropped:
    a missing line is invisible in the UI, while a visible line with no price is a
    question the operator can act on.
    """
    entry = rates.get(sku)
    if remediable is None:
        remediable = category in REMEDIABLE_CATEGORIES
    item = {"category": category, "sku": sku, "quantity": round(float(quantity), 6),
            "unit": unit, "basis": basis, "remediable": bool(remediable)}
    if entry is None:
        unpriced.append(sku)
        item.update({"unit_price": None, "cost_usd": None, "rate_source": None,
                     "rate_as_of": None})
        return item
    cost = float(quantity) * float(entry["unit_price"])
    item.update({"unit_price": float(entry["unit_price"]), "cost_usd": round(cost, 6),
                 "rate_source": entry["source"], "rate_as_of": entry.get("as_of")})
    return item


def estimate_run(plan: dict, rates: RateCard | dict) -> dict:
    """Estimate one pipeline run, line by line.

    ``plan`` follows the manifest's ``params`` shape (see
    ``orchestration/start_pipeline/handler.py`` DEFAULT_PARAMS): ``sample_count``,
    ``keep_reasoning``, ``max_iterations``, ``training_instance``,
    ``inference_instance``, plus optional overrides documented inline below.

    Returns line items, per-category subtotals, ``total_usd`` (expected) AND
    ``worst_case_usd`` (every remediation iteration taken). The approval gate reads
    the worst case: approving $2000 that can silently become $6000 because the
    self-remediation loop ran three times is not a gate.
    """
    if isinstance(rates, dict):
        rates = RateCard(rates)
    plan = dict(plan or {})
    unpriced: list[str] = []
    assumptions: list[str] = [THROUGHPUT_PROVENANCE]
    items: list[dict] = []

    sample_count = float(plan.get("sample_count", 0) or 0)
    max_iterations = max(1, int(plan.get("max_iterations", 1) or 1))

    # 1. SageMaker training — the biggest and best-calibrated line.
    train_instance = str(plan.get("training_instance", "ml.g5.2xlarge"))
    rows = float(plan.get("train_rows", 0) or 0) or sample_count
    throughput = float(plan.get("rows_per_sec", 0) or 0) or MEASURED_ROWS_PER_SEC
    overhead_s = float(plan.get("setup_overhead_s", MEASURED_SETUP_OVERHEAD_S))
    if rows:
        train_hours = (rows / throughput + overhead_s) / 3600.0
        items.append(_line(
            "sagemaker_training", sku_training(train_instance), train_hours, "hours",
            f"({rows:,.0f} rows / {throughput:.3f} rows/s + {overhead_s:.0f} s setup) / 3600",
            rates, unpriced))

    # 2. SageMaker inference — an endpoint left running is the single largest cost
    #    risk in this repo, so the teardown assumption is stated, not implied.
    inf_instance = str(plan.get("inference_instance", "ml.g5.xlarge"))
    inf_hours = float(plan.get("endpoint_hours", 1.0) or 0)
    if inf_hours:
        teardown = plan.get("teardown", True)
        items.append(_line(
            "sagemaker_inference", sku_inference(inf_instance), inf_hours, "hours",
            f"{inf_hours:g} endpoint hours (teardown={'on' if teardown else 'OFF'})",
            rates, unpriced))
        assumptions.append(
            f"endpoint billed for {inf_hours:g} h then deleted by the Teardown stage"
            if teardown else
            "TEARDOWN DISABLED: the endpoint bills until someone deletes it by hand — "
            "this estimate covers only the hours named, not the ones after")

    # 3. Bedrock teacher tokens. keep_reasoning=True keeps DeepSeek-R1's <think>
    #    chains, which for ARC ARE the training target — so it multiplies the OUTPUT
    #    side several-fold. Today that parameter's cost impact is invisible.
    teacher = str(plan.get("teacher_model") or (plan.get("models") or {}).get("teacher")
                  or "us.deepseek.r1-v1:0")
    if sample_count:
        in_tok = float(plan.get("teacher_input_tokens_per_sample", 1500))
        base_out = float(plan.get("teacher_output_tokens_per_sample", 700))
        keep_reasoning = bool(plan.get("keep_reasoning", True))
        reasoning_mult = float(plan.get("reasoning_multiplier", 4.0))
        out_tok = base_out * (reasoning_mult if keep_reasoning else 1.0)
        items.append(_line(
            "bedrock_teacher", sku_tokens(teacher, "input"),
            sample_count * in_tok / 1000.0, "1K tokens",
            f"{sample_count:,.0f} samples x {in_tok:g} input tokens / 1000",
            rates, unpriced, remediable=False))
        items.append(_line(
            "bedrock_teacher", sku_tokens(teacher, "output"),
            sample_count * out_tok / 1000.0, "1K tokens",
            f"{sample_count:,.0f} samples x {base_out:g} output tokens"
            + (f" x {reasoning_mult:g} (keep_reasoning=True)" if keep_reasoning else "")
            + " / 1000",
            rates, unpriced, remediable=False))
        assumptions.append(
            f"keep_reasoning={keep_reasoning} -> teacher output tokens "
            f"x{reasoning_mult:g}" if keep_reasoning else
            "keep_reasoning=False -> no <think> chains kept, teacher output un-multiplied")

    # 4. AgentCore runtime: the harnesses' own compute, one billed session per stage.
    stages = int(plan.get("n_stages", 9) or 0)
    if stages:
        sess_min = float(plan.get("minutes_per_stage", 6.0))
        vcpu = float(plan.get("runtime_vcpu", 1.0))
        gb = float(plan.get("runtime_gb", 2.0))
        hours = stages * sess_min / 60.0
        items.append(_line("agentcore_runtime", SKU_AGENTCORE_VCPU, hours * vcpu,
                           "vCPU-hours",
                           f"{stages} stages x {sess_min:g} min x {vcpu:g} vCPU / 60",
                           rates, unpriced))
        items.append(_line("agentcore_runtime", SKU_AGENTCORE_GB, hours * gb, "GB-hours",
                           f"{stages} stages x {sess_min:g} min x {gb:g} GB / 60",
                           rates, unpriced))

        # 5. The harness LLM itself. Fable 5 at $10/$50 per MTok is the most expensive
        #    model in the stack, and nothing in the repo has ever surfaced its cost.
        harness_model = str(plan.get("harness_model", "global.anthropic.claude-fable-5"))
        h_in = float(plan.get("harness_input_tokens_per_stage", 40_000))
        h_out = float(plan.get("harness_output_tokens_per_stage", 6_000))
        items.append(_line("agentcore_model", sku_tokens(harness_model, "input"),
                           stages * h_in / 1000.0, "1K tokens",
                           f"{stages} stages x {h_in:,.0f} input tokens / 1000",
                           rates, unpriced))
        items.append(_line("agentcore_model", sku_tokens(harness_model, "output"),
                           stages * h_out / 1000.0, "1K tokens",
                           f"{stages} stages x {h_out:,.0f} output tokens / 1000",
                           rates, unpriced))

        # 6/7. Memory events and online evaluations — small, but named, so
        #      "where did the rest go?" has an answer instead of a shrug.
        mem_events = float(plan.get("memory_events", stages * 20))
        items.append(_line("agentcore_memory", SKU_AGENTCORE_MEMORY, mem_events,
                           "events", f"{stages} stages x 20 short-term memory events",
                           rates, unpriced, remediable=False))
        eval_tok = float(plan.get("evaluation_input_tokens", 0))
        if eval_tok:
            items.append(_line("agentcore_evaluations", SKU_AGENTCORE_EVALS,
                               eval_tok / 1_000_000.0, "1M tokens",
                               f"{eval_tok:,.0f} evaluated input tokens / 1e6",
                               rates, unpriced, remediable=False))

    # 8. Everything else, as one honest lump rather than an implied zero.
    support = float(plan.get("support_usd", 0.50))
    if support:
        items.append({"category": "support", "sku": SKU_SUPPORT, "quantity": 1.0,
                      "unit": "run", "unit_price": support, "cost_usd": round(support, 6),
                      "basis": "flat allowance for S3 + DynamoDB + Lambda + Step Functions",
                      "rate_source": "fallback_static", "rate_as_of": None,
                      "remediable": False})

    subtotals: dict[str, float] = {}
    total = 0.0
    remediable_total = 0.0
    for it in items:
        if it["cost_usd"] is None:
            continue
        subtotals[it["category"]] = round(subtotals.get(it["category"], 0.0) + it["cost_usd"], 6)
        total += it["cost_usd"]
        if it["remediable"]:
            remediable_total += it["cost_usd"]

    worst_case = total + (max_iterations - 1) * remediable_total
    if max_iterations > 1:
        assumptions.append(
            f"worst case assumes all {max_iterations} remediation iterations run, "
            f"repeating ${remediable_total:,.2f} of re-runnable cost "
            f"({', '.join(sorted(REMEDIABLE_CATEGORIES))})")

    # Confidence is the WEAKEST source in play, not the average: one guessed rate on
    # the dominant line makes the whole total a guess.
    confidence = "measured"
    for it in items:
        src = it.get("rate_source")
        c = _SOURCE_CONFIDENCE.get(src or "", "guessed")
        if _CONFIDENCE_ORDER.index(c) > _CONFIDENCE_ORDER.index(confidence):
            confidence = c
    if unpriced:
        confidence = "guessed"
        assumptions.append(
            f"{len(unpriced)} SKU(s) have no rate and contribute $0 to the total: "
            f"{', '.join(sorted(set(unpriced)))} — the total is a FLOOR, not an estimate")

    return {
        "line_items": items,
        "subtotals": subtotals,
        "total_usd": round(total, 4),
        "worst_case_usd": round(worst_case, 4),
        "max_iterations": max_iterations,
        "remediable_usd": round(remediable_total, 4),
        "confidence": confidence,
        "unpriced": sorted(set(unpriced)),
        "assumptions": assumptions,
        "plan": plan,
    }


# ── approval gating ───────────────────────────────────────────────────────────
def approval_decision(estimate: dict, project_to_date_usd: float = 0.0,
                      single_run_limit_usd: float = DEFAULT_SINGLE_RUN_LIMIT_USD,
                      cumulative_limit_usd: float = DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD) -> dict:
    """Does this run need a human approver? Dual threshold, gated on the worst case.

    Two independent limits, because they catch different failure modes: one $5000 run
    trips ``single_run``, while twenty $150 runs against the same project trip
    ``cumulative`` and would otherwise sail through one at a time.

    Reads ``worst_case_usd`` deliberately — see ``estimate_run``.
    """
    gating = float(estimate.get("worst_case_usd", estimate.get("total_usd", 0.0)) or 0.0)
    to_date = float(project_to_date_usd or 0.0)
    reasons = []
    if gating > float(single_run_limit_usd):
        reasons.append(f"single-run worst case ${gating:,.2f} exceeds "
                       f"${float(single_run_limit_usd):,.2f}")
    if to_date + gating > float(cumulative_limit_usd):
        reasons.append(f"project to-date ${to_date:,.2f} + ${gating:,.2f} = "
                       f"${to_date + gating:,.2f} exceeds ${float(cumulative_limit_usd):,.2f}")
    return {
        "approval_required": bool(reasons),
        "reasons": reasons,
        "gating_usd": round(gating, 4),
        "gating_basis": "worst_case_usd",
        "project_to_date_usd": round(to_date, 4),
        "single_run_limit_usd": float(single_run_limit_usd),
        "cumulative_limit_usd": float(cumulative_limit_usd),
        "status": "pending_approval" if reasons else "approved",
    }


def check_approval(record: dict, approver: str, approver_groups: Iterable[str],
                   required_group: str = "llmops-approver") -> dict:
    """Validate an approve/reject action against separation of duties.

    Two rules, both enforced here rather than in the UI, because a hidden button is
    not an access control:

    1. the actor must hold ``required_group``;
    2. the actor must not be the requester — self-approval is REJECTED outright, not
       merely annotated, so a $2000 gate cannot be cleared by the person who opened it.
    """
    groups = {str(g) for g in (approver_groups or [])}
    if required_group not in groups:
        return {"allowed": False, "code": 403,
                "error": f"approval requires membership of the {required_group} group"}
    requested_by = str(record.get("requested_by", "") or "")
    if requested_by and str(approver) == requested_by:
        return {"allowed": False, "code": 403,
                "error": "separation of duties: the requester cannot approve their own "
                         "estimate — a second approver is required"}
    status = str(record.get("status", ""))
    if status != "pending_approval":
        return {"allowed": False, "code": 409,
                "error": f"estimate is {status or 'unknown'}, not pending_approval"}
    return {"allowed": True, "code": 200}


def can_launch(record: dict) -> dict:
    """Only an ``approved`` estimate may launch a run."""
    status = str((record or {}).get("status", ""))
    if status == "approved":
        return {"ok": True}
    return {"ok": False, "code": 409,
            "error": f"estimate status is {status or 'missing'}; only 'approved' may launch"}


# ── attribution ───────────────────────────────────────────────────────────────
def is_project_resource(resource_id: str,
                        patterns: Iterable[str] = PROJECT_RESOURCE_PATTERNS) -> bool:
    """Allowlist test for a Cost Explorer ``RESOURCE_ID``.

    Measured hazard: this AWS account also bills ``USE1-Canvas:Session-Hrs`` ($296)
    and ``endpoint/jumpstart-dft-hf-asr-whisper-large-v2`` ($18/day). Filtering by
    service alone would report hundreds of dollars of unrelated spend as this
    project's, so a resource must MATCH to be counted — unrecognised means excluded.
    """
    rid = str(resource_id or "")
    return any(p in rid for p in patterns)


def run_id_from_resource(resource_id: str) -> str | None:
    """Extract ``run_id`` from a SageMaker job/endpoint name.

    Training job names are ``llmops-qlora-<run_id>-<seq>-<pool>`` (measured:
    ``llmops-qlora-run-phase2-main-0001-r3``); endpoints are ``llmops-student-<run_id>``
    with no trailing decoration. So per-run attribution needs no cost-allocation tags
    at all — which matters, because the ``project`` tag is Inactive on this account and
    tag-filtered Cost Explorer returns $0 for days that really did spend money.

    Exactly two optional trailing tokens are stripped, in this order: a capacity-pool
    tag (a letter plus digits, ``r3``/``e3`` — the capacity race appends one) and a
    numeric sequence (``0001``). The bound is the point: stripping *any* short trailing
    token in a loop consumed short run_ids whole, so ``llmops-student-run-a`` attributed
    to ``run`` and its endpoint cost landed on a run that does not exist.
    """
    rid = str(resource_id or "")
    name = rid.rsplit("/", 1)[-1]
    for prefix in ("llmops-qlora-", "llmops-student-", "llmops-"):
        if name.startswith(prefix):
            parts = name[len(prefix):].split("-")
            if len(parts) > 1 and _is_pool_tag(parts[-1]):
                parts.pop()
            if len(parts) > 1 and parts[-1].isdigit():
                parts.pop()
            return "-".join(parts) or None
    return None


def _is_pool_tag(token: str) -> bool:
    """``r3``/``e3``-style capacity-pool suffix: one letter then digits."""
    return len(token) >= 2 and token[0].isalpha() and token[1:].isdigit()


def settlement_for(ce_estimated: bool) -> str:
    """Cost Explorer's ``Estimated`` flag → our settlement state.

    CE lags roughly 24 h and marks recent periods estimated. Publishing such a period
    as settled is how a number that will still move gets quoted as final, so the flag
    is mapped straight through and reconciliation is expected to re-run.
    """
    return "provisional" if bool(ce_estimated) else "settled"


def attribute_actuals(ce_groups: Iterable[dict], project: str, period: str,
                      ce_estimated: bool = False,
                      patterns: Iterable[str] = PROJECT_RESOURCE_PATTERNS) -> dict:
    """Fold resource-level Cost Explorer groups into per-run, per-category actuals.

    ``ce_groups`` items are ``{"resource_id": str, "service": str, "cost_usd": float,
    "usage_type": str?}`` — already flattened from the CE response by the caller.
    """
    rows: dict[tuple, dict] = {}
    excluded: list[dict] = []
    total = 0.0
    for g in ce_groups or []:
        rid = str(g.get("resource_id", ""))
        cost = float(g.get("cost_usd", 0.0) or 0.0)
        if not is_project_resource(rid, patterns):
            excluded.append({"resource_id": rid, "cost_usd": round(cost, 6),
                             "reason": "not in project allowlist"})
            continue
        category = ("sagemaker_training" if "training-job/" in rid
                    else "sagemaker_inference" if "endpoint" in rid
                    else "agentcore_runtime" if "runtime/" in rid
                    else "support")
        run_id = run_id_from_resource(rid) or "unattributed"
        key = (run_id, category)
        row = rows.setdefault(key, {
            "project": project, "period": period, "run_id": run_id,
            "category": category, "cost_usd": 0.0, "resources": [],
            "settlement": settlement_for(ce_estimated),
            "ce_estimated_flag": bool(ce_estimated)})
        row["cost_usd"] = round(row["cost_usd"] + cost, 6)
        row["resources"].append(rid)
        total += cost
    return {
        "project": project, "period": period,
        "settlement": settlement_for(ce_estimated),
        "rows": [dict(v, sk=f"{period}#{v['run_id']}#{v['category']}")
                 for v in rows.values()],
        "total_usd": round(total, 6),
        "excluded": excluded,
        "excluded_usd": round(sum(e["cost_usd"] for e in excluded), 6),
    }


def cross_check_tagged_total(resource_total_usd: float, tagged_total_usd: float,
                            tag_active: bool) -> dict:
    """Compare tag-filtered spend against resource-level spend, honestly.

    Reproduced live on 2026-07-31: with the ``project`` cost-allocation tag Inactive,
    a tag-filtered Cost Explorer query returned **$0.00** for a day with real spend.
    So a $0 tagged total must never be reported as a $0 project total — the correct
    output is "the tag is not usable yet", and the resource-level number stands.
    """
    res = float(resource_total_usd or 0.0)
    tag = float(tagged_total_usd or 0.0)
    if not tag_active:
        return {"authoritative_total_usd": round(res, 6), "source": "resource_level",
                "tag_usable": False,
                "note": "cost-allocation tag is Inactive, so its $0 is an artefact of "
                        "the tag never having been activated, not an absence of spend"}
    agrees = res == 0 or abs(res - tag) / max(res, 1e-9) <= 0.05
    return {"authoritative_total_usd": round(res, 6), "source": "resource_level",
            "tag_usable": True, "tagged_total_usd": round(tag, 6), "tags_agree": agrees,
            "note": "tag agrees with resource-level attribution" if agrees else
                    "tag DISAGREES with resource-level attribution — the tag is not "
                    "retroactive, so pre-activation usage is legitimately missing"}


# ── reconciliation ────────────────────────────────────────────────────────────
def reconcile(estimate: dict, actual: dict) -> dict:
    """Estimate vs actual, per category, naming the line that drove the miss.

    One aggregate percentage tells you the estimate was wrong; it does not tell you
    what to fix. Naming the driving category is the whole point — that is the input to
    improving estimate accuracy next time.
    """
    est_sub = dict((estimate or {}).get("subtotals") or {})
    act_sub = dict((actual or {}).get("subtotals") or {})
    if not act_sub and (actual or {}).get("rows"):
        for r in actual["rows"]:
            act_sub[r["category"]] = round(act_sub.get(r["category"], 0.0)
                                           + float(r.get("cost_usd", 0.0)), 6)

    categories = sorted(set(est_sub) | set(act_sub))
    per_category = []
    for c in categories:
        e = float(est_sub.get(c, 0.0))
        a = float(act_sub.get(c, 0.0))
        delta = a - e
        per_category.append({
            "category": c, "estimate_usd": round(e, 6), "actual_usd": round(a, 6),
            "delta_usd": round(delta, 6),
            "variance_pct": None if e == 0 else round(100.0 * delta / e, 2),
        })

    est_total = float((estimate or {}).get("total_usd", 0.0) or 0.0)
    act_total = float((actual or {}).get("total_usd", 0.0) or sum(act_sub.values()))
    driver = max(per_category, key=lambda r: abs(r["delta_usd"]), default=None)
    settlement = str((actual or {}).get("settlement", "provisional"))

    if est_total == 0:
        verdict = ("no estimate to compare — this run's spend was never estimated"
                   if act_total else "no estimate and no actual")
        accuracy = None
        variance_pct = None
    else:
        accuracy = round(act_total / est_total, 4)
        variance_pct = round(100.0 * (act_total - est_total) / est_total, 2)
        direction = "over" if act_total > est_total else "under"
        verdict = (f"actual ${act_total:,.2f} came in {abs(variance_pct):.1f}% {direction} "
                   f"the ${est_total:,.2f} estimate; largest contributor: "
                   f"{driver['category']} ({driver['delta_usd']:+,.2f})"
                   if driver else
                   f"actual ${act_total:,.2f} vs estimate ${est_total:,.2f}")
    if settlement == "provisional":
        verdict += " — PROVISIONAL: Cost Explorer still marks this period estimated, " \
                   "so re-reconcile once it settles"

    return {
        "estimate_usd": round(est_total, 4), "actual_usd": round(act_total, 4),
        "delta_usd": round(act_total - est_total, 4),
        "variance_pct": variance_pct, "accuracy_ratio": accuracy,
        "per_category": per_category,
        "driver": driver["category"] if driver else None,
        "settlement": settlement,
        "verdict": verdict,
    }


# ── rate derivation from realized billing ────────────────────────────────────
def realized_rates(ce_usage: Iterable[dict], as_of: str,
                   unit_scale: dict | None = None) -> RateCard:
    """Derive unit prices from our own bill: unit_price = cost ÷ quantity.

    This is the authoritative rate source for anything we have already used, and the
    only one that can price Claude Fable 5 or Opus 5 at all — the models the harnesses
    themselves run on (the Price List API lists neither; verified 2026-07-31, when it
    *did* turn out to price DeepSeek-R1). Zero-quantity groups are skipped rather
    than divided by, because a $0/0 rate would silently price a real SKU at nothing.
    """
    card = RateCard()
    for row in ce_usage or []:
        sku = str(row.get("sku") or row.get("usage_type") or "")
        qty = float(row.get("quantity", 0.0) or 0.0)
        cost = float(row.get("cost_usd", 0.0) or 0.0)
        if not sku or qty <= 0:
            continue
        scale = float((unit_scale or {}).get(sku, 1.0))
        card.put(sku, {"unit_price": cost / qty * scale,
                       "unit": str(row.get("unit", "unit")),
                       "source": "ce_realized", "as_of": as_of})
    return card


def merge_rates(*cards: RateCard | dict) -> RateCard:
    """Merge rate cards under RATE_PRECEDENCE (best source wins per SKU)."""
    out = RateCard()
    ordered: list[tuple[int, str, dict]] = []
    for c in cards:
        rates = c.rates if isinstance(c, RateCard) else dict(c or {})
        for sku, entry in rates.items():
            src = str((entry or {}).get("source", "fallback_static"))
            idx = RATE_PRECEDENCE.index(src) if src in RATE_PRECEDENCE else len(RATE_PRECEDENCE)
            ordered.append((idx, sku, entry))
    for _, sku, entry in sorted(ordered, key=lambda t: t[0]):
        out.put(sku, entry)
    return out


def rate_card_health(card: RateCard, required_skus: Iterable[str],
                     now: datetime.datetime | None = None) -> dict:
    """What the console's rate-card health panel shows.

    ``missing`` is the load-bearing field: it is what makes "the teacher model has no
    price, so every teacher line reads $0" visible instead of invisible.
    """
    required = list(dict.fromkeys(str(s) for s in required_skus or []))
    missing = [s for s in required if card.get(s) is None]
    by_source: dict[str, int] = {}
    for e in card.rates.values():
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1
    ages = [e.get("as_of") for e in card.rates.values() if e.get("as_of")]
    return {
        "n_rates": len(card.rates), "by_source": by_source,
        "required": required, "missing": missing, "n_missing": len(missing),
        "stale": card.stale_skus(now=now), "oldest_as_of": min(ages) if ages else None,
        "healthy": not missing and not card.stale_skus(now=now),
    }


def required_skus_for(plan: dict) -> list[str]:
    """Every SKU a plan will need priced — the input to ``rate_card_health``."""
    plan = dict(plan or {})
    teacher = str(plan.get("teacher_model") or (plan.get("models") or {}).get("teacher")
                  or "us.deepseek.r1-v1:0")
    harness_model = str(plan.get("harness_model", "global.anthropic.claude-fable-5"))
    return [
        sku_training(str(plan.get("training_instance", "ml.g5.2xlarge"))),
        sku_inference(str(plan.get("inference_instance", "ml.g5.xlarge"))),
        sku_tokens(teacher, "input"), sku_tokens(teacher, "output"),
        SKU_AGENTCORE_VCPU, SKU_AGENTCORE_GB,
        sku_tokens(harness_model, "input"), sku_tokens(harness_model, "output"),
        SKU_AGENTCORE_MEMORY,
    ]
