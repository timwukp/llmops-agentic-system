"""Keep the numbers the docs assert in step with the numbers the repo produces.

docs/TEST_RESULTS.md is evidence: it prints a pass count next to the exact command
that produced it, so a reader can re-run it and check. That only works while the
number is true. It has now gone stale twice -- 274 was correct when written and became
a false claim as tests were added -- and nothing failed to say so, because a number in
a markdown table is invisible to the suite it describes.

The count comes from pytest's own collector, run as a subprocess with
``--collect-only``. The first draft of this file counted ``def test_*`` from the AST
instead, and was wrong by 11: three files use ``@pytest.mark.parametrize``, so one
function is several tests. Reimplementing collection means maintaining a second,
subtly different definition of "a test" -- and a guard that is wrong for its own
reasons is worse than no guard, because it fails without telling you which side drifted.
A subprocess avoids the recursion of a suite invoking itself.

The bilingual pair matters as much as the number: docs/TEST_RESULTS.md and its .zh-TW
counterpart are the same evidence for two audiences, and a count updated in one
language only is worse than one stale in both -- it reads as verified in whichever the
reader happens to open.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = (REPO / "docs" / "TEST_RESULTS.md", REPO / "docs" / "TEST_RESULTS.zh-TW.md")

#: Counts in the docs are written as **N passed** / **N/N passed** / **N/N 通過**.
_CLAIM = re.compile(r"\*\*(\d+)(?:/(\d+))?\s*(?:passed|通過)\*\*")


def _collected_test_count() -> int:
    """Ask pytest how many tests it collects, so there is one definition of "a test"."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(REPO / "tests"), "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO, timeout=300)
    m = re.search(r"^(\d+) tests? collected", proc.stdout, re.MULTILINE)
    assert m, ("could not read a collection count out of pytest --collect-only; "
               f"rc={proc.returncode}\nstdout tail:\n{proc.stdout[-2000:]}"
               f"\nstderr tail:\n{proc.stderr[-1000:]}")
    return int(m.group(1))


def test_documented_test_counts_match_the_real_suite():
    """Every **N passed** claim in TEST_RESULTS must equal what pytest collects.

    Note the count includes the tests in this file, which is correct: the docs quote a
    total for ``pytest tests/ -q``, and these run under exactly that command.
    """
    expected = _collected_test_count()
    wrong = []
    for doc in DOCS:
        assert doc.exists(), f"{doc} is referenced as evidence but missing"
        for m in _CLAIM.finditer(doc.read_text()):
            claimed = int(m.group(1))
            if m.group(2) is not None and int(m.group(2)) != claimed:
                wrong.append(f"{doc.name}: {m.group(0)!r} is not of the form N/N")
            if claimed != expected:
                wrong.append(f"{doc.name}: claims {claimed}, pytest collects {expected}")
    assert not wrong, (
        "documented test counts have drifted from the suite: " + "; ".join(wrong)
        + f". Re-run the documented command and update all {len(DOCS)} language "
        "variants in the same commit.")


def test_both_language_variants_make_the_same_count_claims():
    """A count fixed in one language only reads as verified in whichever is opened."""
    per_doc = {doc.name: sorted(int(m.group(1)) for m in _CLAIM.finditer(doc.read_text()))
               for doc in DOCS}
    first, second = per_doc.values()
    assert first == second, (
        f"the bilingual evidence pair disagrees about its own numbers: {per_doc}. "
        "Same evidence, two audiences -- update them together.")
