"""Unit tests for the FinOps cost model — pure functions, no AWS, no torch.

Most of these tests exist to pin a HAZARD rather than a happy path. Each hazard was
measured on the live account on 2026-07-31 before this module was written (see
docs/COST.md), and the comment on each test names the failure it prevents. A cost
number is uniquely dangerous among the values this repo computes: it is quoted to a
human who then approves real spend, so "quietly wrong" is the failure mode to design
against, not "crashes".

Run: .venv/bin/python -m pytest tests/test_cost_model.py -q
"""
from __future__ import annotations

import datetime
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline.contracts.cost_model import (  # noqa: E402
    CATEGORIES, DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD, DEFAULT_SINGLE_RUN_LIMIT_USD,
    MEASURED_INPUT_TOKENS_PER_SAMPLE, MEASURED_NONREASONING_OUTPUT_TOKENS,
    MEASURED_REASONING_MULTIPLIER, MEASURED_REASONING_OUTPUT_TOKENS,
    MEASURED_ROWS_PER_SEC, MEASURED_SETUP_OVERHEAD_S, PROJECT_RESOURCE_PATTERNS,
    RATE_PRECEDENCE,
    REMEDIABLE_CATEGORIES, SKU_AGENTCORE_GB, SKU_AGENTCORE_MEMORY, SKU_AGENTCORE_VCPU,
    RateCard, approval_decision, attribute_actuals, can_launch, check_approval,
    cross_check_tagged_total, estimate_run, is_project_resource, merge_rates,
    rate_card_health, realized_rates, reconcile, required_skus_for,
    run_id_from_resource, settlement_for, sku_inference, sku_tokens, sku_training,
)

NOW = "2026-07-31T00:00:00Z"

# The rate card as it really is on this account: SageMaker from the Price List API,
# tokens from realized billing (the Price List API lists no Fable 5 or Opus 5 — the
# harnesses' own models; verified live, which is exactly why realized rates outrank it).
FULL_RATES = {
    sku_training("ml.g5.2xlarge"): {"unit_price": 1.515, "unit": "hours",
                                    "source": "price_list", "as_of": NOW},
    sku_inference("ml.g5.xlarge"): {"unit_price": 1.006, "unit": "hours",
                                    "source": "price_list", "as_of": NOW},
    sku_tokens("us.deepseek.r1-v1:0", "input"): {"unit_price": 0.00135, "unit": "1K tokens",
                                                 "source": "ce_realized", "as_of": NOW},
    sku_tokens("us.deepseek.r1-v1:0", "output"): {"unit_price": 0.0054, "unit": "1K tokens",
                                                  "source": "ce_realized", "as_of": NOW},
    sku_tokens("global.anthropic.claude-fable-5", "input"): {
        "unit_price": 0.010, "unit": "1K tokens", "source": "ce_realized", "as_of": NOW},
    sku_tokens("global.anthropic.claude-fable-5", "output"): {
        "unit_price": 0.050, "unit": "1K tokens", "source": "ce_realized", "as_of": NOW},
    SKU_AGENTCORE_VCPU: {"unit_price": 0.0895, "unit": "vCPU-hours",
                         "source": "ce_realized", "as_of": NOW},
    SKU_AGENTCORE_GB: {"unit_price": 0.00945, "unit": "GB-hours",
                       "source": "ce_realized", "as_of": NOW},
    SKU_AGENTCORE_MEMORY: {"unit_price": 0.00025, "unit": "events",
                           "source": "ce_realized", "as_of": NOW},
}

PLAN = {"sample_count": 2000, "train_rows": 20000, "keep_reasoning": True,
        "max_iterations": 3, "training_instance": "ml.g5.2xlarge",
        "inference_instance": "ml.g5.xlarge", "endpoint_hours": 2.0, "n_stages": 9}


@pytest.fixture
def rates():
    return RateCard(FULL_RATES)


# ── the ground truth ──────────────────────────────────────────────────────────
def test_estimator_reproduces_the_measured_1077_run(rates):
    """The one run whose actual cost is KNOWN must come out right.

    The 2026-07-31 QLoRA run billed $10.77 for 16,550 rows on ml.g5.2xlarge. An
    estimator that cannot reproduce a run we already paid for has no business gating
    a $2000 one. (This pins the arithmetic chain rows -> seconds -> hours -> dollars;
    the throughput constant itself came from this run, so it is not independent
    evidence of the throughput — only of the model around it.)
    """
    est = estimate_run({"train_rows": 16_550, "sample_count": 0, "endpoint_hours": 0,
                        "n_stages": 0, "support_usd": 0, "max_iterations": 1}, rates)
    assert est["total_usd"] == pytest.approx(10.77, abs=0.05)
    assert abs(est["total_usd"] - 10.77) / 10.77 < 0.20  # the >20% bug threshold


