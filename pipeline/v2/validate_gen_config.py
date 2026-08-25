#!/usr/bin/env python3
"""Preflight a SageMaker CreateTrainingJob payload that RUNS GENERATION, not training.

`validate_job_config.py` is the preflight for a fine-tuning payload and it does not
apply here: every quantity it multiplies -- `save_steps`, `epochs`,
`gradient_accumulation`, rows x epochs / effective batch -- is absent from a
generation job. Pointed at this payload it would emit four FAILs that are all
correct for a trainer and all meaningless for a decoder, and the only way to launch
would be to ignore its verdict. A gate whose output is routinely ignored has stopped
being a gate, so the generation path gets its own arithmetic instead of an exemption.

The defect being gated is the same one, generalised. Run
v2-code-distill-0001-e1g6 burned 43 GPU-minutes and produced zero artifacts because
its only save point sat past the wall clock. A generation job is exposed identically:
`/opt/ml/output/data` is uploaded when the container EXITS, so a job still decoding
when MaxRuntimeInSeconds fires can lose every generation it already wrote. The
answer is the same as the trainer's `--max_train_seconds`: a graceful in-process
budget, checked here.

WHY AN UNMEASURED RATE IS ALLOWED, BUT ONLY TIME-BOXED. The first generation job on
a new model has no measured tokens/second, and there is no way to get one without
running it. Requiring the measurement would make the measuring job unlaunchable;
inventing a default would make the gate pass on a number nobody measured. So this
script splits the difference along the line that actually bounds the money: with
`--tok-per-s` it checks that the configured work FITS the budget, and without it it
checks only that the job CANNOT exceed the budget, and says out loud that the
completeness of the run is therefore unknown. An unmeasured job is safe to launch
exactly when it is time-boxed; it is never safe to call complete.

Usage:
    python validate_gen_config.py job.json --n-rows <val rows> \
        --startup-seconds <download+extract+load> [--tok-per-s <measured>]

Exits non-zero on any FAIL.
"""
from __future__ import annotations

import argparse
import json
import sys

# Time SageMaker needs AFTER the container exits 0, before the job is Completed:
# /opt/ml/output/data is tarred and uploaded. generations.jsonl for 120 tasks is
# under a megabyte, so this is dominated by fixed overhead rather than size.
UPLOAD_SECONDS = 120


