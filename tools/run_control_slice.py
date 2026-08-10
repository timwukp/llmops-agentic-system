#!/usr/bin/env python3
"""Run a contiguous slice of the negative-control suite.

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/run_control_slice.py 171 180

The full runner takes minutes because it invokes pytest once per (guard, mutation) pair, so
checking the controls you just wrote needs a slice. It must be the REAL runner's loop and not
a copy of it: that loop clears `__pycache__` per case, journals before mutating so a killed
run restores the tree, and scores on `PYTEST_TESTS_FAILED` rather than "pytest was unhappy".
A hand-rolled mutate-run-restore is exactly the methodology that produced false catches
(CPython validates a `.pyc` on source mtime-in-whole-seconds and size, so a same-second,
same-byte-count restore runs mutated bytecode against restored source).

The runner has no `main()` -- it is a module-level `for` loop over
`(CASES if __name__ == "__main__" else ())`, kept import-safe on purpose. So this imports it
once to read CASES, then execs the source under `__name__ == "__main__"` with CASES spliced
down. The loop header is asserted before running: if it is ever refactored this fails loudly
instead of silently selecting nothing and printing success.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "tests/negative_controls/monitor_dispatch.py"
LOOP = ('failed = []\n'
        'for name, rel, mutate, tests in (CASES if __name__ == "__main__" else ()):')


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    lo, hi = int(argv[0]), int(argv[1])
    spec = importlib.util.spec_from_file_location("nc_probe", SRC)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)          # import-safe: registers CASES, runs nothing
    keep = [c[0] for c in probe.CASES[lo - 1:hi]]
    if not keep:
        print(f"no cases in [{lo}, {hi}] -- the runner registers {len(probe.CASES)}")
        return 2
    print(f"slice {lo}..{hi}: {len(keep)} of {len(probe.CASES)} cases")

    src = SRC.read_text()
    assert src.count(LOOP) == 1, (
        "the runner's case loop has been refactored; this script would select nothing and "
        "then report success. Re-derive the splice point from the runner.")
    src = src.replace(LOOP, f"failed = []\nCASES[:] = [c for c in CASES if c[0] in {keep!r}]\n"
                            + LOOP.split("\n", 1)[1], 1)
    g = {"__name__": "__main__", "__file__": str(SRC)}
    exec(compile(src, str(SRC), "exec"), g)   # exits via the runner's own sys.exit
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