def test_measured_calibration_constants_match_the_documented_run():
    """Guards both calibration constants against a silent edit.

    The overhead term is the one that would go unnoticed: at 16,550 rows it is 2.7% of
    the bill, so dropping it still looks roughly right, but on a 500-row smoke run it
    is most of the cost and the estimate would be ~3x low.
    """
    assert MEASURED_ROWS_PER_SEC == pytest.approx(0.664, abs=0.001)
    assert MEASURED_SETUP_OVERHEAD_S == pytest.approx(670.0, abs=1.0)


def test_fixed_overhead_dominates_a_small_run(rates):
    """A 100-row run costs far more than 100/16550 of a 16,550-row run — the estimate
    must not be linear in rows, or every smoke test looks free."""
    small = estimate_run({"train_rows": 100, "sample_count": 0, "endpoint_hours": 0,
                          "n_stages": 0, "support_usd": 0, "max_iterations": 1}, rates)
    assert small["total_usd"] > 10.77 * (100 / 16_550) * 3


# ── line items and totals ─────────────────────────────────────────────────────
def test_subtotals_sum_to_total(rates):
    est = estimate_run(PLAN, rates)
    assert sum(est["subtotals"].values()) == pytest.approx(est["total_usd"], abs=1e-3)


def test_every_line_item_carries_a_basis_and_a_rate_source(rates):
    """A number with no formula behind it cannot be argued with, only believed."""
    est = estimate_run(PLAN, rates)
    assert est["line_items"]
    for it in est["line_items"]:
        assert it["basis"], it
        assert it["rate_source"] in RATE_PRECEDENCE, it
        assert it["category"] in CATEGORIES, it


def test_all_expected_categories_are_present(rates):
    est = estimate_run(dict(PLAN, evaluation_input_tokens=1_000_000), rates)
    got = set(est["subtotals"])
    assert {"sagemaker_training", "sagemaker_inference", "bedrock_teacher",
            "agentcore_runtime", "agentcore_model", "agentcore_memory",
            "support"} <= got


# ── unpriced SKUs: the silent-$0 hazard ───────────────────────────────────────
def test_unknown_sku_is_listed_not_silently_zeroed():
    """The measured hazard: the Price List API has no Fable 5 or Opus 5 price, and any
    rate feed can go stale for a SKU it once carried.

    A rate card that answers 0.0 for an unknown SKU prices the teacher at nothing and
    hands back a confident, badly-low total. The line must still appear (so the
    operator sees it) with cost_usd None (so it cannot be added up).
    """
    partial = RateCard({sku_training("ml.g5.2xlarge"): FULL_RATES[sku_training("ml.g5.2xlarge")]})
    est = estimate_run(PLAN, partial)
    teacher_in = sku_tokens("us.deepseek.r1-v1:0", "input")
    assert teacher_in in est["unpriced"]
    line = next(i for i in est["line_items"] if i["sku"] == teacher_in)
    assert line["cost_usd"] is None
    assert line["unit_price"] is None
    assert line["quantity"] > 0          # the usage is known even though the price is not


def test_unpriced_forces_confidence_to_guessed_and_says_the_total_is_a_floor():
    partial = RateCard({sku_training("ml.g5.2xlarge"): FULL_RATES[sku_training("ml.g5.2xlarge")]})
    est = estimate_run(PLAN, partial)
    assert est["confidence"] == "guessed"
    assert any("FLOOR" in a for a in est["assumptions"])


def test_ratecard_get_returns_none_for_unknown_sku_never_zero():
    assert RateCard(FULL_RATES).get("sagemaker:training:ml.p5.48xlarge") is None
    assert RateCard(FULL_RATES).price("nope") is None


def test_confidence_is_the_weakest_source_in_play():
    """One guessed rate on any line makes the whole total a guess — averaging
    provenance would let a measured rate launder a fabricated one."""
    mixed = dict(FULL_RATES)
    mixed[sku_training("ml.g5.2xlarge")] = {"unit_price": 1.515, "unit": "hours",
                                            "source": "fallback_static", "as_of": NOW}
    est = estimate_run(PLAN, RateCard(mixed))
    assert est["confidence"] == "guessed"


