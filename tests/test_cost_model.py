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
import json
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline.contracts.cost_model import (  # noqa: E402
    BUDGET_MODES, CATEGORIES, DEFAULT_BUDGET_MODE,
    DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD, DEFAULT_SINGLE_RUN_LIMIT_USD,
    MEASURED_INPUT_TOKENS_PER_SAMPLE, MEASURED_NONREASONING_OUTPUT_TOKENS,
    MEASURED_REASONING_MULTIPLIER, MEASURED_REASONING_OUTPUT_TOKENS,
    MEASURED_ROWS_PER_SEC, MEASURED_SETUP_OVERHEAD_S, PROJECT_RESOURCE_PATTERNS,
    FALLBACK_AOSS_OCU_USD, RATE_PRECEDENCE,
    REMEDIABLE_CATEGORIES, SKU_AGENTCORE_GB, SKU_AGENTCORE_MEMORY, SKU_AGENTCORE_VCPU,
    SKU_AOSS_OCU, SKU_EMBED_MODEL,
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
    """Quoting $1500 for something that can silently become $4500 is the hazard, and
    it survives advisory mode: what changed is whether we BLOCK, not which number we
    compare. Reported against the expected total this run reads as under budget."""
    est = estimate_run(PLAN, rates)
    # Straddle the reference: expected under it, worst case over. Derived, because a
    # pair of literals stops straddling the moment the reference moves -- and then this
    # test passes while comparing two numbers that are both on the same side.
    under = DEFAULT_SINGLE_RUN_LIMIT_USD * 0.75
    est["total_usd"], est["worst_case_usd"] = under, under * 3
    assert est["total_usd"] < DEFAULT_SINGLE_RUN_LIMIT_USD < est["worst_case_usd"]
    d = approval_decision(est)
    assert d["gating_basis"] == "worst_case_usd"
    assert d["gating_usd"] == under * 3
    assert d["over_budget"], "the worst case is over the limit and must be reported"
    assert approval_decision(est, budget_mode="blocking")["approval_required"] is True


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
# The arithmetic below is unchanged by advisory mode and is the part worth pinning:
# WHICH comparisons fire, on which number, at which limits. Advisory mode changed only
# the consequence -- `over_budget` is populated exactly where `reasons` used to be, and
# `approval_required` is now gated on the mode. Each threshold is therefore asserted
# twice: reported in the default (advisory) mode, and blocking under budget_mode=
# "blocking". A single-mode assertion would let a regression that hard-codes one mode
# pass, which is how a budget silently stops being either enforced or mentioned.
def test_single_run_over_limit_is_reported_and_blocks_only_in_blocking_mode():
    over = DEFAULT_SINGLE_RUN_LIMIT_USD + 500.0
    d = approval_decision({"worst_case_usd": over}, project_to_date_usd=0.0)
    assert d["approval_required"] is False       # advisory: the run is not stopped
    assert d["status"] == "approved"
    assert any("single-run" in r for r in d["over_budget"])
    # Derived from the constant, not retyped: at $2,000 this line read `== 500.0` with
    # a `# 2500 - the 2000 limit` comment beside it, i.e. three copies of one number in
    # one assertion. Raising the limit would have left the comment lying and the assert
    # passing for the wrong reason.
    assert d["over_budget_usd"] == 500.0
    assert d["notes"], "advisory must still SAY it is over; silence is not a reference"

    b = approval_decision({"worst_case_usd": over}, project_to_date_usd=0.0,
                          budget_mode="blocking")
    assert b["approval_required"] is True
    assert b["status"] == "pending_approval"
    assert any("single-run" in r for r in b["reasons"])


def test_under_both_limits_needs_no_approval():
    d = approval_decision({"worst_case_usd": 50.0}, project_to_date_usd=100.0)
    assert d["approval_required"] is False
    assert d["status"] == "approved"
    assert d["reasons"] == []
    assert d["over_budget"] == []
    assert d["over_budget_usd"] == 0.0
    assert d["notes"] == []
    # Under the limit is under the limit in either mode -- blocking mode must not
    # invent an overage, or turning enforcement on would stop every run.
    assert approval_decision({"worst_case_usd": 50.0}, project_to_date_usd=100.0,
                             budget_mode="blocking")["approval_required"] is False


def test_cumulative_only_trip_is_still_detected():
    """The drip case: twenty $150 runs each sail under a single-run limit while the
    project quietly passes $2000. A single-threshold check never sees it -- and the
    cumulative arm is the one advisory mode could most easily have dropped, since
    per-run it always looks fine."""
    # Just under the single-run reference on its own, over it once added to to-date:
    # that gap is the whole point of the second arm, so it is derived from the limit
    # rather than being two literals that happen to sit either side of it.
    to_date = DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD - 50.0
    d = approval_decision({"worst_case_usd": 150.0}, project_to_date_usd=to_date)
    assert len(d["over_budget"]) == 1
    assert "project to-date" in d["over_budget"][0]
    # Under the single-run limit, so there is no single-run overage to name...
    assert d["over_budget_usd"] == 0.0
    # ...but the run is still over budget, and must not read as clean.
    assert d["notes"]
    assert approval_decision({"worst_case_usd": 150.0}, project_to_date_usd=to_date,
                             budget_mode="blocking")["approval_required"] is True


def test_both_thresholds_can_trip_together():
    over = DEFAULT_SINGLE_RUN_LIMIT_USD * 2.5      # over on its own...
    to_date = DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD * 4.5   # ...and over cumulatively
    d = approval_decision({"worst_case_usd": over}, project_to_date_usd=to_date)
    assert len(d["over_budget"]) == 2
    assert len(approval_decision({"worst_case_usd": over}, project_to_date_usd=to_date,
                                 budget_mode="blocking")["reasons"]) == 2


def test_default_limits_are_the_20000_dollars_asked_for():
    """Raised from $2,000 on 2026-08-02: this is the project's own design-and-test
    platform, not a customer's production account, and a reference low enough to be
    crossed by ordinary work is a reference that gets ignored.

    Asserted on the constants with the number in the test NAME as well as the body,
    because that is what makes a silent change impossible to land: a diff that edits the
    constant and the assert together still leaves a test called
    `..._are_the_20000_dollars_asked_for` checking 5,000, which does not read as fine.
    """
    assert DEFAULT_SINGLE_RUN_LIMIT_USD == 20_000.0
    assert DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD == 20_000.0
    # Both arms move together or the dual reference stops being dual: a low cumulative
    # against a high single-run one turns every second run into an overage report.
    assert DEFAULT_SINGLE_RUN_LIMIT_USD == DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD


def test_the_budget_is_a_reference_by_default_not_a_ceiling():
    """The product decision: this platform's owner is its only approver, so a gate
    here could only ever ask them to approve their own run. Advisory is therefore the
    DEFAULT, and it is asserted on the constant rather than inferred from behaviour --
    a flipped default is a change in what the platform does to every run, and it
    should have to break a test to happen."""
    assert DEFAULT_BUDGET_MODE == "advisory"
    assert set(BUDGET_MODES) == {"advisory", "blocking"}
    assert approval_decision({"worst_case_usd": 1e9})["budget_mode"] == "advisory"


def test_an_unrecognised_budget_mode_falls_back_to_advisory_not_to_blocking():
    """A typo'd env var must not silently become enforcement. The failure mode of
    guessing 'blocking' is every run stopping on a value nobody can approve; the
    failure mode of guessing 'advisory' is the documented default."""
    # Over the reference, derived. As a literal 2500.0 this cleared a $2,000 reference
    # and stopped clearing a $20,000 one -- so raising the limit turned the second half
    # of this test ("the overage is still reported") into an assertion about a run that
    # was never over budget at all. The straddle IS the test; it cannot be a constant.
    over = DEFAULT_SINGLE_RUN_LIMIT_USD + 500.0
    d = approval_decision({"worst_case_usd": over}, budget_mode="Blocking!")
    assert d["budget_mode"] == "advisory"
    assert d["approval_required"] is False
    assert d["over_budget"], "the overage is still reported under a bad mode"
    for bogus in (None, "", "off", "enforce", "yes"):
        assert approval_decision({"worst_case_usd": over},
                                 budget_mode=bogus)["approval_required"] is False


def test_limits_are_configurable():
    d = approval_decision({"worst_case_usd": 300.0}, single_run_limit_usd=100.0,
                          cumulative_limit_usd=1e9)
    assert d["over_budget"]
    assert d["over_budget_usd"] == 200.0
    assert d["single_run_limit_usd"] == 100.0
    assert approval_decision({"worst_case_usd": 300.0}, single_run_limit_usd=100.0,
                             cumulative_limit_usd=1e9,
                             budget_mode="blocking")["approval_required"] is True


def test_approval_falls_back_to_total_when_worst_case_absent():
    over = DEFAULT_SINGLE_RUN_LIMIT_USD + 500.0
    d = approval_decision({"total_usd": over})
    assert d["gating_usd"] == over
    assert d["over_budget"]
    assert approval_decision({"total_usd": over},
                             budget_mode="blocking")["approval_required"] is True


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
    endpoint ($36.36/day) share the account. A service-level rollup would bill both to
    this project."""
    groups = [
        {"resource_id": "training-job/llmops-qlora-run-phase2-main-0001-r3", "cost_usd": 10.77},
        {"resource_id": "endpoint/jumpstart-dft-hf-asr-whisper-large-v2", "cost_usd": 36.36},
        {"resource_id": "Canvas:Session-Hrs", "cost_usd": 296.0},
    ]
    out = attribute_actuals(groups, "llmops-agentic-system", "2026-07-30")
    assert out["total_usd"] == pytest.approx(10.77)
    assert out["excluded_usd"] == pytest.approx(332.36)
    assert len(out["excluded"]) == 2


def test_the_whisper_orphans_daily_figure_matches_its_instance_and_hourly_rate():
    """The orphan's daily cost is DERIVED here, not restated.

    Every prose mention of this endpoint said $18/day for as long as it existed. That
    number came from the first monitor sweep, which had to guess: the sweep's own report
    said "sagemaker:DescribeEndpoint denied ... cost figures are estimates based on
    JumpStart defaults". describe_endpoint_config later returned ml.g5.2xlarge x1, and
    Cost Explorer billed $36.36 on each of seven consecutive days -- exactly 24 x the
    $1.515/hr this module already documents for that instance. So the figure that six
    files repeated was half the real one, and a cost control whose headline number is
    an underestimate is one an owner can correctly dismiss.

    Asserting the arithmetic rather than the string is what makes this a guard: if
    someone re-copies a stale figure, or the documented hourly rate moves, the two sides
    stop agreeing. The FinOps rule is the same one the sweep broke -- an assumed number
    must be labelled as one, and a measured number must match what it is derived from.
    """
    hourly = 1.515          # ml.g5.2xlarge, Price List us-east-1; see this module's header
    daily = hourly * 24
    assert daily == pytest.approx(36.36), (
        f"ml.g5.2xlarge at ${hourly}/hr is ${daily:.2f}/day")

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "pipeline" / "contracts" / "cost_model.py").read_text()
    assert f"${daily:.2f}/day" in src, (
        "cost_model.py states a daily figure for the Whisper orphan that its own "
        "documented hourly rate does not produce")
    assert "$18/day" not in src, "the falsified $18/day figure is back in cost_model.py"

    # Every markdown file, in both unit spellings. The first version of this guard listed
    # two English files, so the zh-TW twin kept the falsified figure for as long as the
    # guard existed: `$18/天` is not `$18/day`, and a per-file allowlist is satisfied by
    # the files it happens to name. A falsified figure is falsified in every language.
    repo = pathlib.Path(__file__).resolve().parent.parent
    for doc in sorted(repo.rglob("*.md")):
        if ".venv" in doc.parts or "node_modules" in doc.parts:
            continue
        text = doc.read_text()
        for stale in ("$18/day", "$18/天"):
            assert stale not in text, (
                f"{doc.relative_to(repo)} still carries the falsified {stale} figure; the "
                f"orphan billed ${daily:.2f}/day")


def test_no_harness_prompt_states_the_falsified_orphan_rate():
    """The agents' own prompts are a doc surface, and this guard did not cover them.

    The sweep that corrected the $18/day figure edited `cost_model.py`, `docs/COST.md` and
    `CHANGELOG.md`, and the guard above was anchored to exactly those three -- so
    `agents/finops/harness.json` kept telling the auditor, in the very rule about not
    publishing assumed numbers as measured ones, that the orphan cost ~$18/day. A
    falsified figure inside a system prompt is worse than one in a doc: nobody reads the
    doc mid-audit, and the agent reads the prompt on every single invocation.

    So this scans EVERY harness config rather than naming the one that was wrong. Naming
    the file is how the first guard came to have a hole -- the eighth agent's prompt would
    inherit the same blind spot.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    checked = sorted((root / "agents").glob("*/harness.json"))
    assert len(checked) >= 7, f"expected the whole fleet, found {len(checked)}"
    for cfg in checked:
        text = cfg.read_text()
        assert "$18/day" not in text, (
            f"{cfg.relative_to(root)} states the falsified $18/day orphan rate; the "
            "measured figure is $36.36/day (ml.g5.2xlarge x1 at $1.515/hr x 24)")
    # The correction has to be PRESENT somewhere in the fleet, not merely absent: deleting
    # the sentence would also pass an absence-only check, and the excluded-spend example is
    # what makes the attribute-by-resource rule concrete for the agent that applies it.
    finops = (root / "agents" / "finops" / "harness.json").read_text()
    assert "$36.36/day" in finops, (
        "the finops prompt no longer states the orphan's measured daily rate at all; the "
        "attribution rule lost the example that makes it concrete")


def test_the_orphans_monthly_figure_is_derived_and_the_budget_filter_is_stated_both_ways():
    """The account budget's CostFilters decide whether it can see this orphan at all.

    `describe_budgets` (2026-08-02) returns `{"Service": ["Amazon Bedrock"]}` for
    `bedrock-monthly-dev`, and that single field cuts both ways, so COST.md has to say
    both halves or it misleads whichever reader it leaves out:

      * filtered means the orphan's ~$1106/month does NOT eat our $1000 Bedrock headroom.
        Unfiltered, the guardrail would have sat permanently over 100% on somebody else's
        endpoint, and the first real Bedrock spend would have tripped an alarm about
        something it had nothing to do with.
      * filtered ALSO means no account-level control would ever have flagged that
        endpoint. A budget scoped to one service is blind to waste in another. What found
        it was the whole-account monitor sweep. The two controls are not redundant.

    The monthly number is derived here for the same reason the daily one is: $1106 was
    typed as $1107 on the first pass, and a figure nobody recomputes is a figure that
    drifts. 730 h is the AWS convention for a month, not 30 x 24.
    """
    hourly = 1.515
    monthly = hourly * 730
    assert monthly == pytest.approx(1105.95, abs=0.01), (
        f"ml.g5.2xlarge at ${hourly}/hr over 730h is ${monthly:.2f}/month")
    stated = f"${round(monthly):,}".replace(",", "")   # ~$1106

    repo = pathlib.Path(__file__).resolve().parent.parent
    for doc in ("docs/COST.md", "docs/COST.zh-TW.md"):
        text = (repo / doc).read_text()
        # Anchor to the budget passage. Searching the whole file would let a stray
        # "Amazon Bedrock" or "sweep" anywhere in a 370-line doc satisfy a guard about
        # THIS paragraph -- the same way a repeated phrase elsewhere once silently
        # disarmed test_every_schedule_the_deployer_creates_is_named_in_the_cost_posture.
        assert "bedrock-monthly-dev" in text, f"{doc} no longer names the account budget"
        para = text.partition("bedrock-monthly-dev")[2][:1600]

        assert stated in para, (
            f"{doc} does not state the orphan's monthly cost as {stated} alongside the "
            f"budget, which is what ${hourly}/hr x 730h comes to")
        assert "Amazon Bedrock" in para, (
            f"{doc} does not say what bedrock-monthly-dev is filtered to; an unqualified "
            f"$1000 guardrail reads as covering all account spend, which it does not")
        # Both halves of the consequence, or the sentence misleads by omission.
        assert "sweep" in para, (
            f"{doc} states the budget filter without saying which control DOES cover "
            f"non-Bedrock spend -- a reader is left thinking nothing does")


def test_the_orphans_idle_lifetime_is_derived_from_its_own_two_dates():
    """How long it idled is arithmetic on two recorded dates, not a number to retype.

    It was retyped, and it was wrong: the snapshot and the CHANGELOG both said "838 days"
    while the IAM comment said 842, and the truth is 843 -- 2024-04-11 creation to
    2026-08-02 deletion. Three files, three numbers, no disagreement any of them could
    surface, because each was a standalone digit with nothing to check it against.

    That is the same shape as the $18/day figure two tests above: a measurement copied
    into prose stops being a measurement. The difference here is that the endpoint is now
    deleted, so the interval is FIXED forever -- which makes a wrong value permanent
    rather than merely stale, and makes deriving it strictly better than restating it.
    """
    snap = json.loads(
        (REPO / "deploy" / "evidence" / "whisper_endpoint_snapshot.json").read_text())
    created = datetime.date.fromisoformat(snap["created"][:10])
    assert created == datetime.date(2024, 4, 11), (
        f"the snapshot's creation date moved to {created}; every day count below is "
        "measured from it")

    # The deletion date is stated in prose, in the field that records the deletion.
    deleted_at = re.search(r"DELETED (\d{4}-\d{2}-\d{2})", snap["status"])
    assert deleted_at, "the snapshot's status no longer states the deletion date"
    deleted = datetime.date.fromisoformat(deleted_at.group(1))
    days = (deleted - created).days
    assert days == 843, f"2024-04-11 to {deleted} is {days} days"

    # Every place that states a day count for THIS endpoint must state the derived one.
    # A bare "NNN days" near the endpoint's name is the claim; anchor on the name so an
    # unrelated day count elsewhere in a long file cannot satisfy or break this.
    for rel in ("CHANGELOG.md", "docs/ARCHITECTURE.md", "docs/ARCHITECTURE.zh-TW.md",
                "deploy/evidence/whisper_endpoint_snapshot.json"):
        text = (REPO / rel).read_text()
        for m in re.finditer(r"(\d{3})[ -](?:days|day|天)", text):
            stated = int(m.group(1))
            if not 700 <= stated <= 999:
                continue                      # not a lifetime for this endpoint
            assert stated == days, (
                f"{rel} states {stated} days for the orphan's InService life; "
                f"{created} to {deleted} is {days}")


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


# ── the published document, which is what callers actually have ────────────────
#
# Every test above hands RateCard a bare SKU table. The artifact on S3 is a
# DOCUMENT with the table under "rates", so the shape under test was the one shape
# no real caller has -- and passing the real file raised ValueError from
# dict('rate_card'): an error naming a string fragment, pointing nowhere.

def test_the_published_rate_card_document_can_be_priced_directly():
    """The orchestrator prompt tells the agent to read this exact file first.

    An agent doing as instructed passes the document. If only the console knows to
    unwrap it, every other caller gets a crash five frames from its mistake.
    """
    doc = {"kind": "rate_card", "generated_at": "2026-07-31",
           "rate_precedence": ["ce_realized", "price_list"],
           "rates": FULL_RATES, "n_rates": len(FULL_RATES),
           "health": {"n_rates": len(FULL_RATES)}, "notes": ["..."]}
    est = estimate_run(PLAN, doc)
    assert est["unpriced"] == [], "the document's own rates must price the run"
    assert est["total_usd"] == pytest.approx(estimate_run(PLAN, FULL_RATES)["total_usd"])


def test_a_bare_sku_table_still_works_unwrapped():
    """The pre-existing shape must not regress; both callers exist in the tree."""
    assert RateCard(FULL_RATES).price(sku_training("ml.g5.2xlarge")) == \
        FULL_RATES[sku_training("ml.g5.2xlarge")]["unit_price"]


def test_a_sku_literally_named_rates_is_not_mistaken_for_a_document():
    """Unwrapping keys off the SHAPE, not off the mere presence of a "rates" key.

    A table whose SKU is named "rates" would otherwise be read as a document and
    its one real rate silently discarded -- the card would price nothing and every
    line would come back unpriced, which reads as "no rate card" rather than "we
    threw your rates away".
    """
    table = {"rates": {"unit_price": 2.0, "unit": "hours", "source": "ce_realized"}}
    card = RateCard(table)
    assert card.price("rates") == 2.0


def test_an_empty_document_is_an_empty_card_not_a_crash():
    """A card generator that produced no rates yet is a real state; it must read as
    "everything unpriced", which is visibly not-an-estimate."""
    est = estimate_run(PLAN, {"kind": "rate_card", "rates": {}})
    assert est["unpriced"], "an empty card must leave lines unpriced, not priced at 0"
    # Every rate-card-derived category is absent; only the flat support line, which
    # never consults the card, survives. If a card-derived category showed up here it
    # would mean a missing rate had been read as $0 -- the failure this class exists
    # to prevent.
    assert set(est["subtotals"]) == {"support"}, est["subtotals"]


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


# ── the retrieval index (r6d RAFT runs): the one standing cost on the estimate ─────
def test_a_raft_plan_prices_its_retrieval_index_and_a_closed_book_plan_does_not(rates):
    """The category exists exactly when the plan carries it. On every closed-book plan
    it must be ABSENT — a $0 retrieval line on a plan with no index would read as
    'priced and free', which is the confusion the unpriced-vs-zero rule exists for."""
    closed = estimate_run(PLAN, rates)
    assert not [i for i in closed["line_items"] if i["category"] == "retrieval_index"]
    assert "retrieval_index" not in closed["subtotals"]

    raft = estimate_run(dict(PLAN, kb_ocu_hours=240.0, kb_embed_ingest_tokens=500_000),
                        rates)
    lines = [i for i in raft["line_items"] if i["category"] == "retrieval_index"]
    assert {i["sku"] for i in lines} == {SKU_AOSS_OCU,
                                         sku_tokens(SKU_EMBED_MODEL, "input")}
    ocu = next(i for i in lines if i["sku"] == SKU_AOSS_OCU)
    assert ocu["cost_usd"] == pytest.approx(240.0 * FALLBACK_AOSS_OCU_USD)
    # 5 days at the 2-OCU floor is $57.60 — the number the run protocol budgets.
    assert ocu["cost_usd"] == pytest.approx(57.60)


def test_the_retrieval_lines_are_not_remediable(rates):
    """A remediation iteration re-trains and re-judges; it does not re-provision the
    index. Multiplying the OCU line into worst_case would overstate exactly the way
    ignoring max_iterations understates."""
    raft_plan = dict(PLAN, kb_ocu_hours=240.0, kb_embed_ingest_tokens=500_000)
    est = estimate_run(raft_plan, rates)
    lines = [i for i in est["line_items"] if i["category"] == "retrieval_index"]
    assert lines and all(i["remediable"] is False for i in lines)
    base = estimate_run(PLAN, rates)
    # worst_case grows by exactly the retrieval subtotal, once — never times iterations.
    assert est["worst_case_usd"] - base["worst_case_usd"] == pytest.approx(
        est["subtotals"]["retrieval_index"], abs=0.01)


def test_the_fallback_ocu_rate_says_it_is_a_guess_and_loses_to_the_card(rates):
    """fallback_static must (a) mark the estimate's confidence down to 'guessed' and
    (b) yield the moment pricing_refresh lands a realized rate — a static guess wearing
    a measured rate's confidence is how a wrong number inherits credibility."""
    raft_plan = dict(PLAN, kb_ocu_hours=100.0)
    est = estimate_run(raft_plan, rates)
    ocu = next(i for i in est["line_items"] if i["sku"] == SKU_AOSS_OCU)
    assert ocu["rate_source"] == "fallback_static"
    assert est["confidence"] == "guessed", (
        "a guessed OCU rate left the whole estimate claiming better than 'guessed'")

    realized = RateCard(dict(FULL_RATES, **{SKU_AOSS_OCU: {
        "unit_price": 0.30, "source": "ce_realized", "as_of": NOW}}))
    est2 = estimate_run(raft_plan, realized)
    ocu2 = next(i for i in est2["line_items"] if i["sku"] == SKU_AOSS_OCU)
    assert ocu2["rate_source"] == "ce_realized"
    assert ocu2["cost_usd"] == pytest.approx(30.0)


def test_the_ocu_assumption_names_the_teardown_command(rates):
    """The estimate covers the hours the plan admits to; only the teardown stops the
    meter. An assumption that says the first half without the second prices a cost
    while hiding its off switch."""
    est = estimate_run(dict(PLAN, kb_ocu_hours=240.0), rates)
    line = next(a for a in est["assumptions"] if "OCU" in a)
    assert "EXISTS" in line and "09_retrieval.py --teardown" in line, line
    # And no such assumption on a closed-book plan.
    assert not [a for a in estimate_run(PLAN, rates)["assumptions"] if "OCU" in a]


def test_the_fallback_ocu_rate_agrees_with_the_deploy_scripts_constant():
    """Two files quote one collection's hourly rate. The deploy log saying $0.24 while
    the approval estimate prices $0.30 is the two-numbers-two-claims bug: both look
    authoritative, neither is checked against the other — except here."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "llmops_09_retrieval_cost", REPO / "deploy/09_retrieval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert FALLBACK_AOSS_OCU_USD == mod.OCU_HOURLY_USD


def test_required_skus_include_the_index_only_when_the_plan_carries_one():
    assert SKU_AOSS_OCU not in required_skus_for(PLAN)
    req = required_skus_for(dict(PLAN, kb_ocu_hours=240.0,
                                 kb_embed_ingest_tokens=500_000))
    assert SKU_AOSS_OCU in req
    assert sku_tokens(SKU_EMBED_MODEL, "input") in req
