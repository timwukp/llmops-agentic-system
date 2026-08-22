"""v2 distillation augmentation engine.

Expands 849 sandbox-verified (prompt, code) triplets into a large
zero-noise training set via label-preserving transform group D:

  - color permutations  P  (permute colors 1-9 with 0 fixed, and a
    variant permuting all 10 colors)
  - rotations           G  (90 / 180 / 270 via np.rot90)
  - reflections         G  (horizontal / vertical via np.flip)
  - compositions        P o G

For every group element g the train pair grids are transformed to
(g(x), g(y)) and the code target becomes the wrapper

    transform'(z) = g(transform_orig(g^{-1}(z)))

which is correct by construction:
    transform'(g(x)) = g(transform_orig(x)) = g(y).

Zero-noise guarantee: correctness is NOT assumed — every candidate
(wrapped code, transformed pairs) is executed in the sandbox
(verify_sandbox.py) and only emitted if ALL pairs reproduce exactly.

What that guarantee CANNOT see, and why the held-out gate below exists:
the wrapper is correct by construction, so it preserves the base program's
semantics exactly — including when the base program is the WRONG RULE. A
solver that reproduces every shown pair and still mis-transforms an unseen
input passes the sandbox in all 25 variants, and `rejection_rate` stays
0.0%. Measured on real ARC (n=742, `distill_results.json`): 8.8% of
train-verified solvers are wrong programs, rising to 14.7% for those
repaired after being told which pair mismatched. One such solver entering
here becomes 25 poisoned rows that look pristine. So every source row must
carry `heldout_pairs` — pairs the solver's author never saw — the BASE code
is gated on them before any variant is built, and each variant is re-checked
against the same pairs pushed through the same g. Pass
--allow-missing-heldout to augment a corpus that has none, and accept that
the emitted rows are verified only on examples the generator was shown.

Prompt discipline: the augmented prompt is produced by surgically
replacing only the "(HxW):\n<grid>" blocks inside the ORIGINAL prompt
text, so header/trailer/rendering are byte-identical to the source
format (train == inference format). This also transparently handles
the two header variants present in the source file.

Usage:
    python3 augment.py --source <triplets.jsonl> [--arc-dir DIR]
                       [--limit N] [--workers K] [--n-variants 24]
                       [--allow-missing-heldout]

    --source (or $V2_SOURCE_JSONL) is required: the distill output holding the
    sandbox-verified (task_id, prompt, code, heldout_pairs) rows -- see
    build_heldout_source.py, which attaches the held-out pairs and provenance.
    --arc-dir (or $V2_ARC_TRAINING_DIR) is an optional speedup only -- without
    it the train pairs are parsed back out of the prompt text.

Output: pipeline/v2/out/augmented.jsonl
        pipeline/v2/out/augment_stats.json

Exits non-zero when nothing was emitted, or when any variant passes its train
pairs but fails its transformed held-out pairs -- that combination cannot
happen if the wrapper algebra is right, so it is a defect in this file, not a
property of the data, and it must not be written out as a clean corpus.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import random
import re
import sys
import time
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_sandbox import verify_code  # noqa: E402

# The source triplets and the raw ARC tasks are INPUTS, named per invocation. They were
# once absolute paths under one laptop's home directory, which meant the module could only
# ever run on that laptop -- and would have failed there too the moment the sibling
# checkout moved. There is no default for the source: a wrong default silently augments
# the wrong corpus, while a missing one stops with a message naming the flag.
SOURCE_ENV = "V2_SOURCE_JSONL"
ARC_DIR_ENV = "V2_ARC_TRAINING_DIR"
# /tmp/arc is a scratch extraction of the public ARC-AGI training set and is only an
# optimisation: load_train_pairs falls back to parsing the pairs out of the prompt text,
# so an absent directory costs fidelity nowhere.
DEFAULT_ARC_TRAINING_DIR = "/tmp/arc/data/training"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

BASE_NAME = "_transform_base"
TIMEOUT_SEC = 5

# Matches "(HxW):\n<digit rows>" grid blocks in a prompt.
GRID_BLOCK_RE = re.compile(r"\((\d+)x(\d+)\):\n((?:\d(?: \d)*\n)+)")

# Geometric elements: name -> (kind, arg). rot arg = k for np.rot90,
# flip arg = axis for np.flip (0 = vertical/up-down, 1 = horizontal/left-right).
GEOMS = {
    "rot90": ("rot", 1),
    "rot180": ("rot", 2),
    "rot270": ("rot", 3),
    "fliph": ("flip", 1),
    "flipv": ("flip", 0),
}


# --------------------------------------------------------------------------
# Source loading
# --------------------------------------------------------------------------

def resolve_source(cli_value: str | None) -> str:
    """Where the sandbox-verified triplets live: --source, else $V2_SOURCE_JSONL."""
    path = cli_value or os.environ.get(SOURCE_ENV)
    if not path:
        raise SystemExit(
            f"no source triplets given: pass --source <triplets.jsonl> or set "
            f"${SOURCE_ENV}. This is the distill output holding (task_id, prompt, "
            f"code) rows; there is deliberately no default because augmenting the "
            f"wrong corpus produces a plausible training set for the wrong task.")
    if not os.path.exists(path):
        raise SystemExit(f"source triplets not found: {path}")
    return path


def load_source_rows(source_jsonl: str, limit: int | None = None) -> list[dict]:
    rows = []
    with open(source_jsonl) as f:
        for line in f:
            rows.append(json.loads(line))
    return rows[:limit] if limit else rows


def parse_pairs_from_prompt(prompt: str) -> list[dict]:
    """Fallback: parse train pairs from the rendered prompt text."""
    grids = []
    for h, w, body in GRID_BLOCK_RE.findall(prompt):
        grid = [[int(x) for x in ln.split()]
                for ln in body.strip("\n").split("\n")]
        assert len(grid) == int(h) and all(len(r) == int(w) for r in grid)
        grids.append(grid)
    assert len(grids) % 2 == 0
    return [{"input": grids[i], "output": grids[i + 1]}
            for i in range(0, len(grids), 2)]


def load_train_pairs(task_id: str, prompt: str, arc_dir: str) -> list[dict]:
    """Prefer the raw ARC task JSON; fall back to parsing the prompt."""
    path = os.path.join(arc_dir, f"{task_id}.json")
    if os.path.exists(path):
        with open(path) as f:
            task = json.load(f)
        pairs = [{"input": p["input"], "output": p["output"]}
                 for p in task["train"]]
        # The source prompts render exactly the full ARC train set
        # (verified 849/849); guard anyway.
        if len(pairs) * 2 == len(GRID_BLOCK_RE.findall(prompt)):
            return pairs
    return parse_pairs_from_prompt(prompt)


# --------------------------------------------------------------------------
# Group elements
# --------------------------------------------------------------------------

def apply_geom(grid: np.ndarray, geom: str | None) -> np.ndarray:
    if geom is None:
        return grid
    kind, arg = GEOMS[geom]
    return np.rot90(grid, arg) if kind == "rot" else np.flip(grid, arg)


def transform_grid(grid: list[list[int]], geom: str | None,
                   perm: tuple[int, ...] | None) -> list[list[int]]:
    """g(x) = perm[geom(x)] — geometry then pointwise color remap."""
    g = np.array(grid)
    g = apply_geom(g, geom)
    if perm is not None:
        g = np.array(perm)[g]
    return g.tolist()


def invert_perm(perm: tuple[int, ...]) -> list[int]:
    inv = [0] * 10
    for i, p in enumerate(perm):
        inv[p] = i
    return inv


def make_perm(rng: random.Random, full: bool) -> tuple[int, ...]:
    """Random color permutation. full=False keeps 0 fixed (permute 1-9);
    full=True permutes all 10 colors and is required to move color 0."""
    while True:
        if full:
            p = rng.sample(range(10), 10)
            if p[0] != 0 and p != list(range(10)):
                return tuple(p)
        else:
            p = [0] + rng.sample(range(1, 10), 9)
            if p != list(range(10)):
                return tuple(p)


# --------------------------------------------------------------------------
# Code wrapper: transform'(z) = g(base(g^{-1}(z)))
# --------------------------------------------------------------------------

def rename_base(code: str) -> str:
    """Rename `transform` -> `_transform_base` (covers the one recursive
    solution in the corpus; no name collisions exist — verified)."""
    return re.sub(r"\btransform\b", BASE_NAME, code)


def build_wrapped_code(code: str, geom: str | None,
                       perm: tuple[int, ...] | None) -> str:
    body = [rename_base(code).rstrip(), "", "", "def transform(grid):",
            "    import numpy as np"]
    if perm is not None:
        body.append(f"    _perm = np.array({list(perm)})")
        body.append(f"    _inv_perm = np.array({invert_perm(perm)})")
    body.append("    g = np.array(grid)")
    # inverse of g: undo colors first, then undo geometry
    if perm is not None:
        body.append("    g = _inv_perm[g]")
    if geom is not None:
        kind, arg = GEOMS[geom]
        body.append(f"    g = np.rot90(g, {4 - arg})" if kind == "rot"
                    else f"    g = np.flip(g, {arg})")
    body.append(f"    out = np.array({BASE_NAME}(g.tolist()))")
    # forward g: geometry then colors
    if geom is not None:
        kind, arg = GEOMS[geom]
        body.append(f"    out = np.rot90(out, {arg})" if kind == "rot"
                    else f"    out = np.flip(out, {arg})")
    if perm is not None:
        body.append("    out = _perm[out]")
    body.append("    return out.tolist()")
    return "\n".join(body)


# --------------------------------------------------------------------------
# Prompt re-rendering (surgical block replacement)
# --------------------------------------------------------------------------

def render_grid_block(grid: list[list[int]]) -> str:
    h, w = len(grid), len(grid[0])
    body = "\n".join(" ".join(str(c) for c in row) for row in grid)
    return f"({h}x{w}):\n{body}\n"


def rerender_prompt(prompt: str, new_grids: list[list[list[int]]]) -> str:
    """Replace the i-th grid block with new_grids[i]; all other prompt
    text (headers, labels, trailers) is preserved byte-for-byte."""
    matches = list(GRID_BLOCK_RE.finditer(prompt))
    assert len(matches) == len(new_grids), "grid block count mismatch"
    out, pos = [], 0
    for m, grid in zip(matches, new_grids):
        out.append(prompt[pos:m.start()])
        out.append(render_grid_block(grid))
        pos = m.end()
    out.append(prompt[pos:])
    return "".join(out)


# --------------------------------------------------------------------------
# Variant planning
# --------------------------------------------------------------------------

def plan_variants(task_id: str, n_variants: int) -> list[dict]:
    """Deterministic per-task plan of up to n_variants group elements
    (mix of pure geometric, pure color, and compositions)."""
    rng = random.Random(int(task_id, 16) ^ 0x5F3759DF)
    plans = []

    def add(geom, perm, tag):
        sig = f"{geom or 'id'}|{','.join(map(str, perm)) if perm else 'id'}"
        plans.append({"geom": geom, "perm": perm, "tag": tag, "sig": sig})

    # 5 pure geometric
    for geom in GEOMS:
        add(geom, None, f"geom:{geom}")
    # 4 pure color: 2x perm(1-9), 2x perm(all 10)
    seen = set()
    for full, label in [(False, "perm9"), (False, "perm9"),
                        (True, "perm10"), (True, "perm10")]:
        while True:
            p = make_perm(rng, full)
            if p not in seen:
                seen.add(p)
                break
        add(None, p, f"color:{label}")
    # 15 compositions: each geometric element with a fresh perm
    comp_kinds = [False, True, False]  # perm9, perm10, perm9
    for i, full in enumerate(comp_kinds):
        for geom in GEOMS:
            while True:
                p = make_perm(rng, full)
                if p not in seen:
                    seen.add(p)
                    break
            add(geom, p, f"comp:{geom}+{'perm10' if full else 'perm9'}")

    # dedupe by signature, cap at n_variants
    out, sigs = [], set()
    for pl in plans:
        if pl["sig"] not in sigs:
            sigs.add(pl["sig"])
            out.append(pl)
    return out[:n_variants]


# --------------------------------------------------------------------------
# Per-task worker
# --------------------------------------------------------------------------

def transform_pairs(pairs: list[dict], geom: str | None,
                    perm: tuple[int, ...] | None) -> list[dict]:
    return [{"input": transform_grid(p["input"], geom, perm),
             "output": transform_grid(p["output"], geom, perm)}
            for p in pairs]


def process_task(args: tuple[dict, int, str, bool]) -> dict:
    """Generate + sandbox-verify all variants for one source triplet.

    arc_dir travels in the tuple rather than being read from a module global: the
    worker pool uses `spawn` on Windows, which re-imports this module in a fresh
    interpreter, so a global mutated by main() in the parent would silently revert
    to its default in every worker. require_heldout travels the same way and for
    the same reason -- a gate that reverts to a default in the workers is not a
    gate.

    Held-out handling, in order:
      1. no held-out pairs and they are required -> emit nothing, status "missing"
      2. base code fails them                    -> emit nothing, status "gated_out"
         (the whole task goes, not one variant: every variant shares the base rule)
      3. a variant passes train but fails its transformed held-out pairs
         -> "wrapper_mismatch": impossible unless transform_pairs and
            build_wrapped_code disagree, so it is reported as a code defect and
            the driver exits non-zero rather than emitting the row.
    """
    row, n_variants, arc_dir, require_heldout = args
    task_id, prompt, code = row["task_id"], row["prompt"], row["code"]
    pairs = load_train_pairs(task_id, prompt, arc_dir)
    heldout = row.get("heldout_pairs") or []
    repair_rounds = row.get("repair_rounds", "unknown")

    base = {"task_id": task_id, "emitted": [], "rejected": [],
            "heldout_mismatch": [], "heldout_status": "ok"}

    if not heldout:
        if require_heldout:
            return {**base, "heldout_status": "missing"}
    else:
        hv = verify_code(code, heldout, TIMEOUT_SEC)
        if not hv["all_pass"]:
            return {**base, "heldout_status": "gated_out",
                    "heldout_fail_reason": hv["fail_reason"]}

    emitted, rejected, mismatch = [], [], []

    # variant 0: the original, re-verified (belt and braces)
    candidates = [{"geom": None, "perm": None, "tag": "orig", "sig": "orig",
                   "prompt": prompt, "code": code, "pairs": pairs,
                   "heldout": heldout}]

    for pl in plan_variants(task_id, n_variants):
        new_pairs = transform_pairs(pairs, pl["geom"], pl["perm"])
        grids = []
        for p in new_pairs:
            grids.extend([p["input"], p["output"]])
        candidates.append({
            **pl,
            "prompt": rerender_prompt(prompt, grids),
            "code": build_wrapped_code(code, pl["geom"], pl["perm"]),
            "pairs": new_pairs,
            # The same g that moved the train pairs moves the held-out pairs, so
            # transform'(g(test_in)) == g(base(test_in)) == g(test_out) whenever
            # the base is right on the test input. The variant's held-out pair is
            # therefore ground truth for the variant's prompt, which is what makes
            # a held-out metric possible at eval time at all.
            "heldout": transform_pairs(heldout, pl["geom"], pl["perm"]),
        })

    for cand in candidates:
        v = verify_code(cand["code"], cand["pairs"], TIMEOUT_SEC)
        if not v["all_pass"]:
            rejected.append({"task_id": task_id, "tag": cand["tag"],
                             "sig": cand["sig"], "reason": v["fail_reason"]})
            continue
        if cand["heldout"]:
            hv = verify_code(cand["code"], cand["heldout"], TIMEOUT_SEC)
            if not hv["all_pass"]:
                mismatch.append({"task_id": task_id, "tag": cand["tag"],
                                 "sig": cand["sig"], "reason": hv["fail_reason"]})
                continue
        emitted.append({
            "task_id": task_id,
            "variant": cand["tag"] if cand["tag"] == "orig" else
                       f"{cand['tag']}#{cand['sig']}",
            "prompt": cand["prompt"],
            "code": cand["code"],
            "n_train_pairs": len(cand["pairs"]),
            "verified": True,
            # Carried per row, not per source task: the eval scorer sees one row
            # at a time and has no way back to the source. Dropping these here is
            # what made the student's solve_rate a shown-examples tautology.
            "heldout_pairs": cand["heldout"],
            "heldout_ok": bool(cand["heldout"]),
            "repair_rounds": repair_rounds,
        })

    return {**base, "emitted": emitted, "rejected": rejected,
            "heldout_mismatch": mismatch}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="only process first N source rows (smoke test)")
    ap.add_argument("--workers", type=int, default=max(1, os.cpu_count() - 2))
    ap.add_argument("--n-variants", type=int, default=24)
    ap.add_argument("--allow-missing-heldout", action="store_true",
                    help="augment source rows that carry no `heldout_pairs`. Off by "
                         "default: without held-out pairs `verified` means only "
                         "'reproduces the examples the generator was shown', and a "
                         "wrong rule is copied into every variant undetected")
    ap.add_argument("--source", default=None,
                    help=f"sandbox-verified (task_id, prompt, code) triplets as "
                         f"jsonl; required, or set ${SOURCE_ENV}")
    ap.add_argument("--arc-dir", default=None,
                    help=f"directory of raw ARC task JSON (optional speedup; the "
                         f"pairs are otherwise parsed out of the prompt). Defaults "
                         f"to ${ARC_DIR_ENV} then {DEFAULT_ARC_TRAINING_DIR}")
    args = ap.parse_args()

    source = resolve_source(args.source)
    arc_dir = (args.arc_dir or os.environ.get(ARC_DIR_ENV)
               or DEFAULT_ARC_TRAINING_DIR)

    os.makedirs(OUT_DIR, exist_ok=True)
    rows = load_source_rows(source, args.limit)
    print(f"source: {source}\narc_dir: {arc_dir}"
          f"{'' if os.path.isdir(arc_dir) else ' (absent -- parsing prompts)'}")
    print(f"source rows: {len(rows)}, workers: {args.workers}, "
          f"n_variants: {args.n_variants}")

    require_heldout = not args.allow_missing_heldout
    print(f"held-out gate: {'REQUIRED' if require_heldout else 'DISABLED'}")

    t0 = time.time()
    tasks = [(r, args.n_variants, arc_dir, require_heldout) for r in rows]
    if args.workers > 1:
        ctx = mp.get_context("fork" if sys.platform != "win32" else "spawn")
        with ctx.Pool(args.workers) as pool:
            results = []
            for i, res in enumerate(pool.imap_unordered(process_task, tasks,
                                                        chunksize=4), 1):
                results.append(res)
                if i % 100 == 0 or i == len(tasks):
                    print(f"  {i}/{len(tasks)} tasks "
                          f"({time.time() - t0:.0f}s)", flush=True)
    else:
        results = [process_task(t) for t in tasks]

    emitted, rejected, mismatch = [], [], []
    gated_out, missing = [], []
    for res in results:
        emitted.extend(res["emitted"])
        rejected.extend(res["rejected"])
        mismatch.extend(res.get("heldout_mismatch", []))
        if res.get("heldout_status") == "gated_out":
            gated_out.append({"task_id": res["task_id"],
                              "reason": res.get("heldout_fail_reason")})
        elif res.get("heldout_status") == "missing":
            missing.append(res["task_id"])
    emitted.sort(key=lambda r: (r["task_id"], r["variant"]))

    out_path = os.path.join(OUT_DIR, "augmented.jsonl")
    with open(out_path, "w") as f:
        for r in emitted:
            f.write(json.dumps(r) + "\n")

    per_transform = Counter(r["variant"].split("#")[0] for r in emitted)
    reject_by_tag = Counter(r["tag"] for r in rejected)
    tok = sorted((len(r["prompt"]) + len(r["code"])) // 4 for r in emitted)
    n_source_gated = len(rows) - len(gated_out) - len(missing)
    stats = {
        "source_rows": len(rows),
        "emitted": len(emitted),
        "rejected_verify_fail": len(rejected),
        "rejection_rate": round(len(rejected) /
                                max(1, len(emitted) + len(rejected)), 4),
        # The three numbers that describe the held-out gate. `rejection_rate`
        # cannot: a wrong base rule passes every shown pair in every variant, so
        # it is invisible there by construction. Kept as separate counters rather
        # than folded into the rejection total because they mean different things
        # -- one is a wrong program, one is an absent measurement, one is a bug.
        "heldout_gate": "required" if require_heldout else "disabled",
        "heldout_gated_out_tasks": len(gated_out),
        "heldout_missing_tasks": len(missing),
        "heldout_wrapper_mismatch": len(mismatch),
        "source_tasks_heldout_ok": n_source_gated,
        "per_transform_emitted": dict(sorted(per_transform.items())),
        "per_transform_rejected": dict(sorted(reject_by_tag.items())),
        "median_prompt_plus_code_tokens_est": tok[len(tok) // 2] if tok else 0,
        # chars/4 is the generic English ratio and it is WRONG for these rows.
        # Measured with the real Qwen3 tokenizer on ARC grid text: ~2 chars per
        # token (~2 tokens per cell), so this estimate understates the true length
        # by 2-2.5x. Sizing --max_length from it is how 18% of a corpus gets
        # silently dropped by --drop_overlong.
        "token_estimate_caveat": (
            "median_prompt_plus_code_tokens_est is chars/4; the real Qwen3 "
            "tokenizer yields ~2 chars/token on ARC grid text, so multiply by "
            "2-2.5x before choosing max_length (see pipeline/v2/README.md)"),
        "elapsed_sec": round(time.time() - t0, 1),
        "reject_samples": rejected[:20],
        "heldout_gated_out_samples": gated_out[:20],
        "heldout_wrapper_mismatch_samples": mismatch[:20],
    }
    with open(os.path.join(OUT_DIR, "augment_stats.json"), "w") as f:
        json.dump(stats, f, indent=2)

    print(json.dumps({k: v for k, v in stats.items()
                      if not k.endswith("_samples")}, indent=2))
    print(f"wrote {out_path}")

    # Stats are written first: a run that stops here must still leave behind the
    # evidence of why. Both conditions are failures of this program rather than
    # verdicts about the corpus, so neither may exit 0.
    if mismatch:
        print(f"FAIL: {len(mismatch)} variants passed their train pairs and failed "
              f"their transformed held-out pairs. The wrapper algebra guarantees "
              f"this cannot happen -- transform_pairs and build_wrapped_code "
              f"disagree. Nothing was emitted for those rows; fix before training.",
              file=sys.stderr)
        return 1
    if not emitted:
        print(f"FAIL: emitted 0 rows from {len(rows)} source rows "
              f"({len(missing)} missing held-out pairs, {len(gated_out)} gated out). "
              f"An empty corpus must not read as a successful run.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