# ── worst case vs expected: the remediation-loop hazard ───────────────────────
def test_worst_case_reflects_max_iterations(rates):
    """The state machine can re-run finetune up to max_iterations (default 3).
    An estimate that ignores it understates the ceiling ~3x on the priciest line."""
    one = estimate_run(dict(PLAN, max_iterations=1), rates)
    three = estimate_run(dict(PLAN, max_iterations=3), rates)
    assert one["worst_case_usd"] == pytest.approx(one["total_usd"])
    assert three["total_usd"] == pytest.approx(one["total_usd"])   # expected is unchanged
    assert three["worst_case_usd"] > three["total_usd"]
    # abs=0.01: the three reported fields are each rounded independently for display,
    # so the identity holds to cents, not to floating-point equality.
    assert three["worst_case_usd"] == pytest.approx(
        three["total_usd"] + 2 * three["remediable_usd"], abs=0.01)


def test_remediation_multiplies_only_the_rerunnable_categories(rates):
    """Applying max_iterations to the teacher line would OVERSTATE as badly as
    ignoring it understates: remediation re-trains, it does not regenerate the
    dataset the teacher already produced."""
    est = estimate_run(PLAN, rates)
    remediable = {i["category"] for i in est["line_items"] if i["remediable"]}
    assert remediable <= set(REMEDIABLE_CATEGORIES)
    assert "bedrock_teacher" not in remediable
    assert "sagemaker_training" in remediable


def test_gate_reads_worst_case_not_expected(rates):
    """Approving $2000 that can silently become $6000 is not a gate."""
    est = estimate_run(PLAN, rates)
    est["total_usd"], est["worst_case_usd"] = 1500.0, 4500.0
    d = approval_decision(est)
    assert d["approval_required"] is True
    assert d["gating_basis"] == "worst_case_usd"
    assert d["gating_usd"] == 4500.0


# ── keep_reasoning: the invisible-parameter hazard ────────────────────────────
def test_keep_reasoning_raises_the_teacher_output_line(rates):
    """keep_reasoning=True keeps R1's <think> chains, which for ARC ARE the target.
    Today that flag's cost impact is invisible; it must not be."""
    on = estimate_run(dict(PLAN, keep_reasoning=True), rates)
    off = estimate_run(dict(PLAN, keep_reasoning=False), rates)
    assert on["subtotals"]["bedrock_teacher"] > off["subtotals"]["bedrock_teacher"]
    assert any("keep_reasoning=True" in i["basis"] for i in on["line_items"])


def test_teacher_token_calibration_matches_the_measured_run():
    """The 4.7x underestimate that the whole suite used to be blind to.

    Before this guard the defaults were 700 output tokens x reasoning_multiplier 4.0 =
    2,800/sample, and 393 tests passed anyway -- nothing anywhere pinned the largest
    quantity in a distillation estimate. Three independent checkpoints of
    run-20260731T183103Z-8b864805, all within 3% of each other:

        24 attempts ->   323,226 out tokens = 13,468/attempt
        36 attempts ->   471,496 out tokens = 13,097/attempt
        70 attempts ->   922,088 out tokens = 13,173/attempt

    The band below is the measurement, not a tolerance chosen to fit: 12,000-14,500
    contains all three and excludes both 2,800 and any drift back toward it.
    """
    assert MEASURED_REASONING_OUTPUT_TOKENS == pytest.approx(13_125, abs=1)
    # The product is what the estimator actually multiplies, so pin the product too --
    # a base/multiplier pair that silently cancels out would otherwise pass.
    product = MEASURED_NONREASONING_OUTPUT_TOKENS * MEASURED_REASONING_MULTIPLIER
    assert product == pytest.approx(MEASURED_REASONING_OUTPUT_TOKENS, abs=1)
    assert 12_000 <= product <= 14_500, "outside the measured 13.1-13.5k band"
    assert MEASURED_INPUT_TOKENS_PER_SAMPLE == pytest.approx(2_014, abs=1)
    # 922,088 / 70 is the strongest single checkpoint (largest n): stay within 10% of it.
    assert abs(product - 922_088 / 70) / (922_088 / 70) < 0.10