def check(payload, n_rows, startup_seconds, tok_per_s):
    """Return (failures, warnings, facts) for a generation payload."""
    fails, warns, facts = [], [], []

    hp = {k: str(v).strip().strip('"')
          for k, v in payload.get("HyperParameters", {}).items()}
    max_runtime = payload.get("StoppingCondition", {}).get("MaxRuntimeInSeconds")

    def num(key, default=None):
        try:
            return float(hp[key])
        except (KeyError, ValueError):
            return default

    budget = int(num("max_seconds", 0) or 0)
    limit = int(num("limit", 0) or 0)
    n_samples = int(num("n_samples", 1) or 1)
    max_new = int(num("max_new_tokens", 1536) or 1536)
    batch = int(num("batch_size", 1) or 1)
    input_window = int(num("input_window", 0) or 0)
    temperature = num("temperature", 0.0) or 0.0

    # 1. The job must be bounded from the inside, not only from the outside.
    if not max_runtime:
        fails.append("StoppingCondition.MaxRuntimeInSeconds is not set")
    if budget <= 0:
        fails.append(
            "max_seconds is not set: the only bound on this job is SageMaker's hard "
            "kill, which terminates the container mid-decode and can lose every "
            "generation already written, because /opt/ml/output/data is uploaded on "
            "container EXIT. Set max_seconds so the loop stops gracefully.")
    if max_runtime and budget > 0:
        need = startup_seconds + budget + UPLOAD_SECONDS
        facts.append(f"startup {startup_seconds}s + budget {budget}s + upload "
                     f"{UPLOAD_SECONDS}s = {need}s vs MaxRuntime {max_runtime}s")
        if need >= max_runtime:
            fails.append(
                f"startup + max_seconds + upload ({need}s) >= MaxRuntimeInSeconds "
                f"({max_runtime}s): the graceful budget cannot finish before the hard "
                f"kill, so it buys nothing. Raise MaxRuntime or lower max_seconds.")
        else:
            facts.append(f"{max_runtime - need}s of slack beyond the accounted time, "
                         f"before the final batch's overshoot is counted")

    # 2. Is the configured work reachable in the budget? Only answerable with a rate.
    generations = (min(limit, n_rows) if limit else n_rows) * n_samples
    if generations <= 0:
        fails.append(f"no generations would be produced (n_rows {n_rows}, limit "
                     f"{limit}, n_samples {n_samples})")
    facts.append(f"{generations} generations = "
                 f"{min(limit, n_rows) if limit else n_rows} prompts x {n_samples} "
                 f"samples, batch {batch}, <= {max_new} new tokens each")
    if tok_per_s and generations > 0:
        worst = generations * max_new / tok_per_s
        facts.append(f"worst case {generations * max_new} decoded tokens at "
                     f"{tok_per_s:.0f} tok/s = {worst / 60:.1f} min of decoding")
        if budget and worst > budget:
            warns.append(
                f"worst-case decoding ({worst / 60:.1f} min) exceeds max_seconds "
                f"({budget / 60:.1f} min), so this job may stop early and cover only "
                f"part of the prompts. That is fine for a MEASUREMENT and wrong for a "
                f"lift comparison: check stopped_early in the .done before comparing.")
        # THE BUDGET CANNOT INTERRUPT A BATCH. It is read before a batch starts, so the
        # worst case is `budget + one batch's decode`, and it is that sum -- not the
        # budget -- that has to clear the hard kill. Measured, which is why this check
        # exists: arc2v2-gen-base-0823a-g5 (base Qwen3-4B-Thinking, A10G, batch 2,
        # max_new_tokens 16384) had not finished its FIRST batch after 25 minutes, on a
        # job whose graceful budget was 45 minutes and whose hard kill was 60. It
        # cleared the old headroom check with 180s of slack and had no bound on the one
        # batch that mattered.
        # `batch_size` counts PROMPTS; `generate` is asked for `n_samples` sequences per
        # prompt, so a batch decodes batch_size * n_samples sequences concurrently and the
        # overshoot is n_samples times what the prompt count suggests. Using `batch` here
        # understated the overshoot by exactly that factor -- invisible in every test
        # because they all ran at the n_samples=1 default, where the two agree. It also
        # disagreed with `generations` above, which has always included n_samples.
        seqs_per_batch = min(batch * n_samples, generations)
        one_batch = seqs_per_batch * max_new / tok_per_s
        if max_runtime and budget > 0:
            with_overshoot = startup_seconds + budget + one_batch + UPLOAD_SECONDS
            facts.append(f"one batch = {min(batch, generations)} prompts x {n_samples} "
                         f"samples = {seqs_per_batch} sequences x {max_new} tokens = "
                         f"{one_batch / 60:.1f} min of possible overshoot; "
                         f"{with_overshoot:.0f}s worst case vs MaxRuntime "
                         f"{max_runtime}s")
            if with_overshoot >= max_runtime:
                fails.append(
                    f"the budget is checked BETWEEN batches, so the run can reach "
                    f"{with_overshoot:.0f}s (startup + max_seconds + one batch + "
                    f"upload) against MaxRuntimeInSeconds {max_runtime}s. A batch that "
                    f"is still decoding when the hard kill fires loses every generation "
                    f"in it, and with one batch that is the whole run. Raise MaxRuntime, "
                    f"lower max_new_tokens, or lower batch_size.")
    elif generations > 0:
        warns.append(
            "--tok-per-s was not given, so whether the budget covers the configured "
            "work is UNKNOWN. This is launchable only because max_seconds bounds the "
            "spend; treat the run as a measurement of the rate and do not report its "
            "coverage as complete. Read tok/s off the [gen] progress lines.")
        # Unconditional, and deliberately not gated on a slack threshold: without a rate
        # the overshoot is an unknown quantity, and a warning that only fires when slack
        # happens to be thin is measuring the wrong thing. That is the version that let
        # the g5 job through.
        warns.append(
            f"and because max_seconds is only read BETWEEN batches, the final batch is "
            f"bounded by max_new_tokens ({max_new}) and NOT by the budget. With an "
            f"unmeasured rate, the overshoot past max_seconds is UNKNOWN, so "
            f"MaxRuntimeInSeconds is the only thing standing between this job and losing "
            f"the batch it is decoding. Size max_new_tokens so one batch cannot plausibly "
            f"fill the slack, and read the rate off this run.")
        # An unmeasured rate does not make the overshoot unquantifiable -- only its
        # ACTUAL value is unknown. The rate the configuration would REQUIRE is pure
        # arithmetic, and stating it converts "UNKNOWN" into a number the reader can
        # judge against anything they know about the instance. On the g5 job this line
        # would have read "requires >= 182 tok/s"; the A10G delivered 12.2, and a
        # 15x gap is visible without any prior measurement of this model.
        if max_runtime and budget > 0 and startup_seconds + budget + UPLOAD_SECONDS \
                < max_runtime:
            slack = max_runtime - (startup_seconds + budget + UPLOAD_SECONDS)
            # Same sequences-not-prompts correction as the measured branch above. A
            # required rate quoted per prompt is n_samples times too lenient, which is the
            # direction that lets a job launch.
            seqs_per_batch = min(batch * n_samples, generations)
            need_rate = seqs_per_batch * max_new / slack
            facts.append(
                f"the final batch has {slack}s of slack to finish in, so clearing it "
                f"requires >= {need_rate:.0f} tok/s aggregate for "
                f"{min(batch, generations)} prompts x {n_samples} samples = "
                f"{seqs_per_batch} sequences x {max_new} tokens. That rate is NOT "
                f"measured here -- it is the rate this configuration assumes.")

    # 3. Sampling and window: two ways to produce a plausible, wrong eval.
    if n_samples > 1 and temperature <= 0:
        fails.append(
            f"n_samples {n_samples} with temperature {temperature}: k greedy samples "
            f"are k identical strings, so pass@k over them is pass@1 with a k-times "
            f"larger denominator")
    if input_window <= 0:
        warns.append("input_window is unset, so the script default applies; the "
                     "eval120 prompts measure up to 14,513 chat-wrapped tokens and "
                     "left-truncation deletes the instruction header")
    elif input_window < 14513:
        fails.append(
            f"input_window {input_window} < 14513, the measured maximum chat-wrapped "
            f"eval120 prompt: truncation_side is 'left', so the overflowing task loses "
            f"its instruction header and is scored as a model failure")

    return fails, warns, facts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job_json")
    ap.add_argument("--n-rows", type=int, required=True,
                    help="rows in the val file this job will read")
    # No default. The startup cost is model download + tar extraction + weight load,
    # and it is the term that decides whether a budget leaves room to exit cleanly.
    # A guessed value here would make the headroom check agree with itself.
    ap.add_argument("--startup-seconds", type=int, required=True,
                    help="S3 download + extract + from_pretrained, measured if a "
                         "prior run of the same artifact reported it")
    ap.add_argument("--tok-per-s", type=float, default=None,
                    help="MEASURED aggregate generation throughput for this model, "
                         "instance and batch size; omit on the job that measures it")
    args = ap.parse_args()

    payload = json.loads(open(args.job_json).read())
    fails, warns, facts = check(payload, args.n_rows, args.startup_seconds,
                                args.tok_per_s)

    print(f"job: {payload.get('TrainingJobName', '(unnamed)')}")
    for f in facts:
        print(f"  fact  {f}")
    for w in warns:
        print(f"  WARN  {w}")
    for f in fails:
        print(f"  FAIL  {f}")
    print("PASS — safe to launch" if not fails
          else f"{len(fails)} FAIL — do not launch")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
