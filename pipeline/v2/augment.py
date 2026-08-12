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

Prompt discipline: the augmented prompt is produced by surgically
replacing only the "(HxW):\n<grid>" blocks inside the ORIGINAL prompt
text, so header/trailer/rendering are byte-identical to the source
format (train == inference format). This also transparently handles
the two header variants present in the source file.

Usage:
    python3 augment.py --source <triplets.jsonl> [--arc-dir DIR]
                       [--limit N] [--workers K] [--n-variants 24]

    --source (or $V2_SOURCE_JSONL) is required: the distill output holding the
    sandbox-verified (task_id, prompt, code) rows. --arc-dir (or
    $V2_ARC_TRAINING_DIR) is an optional speedup only -- without it the train
    pairs are parsed back out of the prompt text.

Output: pipeline/v2/out/augmented.jsonl
        pipeline/v2/out/augment_stats.json
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

def process_task(args: tuple[dict, int, str]) -> dict:
    """Generate + sandbox-verify all variants for one source triplet.

    arc_dir travels in the tuple rather than being read from a module global: the
    worker pool uses `spawn` on Windows, which re-imports this module in a fresh
    interpreter, so a global mutated by main() in the parent would silently revert
    to its default in every worker."""
    row, n_variants, arc_dir = args
    task_id, prompt, code = row["task_id"], row["prompt"], row["code"]
    pairs = load_train_pairs(task_id, prompt, arc_dir)

    emitted, rejected = [], []

    # variant 0: the original, re-verified (belt and braces)
    candidates = [{"geom": None, "perm": None, "tag": "orig", "sig": "orig",
                   "prompt": prompt, "code": code, "pairs": pairs}]

    for pl in plan_variants(task_id, n_variants):
        new_pairs = [{"input": transform_grid(p["input"], pl["geom"], pl["perm"]),
                      "output": transform_grid(p["output"], pl["geom"], pl["perm"])}
                     for p in pairs]
        grids = []
        for p in new_pairs:
            grids.extend([p["input"], p["output"]])
        candidates.append({
            **pl,
            "prompt": rerender_prompt(prompt, grids),
            "code": build_wrapped_code(code, pl["geom"], pl["perm"]),
            "pairs": new_pairs,
        })

    for cand in candidates:
        v = verify_code(cand["code"], cand["pairs"], TIMEOUT_SEC)
        if v["all_pass"]:
            emitted.append({
                "task_id": task_id,
                "variant": cand["tag"] if cand["tag"] == "orig" else
                           f"{cand['tag']}#{cand['sig']}",
                "prompt": cand["prompt"],
                "code": cand["code"],
                "n_train_pairs": len(cand["pairs"]),
                "verified": True,
            })
        else:
            rejected.append({"task_id": task_id, "tag": cand["tag"],
                             "sig": cand["sig"], "reason": v["fail_reason"]})

    return {"task_id": task_id, "emitted": emitted, "rejected": rejected}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="only process first N source rows (smoke test)")
    ap.add_argument("--workers", type=int, default=max(1, os.cpu_count() - 2))
    ap.add_argument("--n-variants", type=int, default=24)
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

    t0 = time.time()
    tasks = [(r, args.n_variants, arc_dir) for r in rows]
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

    emitted, rejected = [], []
    for res in results:
        emitted.extend(res["emitted"])
        rejected.extend(res["rejected"])
    emitted.sort(key=lambda r: (r["task_id"], r["variant"]))

    out_path = os.path.join(OUT_DIR, "augmented.jsonl")
    with open(out_path, "w") as f:
        for r in emitted:
            f.write(json.dumps(r) + "\n")

    per_transform = Counter(r["variant"].split("#")[0] for r in emitted)
    reject_by_tag = Counter(r["tag"] for r in rejected)
    tok = sorted((len(r["prompt"]) + len(r["code"])) // 4 for r in emitted)
    stats = {
        "source_rows": len(rows),
        "emitted": len(emitted),
        "rejected_verify_fail": len(rejected),
        "rejection_rate": round(len(rejected) /
                                max(1, len(emitted) + len(rejected)), 4),
        "per_transform_emitted": dict(sorted(per_transform.items())),
        "per_transform_rejected": dict(sorted(reject_by_tag.items())),
        "median_prompt_plus_code_tokens_est": tok[len(tok) // 2] if tok else 0,
        "elapsed_sec": round(time.time() - t0, 1),
        "reject_samples": rejected[:20],
    }
    with open(os.path.join(OUT_DIR, "augment_stats.json"), "w") as f:
        json.dump(stats, f, indent=2)

    print(json.dumps({k: v for k, v in stats.items()
                      if k != "reject_samples"}, indent=2))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