def test_the_estimate_would_have_caught_the_infeasible_teacher_cap(rates):
    """The failure this recalibration exists to prevent, priced end to end.

    run-20260731T183103Z-8b864805's plan promised 160 samples AND capped the teacher
    line at $7.56 -- a cap computed from 2,800 tokens/sample. At the measured rate 160
    samples cost ~$11.78, so the two halves of the same plan contradicted each other and
    the run stopped at 70 samples (44%) having done nothing wrong. Under the old defaults
    the estimate came out at $2.85, i.e. it AGREED with the infeasible cap.
    """
    est = estimate_run({"sample_count": 160, "train_rows": 0, "endpoint_hours": 0,
                        "n_stages": 0, "support_usd": 0, "max_iterations": 1,
                        "keep_reasoning": True}, rates)
    teacher = est["subtotals"]["bedrock_teacher"]
    assert teacher > 7.56, (
        f"a ${teacher:.2f} estimate still fits under the cap that killed the run at 44% "
        "-- the estimator is back to blessing an infeasible plan")
    assert teacher == pytest.approx(11.78, rel=0.05)


def test_an_estimate_says_where_its_largest_number_came_from(rates):
    """Provenance, and only when it is earned.

    The teacher output line is the biggest quantity in a distillation estimate, so it
    must name its source. But if the PLAN overrides the tokens, the estimate must say
    THAT instead -- attributing a caller's guess to our measurement would lend it
    credibility it has not got, which is the same class of bug as the hover card's
    concatenated runtime name.
    """
    est = estimate_run(dict(PLAN, keep_reasoning=True), rates)
    assert any("run-20260731T183103Z-8b864805" in a for a in est["assumptions"]), \
        "the measured teacher tokens carry no provenance"
    over = estimate_run(dict(PLAN, teacher_output_tokens_per_sample=700,
                             reasoning_multiplier=4.0), rates)
    assert any("OVERRIDDEN by the plan" in a for a in over["assumptions"]), \
        "a plan override is being passed off as our measurement"
    assert not any("run-20260731T183103Z-8b864805" in a for a in over["assumptions"])


def test_teardown_off_is_stated_as_an_assumption(rates):
    """An endpoint left up is the largest cost risk in the repo; the estimate must
    show what it assumed rather than quietly covering only the named hours."""
    est = estimate_run(dict(PLAN, teardown=False), rates)
    assert any("TEARDOWN DISABLED" in a for a in est["assumptions"])


# ── dual threshold ────────────────────────────────────────────────────────────
def test_single_run_over_limit_requires_approval():
    d = approval_decision({"worst_case_usd": 2500.0}, project_to_date_usd=0.0)
    assert d["approval_required"] is True
    assert d["status"] == "pending_approval"
    assert any("single-run" in r for r in d["reasons"])


def test_under_both_limits_needs_no_approval():
    d = approval_decision({"worst_case_usd": 50.0}, project_to_date_usd=100.0)
    assert d["approval_required"] is False
    assert d["status"] == "approved"
    assert d["reasons"] == []


def test_cumulative_only_trip_still_requires_approval():
    """The drip case: twenty $150 runs each sail under a single-run limit while the
    project quietly passes $2000. A single-threshold gate never fires here."""
    d = approval_decision({"worst_case_usd": 150.0}, project_to_date_usd=1950.0)
    assert d["approval_required"] is True
    assert len(d["reasons"]) == 1
    assert "project to-date" in d["reasons"][0]


def test_both_thresholds_can_trip_together():
    d = approval_decision({"worst_case_usd": 5000.0}, project_to_date_usd=9000.0)
    assert len(d["reasons"]) == 2


def test_default_limits_are_the_2000_dollars_asked_for():
    assert DEFAULT_SINGLE_RUN_LIMIT_USD == 2000.0
    assert DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD == 2000.0


def test_limits_are_configurable():
    d = approval_decision({"worst_case_usd": 300.0}, single_run_limit_usd=100.0,
                          cumulative_limit_usd=1e9)
    assert d["approval_required"] is True


def test_approval_falls_back_to_total_when_worst_case_absent():
    d = approval_decision({"total_usd": 2500.0})
    assert d["approval_required"] is True


# ── separation of duties ──────────────────────────────────────────────────────
PENDING = {"id": "est-1", "status": "pending_approval", "requested_by": "alice"}


