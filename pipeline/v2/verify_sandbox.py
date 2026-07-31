"""Sandboxed verification of ARC `transform(grid)` code against train pairs.

Ported from kaggle-arc-agi-2/src/verification_engine.py (safe-exec pattern):
- restricted builtins namespace
- guarded __import__ (whitelist only)
- signal-based timeout (SIGALRM) when running in a process main thread,
  which is the case both in the driver and inside multiprocessing.Pool
  workers on Unix
- exact cell-by-cell grid comparison

Pure Python 3.12 + numpy (scipy and a few stdlib modules are whitelisted
because the 849 source solutions import them; the sandbox itself only
requires numpy).
"""
from __future__ import annotations

import signal
import threading
import traceback

import numpy as np

# Modules the verified source solutions are allowed to import.
# Measured over the 849 source codes: numpy 832, collections 138,
# scipy 32, itertools 8, math 3.
ALLOWED_MODULES = {
    "numpy", "scipy", "collections", "itertools", "math", "functools", "copy",
}


class SandboxTimeoutError(Exception):
    pass


def _timeout_handler(signum, frame):
    raise SandboxTimeoutError("Execution timed out")


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root not in ALLOWED_MODULES:
        raise ImportError(f"import of '{name}' is not allowed in sandbox")
    return __import__(name, globals, locals, fromlist, level)


def build_namespace() -> dict:
    """Restricted namespace for executing untrusted transform code."""
    from copy import deepcopy
    from collections import Counter, defaultdict

    namespace = {"__builtins__": {
        "range": range, "len": len, "int": int, "float": float,
        "list": list, "tuple": tuple, "dict": dict, "set": set,
        "min": min, "max": max, "abs": abs, "sum": sum,
        "enumerate": enumerate, "zip": zip, "sorted": sorted,
        "reversed": reversed, "any": any, "all": all,
        "True": True, "False": False, "None": None,
        "isinstance": isinstance, "type": type,
        "map": map, "filter": filter, "print": print,
        "ValueError": ValueError, "IndexError": IndexError,
        "KeyError": KeyError, "TypeError": TypeError,
        "StopIteration": StopIteration,
        "bool": bool, "str": str, "bytes": bytes,
        "frozenset": frozenset, "slice": slice,
        "hasattr": hasattr, "getattr": getattr, "setattr": setattr,
        "callable": callable, "iter": iter, "next": next,
        "round": round, "pow": pow, "divmod": divmod,
        "hash": hash, "id": id, "repr": repr,
        "chr": chr, "ord": ord,
        "object": object, "super": super,
        "property": property, "staticmethod": staticmethod,
        "classmethod": classmethod,
        "Exception": Exception, "RuntimeError": RuntimeError,
        "ZeroDivisionError": ZeroDivisionError,
        "__import__": _guarded_import,
        "__build_class__": __build_class__,
        "__name__": "sandbox",
    }}
    namespace["np"] = np
    namespace["numpy"] = np
    namespace["deepcopy"] = deepcopy
    namespace["Counter"] = Counter
    namespace["defaultdict"] = defaultdict
    return namespace


def _in_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def execute_transform(code: str, input_grid: list[list[int]],
                      timeout_sec: int = 5) -> dict:
    """Execute `transform(input_grid)` defined by `code` in the sandbox.

    Returns {"status": "success", "output": grid} or
    {"status": "error"|"timeout", "error": msg}.
    Signal-based timeout: valid in the main thread of any process
    (driver or multiprocessing worker).
    """
    namespace = build_namespace()
    use_signal = _in_main_thread()
    if use_signal:
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(timeout_sec)
    try:
        exec(code, namespace)
        if "transform" not in namespace:
            return {"status": "error", "error": "No 'transform' function defined"}
        result = namespace["transform"](input_grid)
        if result is None:
            return {"status": "error", "error": "transform returned None"}
        if not isinstance(result, list):
            result = np.array(result).tolist()
        return {"status": "success", "output": result}
    except SandboxTimeoutError:
        return {"status": "timeout", "error": f"Execution exceeded {timeout_sec}s"}
    except Exception as e:
        return {"status": "error",
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()}
    finally:
        if use_signal:
            try:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
            except SandboxTimeoutError:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)


def grids_equal(expected: list[list[int]], actual) -> bool:
    """Exact shape + cell equality."""
    try:
        exp = np.array(expected)
        act = np.array(actual)
    except Exception:
        return False
    if exp.shape != act.shape or exp.ndim != 2:
        return False
    return bool((exp == act).all())


def verify_code(code: str, pairs: list[dict], timeout_sec: int = 5) -> dict:
    """Run `code` against every {'input','output'} pair; exact match required.

    Returns {"all_pass": bool, "n_pass": int, "n_pairs": int,
             "fail_reason": str|None}.
    """
    # No pairs means nothing was verified, and "all of zero pairs passed" is
    # vacuously true -- so the plain `n_pass == len(pairs)` below hands back
    # all_pass=True for ANY code on a task whose pairs failed to parse, inflating
    # every solve rate computed from it. An unverifiable task must not read as a
    # solved one; refuse, and let the caller surface it as the data defect it is.
    if not pairs:
        return {"all_pass": False, "n_pass": 0, "n_pairs": 0,
                "fail_reason": "no pairs to verify against"}

    n_pass = 0
    fail_reason = None
    for i, pair in enumerate(pairs):
        res = execute_transform(code, pair["input"], timeout_sec)
        if res["status"] != "success":
            fail_reason = f"pair {i}: {res['status']}: {res.get('error', '')}"
            break
        if not grids_equal(pair["output"], res["output"]):
            fail_reason = f"pair {i}: output mismatch"
            break
        n_pass += 1
    return {
        "all_pass": n_pass == len(pairs),
        "n_pass": n_pass,
        "n_pairs": len(pairs),
        "fail_reason": fail_reason,
    }