def test_self_approval_is_rejected_not_merely_flagged():
    """Strict SoD, per the product decision: a $2000 gate the requester can clear
    themselves is decoration."""
    r = check_approval(PENDING, "alice", ["admin", "llmops-approver"])
    assert r["allowed"] is False
    assert r["code"] == 403
    assert "separation of duties" in r["error"]


def test_non_approver_group_is_403():
    r = check_approval(PENDING, "bob", ["admin"])
    assert r["allowed"] is False
    assert r["code"] == 403
    assert "llmops-approver" in r["error"]


def test_second_approver_in_the_group_is_allowed():
    r = check_approval(PENDING, "bob", ["admin", "llmops-approver"])
    assert r["allowed"] is True


def test_approving_something_not_pending_is_a_conflict():
    r = check_approval({"status": "approved", "requested_by": "alice"}, "bob",
                       ["llmops-approver"])
    assert r["allowed"] is False
    assert r["code"] == 409


def test_only_approved_estimates_can_launch():
    assert can_launch({"status": "approved"})["ok"] is True
    for bad in ("draft", "pending_approval", "rejected", "launched", ""):
        out = can_launch({"status": bad})
        assert out["ok"] is False, bad
        assert out["code"] == 409


# ── attribution: the contamination hazard ─────────────────────────────────────
def test_foreign_resources_are_excluded_from_the_project_rollup():
    """Measured on this account: SageMaker Canvas ($296) and a JumpStart Whisper
    endpoint ($18/day) share the account. A service-level rollup would bill both to
    this project."""
    groups = [
        {"resource_id": "training-job/llmops-qlora-run-phase2-main-0001-r3", "cost_usd": 10.77},
        {"resource_id": "endpoint/jumpstart-dft-hf-asr-whisper-large-v2", "cost_usd": 18.18},
        {"resource_id": "Canvas:Session-Hrs", "cost_usd": 296.0},
    ]
    out = attribute_actuals(groups, "llmops-agentic-system", "2026-07-30")
    assert out["total_usd"] == pytest.approx(10.77)
    assert out["excluded_usd"] == pytest.approx(314.18)
    assert len(out["excluded"]) == 2


def test_attribution_is_an_allowlist_so_unknown_shapes_are_excluded():
    assert is_project_resource("training-job/llmops-qlora-x") is True
    assert is_project_resource("training-job/someone-elses-job") is False
    assert is_project_resource("") is False
    assert all(is_project_resource(f"{p}x") for p in PROJECT_RESOURCE_PATTERNS)


def test_run_id_is_recovered_from_the_job_name_without_any_tag():
    """Per-run attribution needs no cost-allocation tags: run_id is already in the
    job name. This matters because the tag is Inactive on this account."""
    assert run_id_from_resource(
        "training-job/llmops-qlora-run-phase2-main-0001-r3") == "run-phase2-main"
    assert run_id_from_resource("training-job/not-ours-123") is None


def test_a_short_run_id_is_not_eaten_by_suffix_stripping():
    """Found by this test, not by review: the first extractor stripped trailing short
    tokens in a loop, so `llmops-student-run-a` yielded `run` and that endpoint's cost
    was attributed to a run_id that does not exist. Exactly two optional suffixes are
    stripped — pool tag, then sequence — and never the last remaining token."""
    assert run_id_from_resource("endpoint/llmops-student-run-a") == "run-a"
    assert run_id_from_resource("training-job/llmops-qlora-run-a-0001-e3") == "run-a"
    assert run_id_from_resource("endpoint/llmops-student-e3") == "e3"


def test_actual_rows_key_by_period_run_and_category():
    out = attribute_actuals(
        [{"resource_id": "training-job/llmops-qlora-run-a-0001-e3", "cost_usd": 1.0},
         {"resource_id": "endpoint/llmops-student-run-a", "cost_usd": 2.0}],
        "llmops-agentic-system", "2026-07-30")
    sks = sorted(r["sk"] for r in out["rows"])
    assert sks == ["2026-07-30#run-a#sagemaker_inference",
                   "2026-07-30#run-a#sagemaker_training"]


# ── Cost Explorer lag ─────────────────────────────────────────────────────────
def test_estimated_period_settles_as_provisional_never_settled():
    """CE lags ~24 h and flags recent periods Estimated. Publishing one as settled is
    how a number that will still move gets quoted as final."""
    assert settlement_for(True) == "provisional"
    assert settlement_for(False) == "settled"
    out = attribute_actuals([{"resource_id": "training-job/llmops-qlora-run-a-1-e3",
                              "cost_usd": 5.0}], "p", "2026-07-31", ce_estimated=True)
    assert out["settlement"] == "provisional"
    assert all(r["settlement"] == "provisional" for r in out["rows"])
    assert all(r["ce_estimated_flag"] is True for r in out["rows"])


def test_provisional_reconcile_says_so_in_the_verdict():
    r = reconcile({"total_usd": 10.0, "subtotals": {"sagemaker_training": 10.0}},
                  {"total_usd": 12.0, "subtotals": {"sagemaker_training": 12.0},
                   "settlement": "provisional"})
    assert "PROVISIONAL" in r["verdict"]


# ── the tag-filtered $0 hazard ────────────────────────────────────────────────
def test_inactive_tag_zero_does_not_become_a_zero_project_total():
    """Reproduced live: with the `project` tag Inactive, a tag-filtered CE query
    returned $0.00 for a day that really spent money. Reporting that $0 as the
    project total is the exact bug this function prevents."""
    out = cross_check_tagged_total(resource_total_usd=10.77, tagged_total_usd=0.0,
                                  tag_active=False)
    assert out["authoritative_total_usd"] == pytest.approx(10.77)
    assert out["source"] == "resource_level"
    assert out["tag_usable"] is False
    assert "Inactive" in out["note"]


def test_active_tag_that_agrees_is_reported_as_agreeing():
    out = cross_check_tagged_total(10.0, 10.2, tag_active=True)
    assert out["tag_usable"] is True and out["tags_agree"] is True


def test_active_tag_that_disagrees_says_the_tag_is_not_retroactive():
    out = cross_check_tagged_total(10.0, 2.0, tag_active=True)
    assert out["tags_agree"] is False
    assert "not" in out["note"] and "retroactive" in out["note"]


# ── reconciliation ────────────────────────────────────────────────────────────
def test_reconcile_names_the_driving_category_not_just_a_percentage():
    """One aggregate % says the estimate was wrong; it does not say what to fix."""
    r = reconcile(
        {"total_usd": 100.0, "subtotals": {"sagemaker_training": 80.0,
                                           "bedrock_teacher": 20.0}},
        {"total_usd": 160.0, "subtotals": {"sagemaker_training": 140.0,
                                           "bedrock_teacher": 20.0},
         "settlement": "settled"})
    assert r["driver"] == "sagemaker_training"
    assert "sagemaker_training" in r["verdict"]
    assert r["variance_pct"] == pytest.approx(60.0)
    assert r["accuracy_ratio"] == pytest.approx(1.6)


def test_reconcile_derives_subtotals_from_attributed_rows():
    actual = attribute_actuals(
        [{"resource_id": "training-job/llmops-qlora-run-a-0001-e3", "cost_usd": 12.0}],
        "p", "2026-07-30")
    r = reconcile({"total_usd": 10.0, "subtotals": {"sagemaker_training": 10.0}}, actual)
    assert r["actual_usd"] == pytest.approx(12.0)
    assert r["driver"] == "sagemaker_training"


def test_unestimated_spend_is_named_as_such_rather_than_dividing_by_zero():
    """Runs launched without an estimate stay legal, so the variance report has to be
    able to say honestly that no estimate existed."""
    r = reconcile({}, {"total_usd": 42.0, "subtotals": {"support": 42.0}})
    assert r["accuracy_ratio"] is None
    assert r["variance_pct"] is None
    assert "never estimated" in r["verdict"]


def test_reconcile_reports_a_category_the_estimate_missed_entirely():
    r = reconcile({"total_usd": 10.0, "subtotals": {"sagemaker_training": 10.0}},
                  {"total_usd": 30.0, "subtotals": {"sagemaker_training": 10.0,
                                                    "sagemaker_inference": 20.0},
                   "settlement": "settled"})
    row = next(x for x in r["per_category"] if x["category"] == "sagemaker_inference")
    assert row["estimate_usd"] == 0.0 and row["actual_usd"] == 20.0
    assert row["variance_pct"] is None      # no divide-by-zero percentage invented
    assert r["driver"] == "sagemaker_inference"


# ── rate precedence and realized rates ────────────────────────────────────────
def test_rate_precedence_prefers_realized_billing_over_price_list():
    """The Price List API cannot price the harness fleet's own models (Fable 5, Opus 5),
    so our own bill is the authority wherever both exist. Where both DO exist they agree
    to <0.001% (measured on 5 SKUs), so this precedence costs nothing when the feed is
    right and saves the estimate when it is not."""
    merged = merge_rates(
        {"sku:a": {"unit_price": 9.0, "source": "price_list", "as_of": NOW}},
        {"sku:a": {"unit_price": 1.0, "source": "ce_realized", "as_of": NOW}},
        {"sku:a": {"unit_price": 5.0, "source": "fallback_static", "as_of": NOW}})
    assert merged.price("sku:a") == 1.0
    assert merged.source("sku:a") == "ce_realized"


def test_precedence_order_is_realized_then_pricelist_then_static():
    assert RATE_PRECEDENCE == ("ce_realized", "price_list", "fallback_static")


def test_realized_rates_divide_cost_by_quantity():
    card = realized_rates([{"sku": "USE1-DeepSeek-R1-input-tokens",
                            "quantity": 1_000_000, "cost_usd": 1.35}], NOW,
                          unit_scale={"USE1-DeepSeek-R1-input-tokens": 1000})
    e = card.get("USE1-DeepSeek-R1-input-tokens")
    assert e["unit_price"] == pytest.approx(0.00135)   # the live-measured rate
    assert e["source"] == "ce_realized"


def test_zero_quantity_never_produces_a_zero_rate():
    """A $0/0 group would otherwise price a real SKU at nothing."""
    card = realized_rates([{"sku": "x", "quantity": 0, "cost_usd": 0.0}], NOW)
    assert card.get("x") is None


def test_unknown_source_is_treated_as_the_weakest_not_trusted():
    c = RateCard({"s": {"unit_price": 1.0, "source": "vibes"}})
    assert c.source("s") == "fallback_static"


# ── rate card health ──────────────────────────────────────────────────────────
def test_health_flags_a_missing_teacher_price_instead_of_hiding_it():
    """This is the panel that makes 'the teacher prices at $0' visible."""
    partial = RateCard({sku_training("ml.g5.2xlarge"): FULL_RATES[sku_training("ml.g5.2xlarge")]})
    h = rate_card_health(partial, required_skus_for(PLAN))
    assert h["healthy"] is False
    assert sku_tokens("us.deepseek.r1-v1:0", "output") in h["missing"]
    assert h["n_missing"] == len(h["missing"]) > 0


def test_health_is_healthy_when_every_required_sku_is_priced_and_fresh(rates):
    now = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    h = rate_card_health(rates, required_skus_for(PLAN), now=now)
    assert h["missing"] == [] and h["stale"] == []
    assert h["healthy"] is True


def test_stale_rates_are_reported_by_age():
    old = RateCard({"s": {"unit_price": 1.0, "source": "ce_realized",
                          "as_of": "2026-01-01T00:00:00Z"}})
    now = datetime.datetime(2026, 7, 31, tzinfo=datetime.timezone.utc)
    assert old.stale_skus(now=now) == ["s"]


def test_a_rate_with_no_as_of_counts_as_stale():
    assert RateCard({"s": {"unit_price": 1.0, "source": "ce_realized"}}).stale_skus() == ["s"]


def test_required_skus_follow_the_plans_models_and_instances():
    req = required_skus_for({"training_instance": "ml.p4d.24xlarge",
                             "teacher_model": "us.anthropic.kimi-k3"})
    assert sku_training("ml.p4d.24xlarge") in req
    assert sku_tokens("us.anthropic.kimi-k3", "output") in req


def test_pricing_refresh_without_token_rates_marks_them_missing_not_absent():
    """The measured Price List gap: refreshing from Price List alone must leave the
    token lines visibly unpriced rather than dropping them from the estimate. Named
    generically because WHICH model the feed omits changes between refreshes -- it
    omits Fable 5 and Opus 5 today and did price DeepSeek-R1 on 2026-07-31; the
    behaviour under test is what happens to any absent rate."""
    price_list_only = RateCard({
        sku_training("ml.g5.2xlarge"): FULL_RATES[sku_training("ml.g5.2xlarge")],
        sku_inference("ml.g5.xlarge"): FULL_RATES[sku_inference("ml.g5.xlarge")]})
    h = rate_card_health(price_list_only, required_skus_for(PLAN))
    assert sku_tokens("us.deepseek.r1-v1:0", "input") in h["missing"]
    est = estimate_run(PLAN, price_list_only)
    assert any(i["category"] == "bedrock_teacher" for i in est["line_items"])
