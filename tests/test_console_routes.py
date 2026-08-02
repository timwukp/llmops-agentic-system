"""Derive the console's route shape from the router, and hold the docs to it.

Three documents and one diagram describe the admin console's design, and the load-
bearing claim in all of them is about the AUTH BOUNDARY: reads are public, writes are
authenticated at one chokepoint. That claim was stated as "Cognito on **every** POST"
and it was false -- `/api/login`, `/api/refresh` and `/api/refresh/revoke` are all
handled ABOVE the chokepoint, by design, because requiring a live session to log in
(or to recover one after a reload) is a contradiction.

Being wrong in the flattering direction is the failure mode this repo keeps hitting:
a design read back as a delivered property. So the numbers are read out of
`lambda_function.py` here, and the docs must state what the router actually does:

  * how many handlers exist, split GET / pre-auth POST / authed POST;
  * WHICH POSTs sit above the chokepoint -- an allowlist, so adding a fourth fails
    this test instead of silently widening the unauthenticated surface.

The allowlist is the point. A count alone would pass if a new unauthenticated write
replaced a session route.
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
LAMBDA = REPO / "deploy" / "console" / "lambda_function.py"

#: POSTs that legitimately precede the auth chokepoint: each one MINTS or REVOKES a
#: session rather than acting on the platform. Nothing else may ever be added here
#: without a reviewer reading this comment.
SESSION_POSTS = {"/api/login", "/api/refresh", "/api/refresh/revoke"}

_ROUTE = re.compile(
    r'(?:method == "(?P<m>GET|POST)" and )?path\s*(?:==|\.startswith\()\s*"(?P<p>/api/[^"]*)"')


def _router() -> dict[str, list[str]]:
    """Split every route handler by method and by side of the auth chokepoint."""
    lines = LAMBDA.read_text().split("\n")
    choke = [i for i, l in enumerate(lines)
             if l.strip() == 'if method == "POST":'
             and "_authed_user" in "\n".join(lines[i:i + 10])]
    assert len(choke) == 1, (
        f"expected exactly one POST auth chokepoint in {LAMBDA.name}, found {len(choke)} "
        "at lines " + str([i + 1 for i in choke]) + ". More than one means a POST can be "
        "authorised by a second, divergent path; none means the block was renamed and "
        "this guard has stopped guarding anything.")
    choke = choke[0]

    out = {"get": [], "session_post": [], "authed_post": []}
    for i, l in enumerate(lines):
        m = _ROUTE.search(l)
        if not m:
            continue
        path, meth = m.group("p"), m.group("m")
        if meth == "GET":
            out["get"].append(path)
        elif meth == "POST":
            out["session_post"].append(path)
        elif i > choke:
            out["authed_post"].append(path)
    return out


def test_only_session_routes_are_handled_before_the_auth_chokepoint():
    """The unauthenticated POST surface is exactly the three session routes.

    This is the assertion the prose got wrong. A count would not catch a swap, so the
    set is compared: a new POST above the chokepoint fails here by name.
    """
    r = _router()
    assert set(r["session_post"]) == SESSION_POSTS, (
        f"POSTs handled before the auth chokepoint are {sorted(r['session_post'])}, "
        f"expected exactly {sorted(SESSION_POSTS)}. A POST above the chokepoint is an "
        "UNAUTHENTICATED write unless it only mints or revokes a session. If the new "
        "route really is a session route, add it to SESSION_POSTS and say so in the "
        "console section of both ARCHITECTURE variants.")
    assert r["authed_post"], "no POSTs found below the chokepoint -- the parse is broken"


def test_every_non_session_post_is_below_the_chokepoint():
    """No route may be served on both sides of the boundary.

    `/api/tasks` and `/api/tasks/` are legitimately both GET and POST; the failure
    this catches is a POST path ALSO matched above the chokepoint, which would make
    the authenticated handler dead code reached only when the first one fell through.
    """
    r = _router()
    both = set(r["session_post"]) & set(r["authed_post"])
    assert not both, (
        f"these paths are matched on both sides of the auth chokepoint: {sorted(both)}. "
        "The unauthenticated match wins, so the authed handler below never runs.")


def test_the_docs_state_the_real_route_counts():
    """ARCHITECTURE (both languages) and the diagram must quote the router's numbers.

    The console diagram said "26 routes" while the router had 30 handlers, and the
    prose said "every POST" while three sit above the chokepoint. Both are checkable
    facts, so they are checked rather than proofread.
    """
    r = _router()
    total = len(r["get"]) + len(r["session_post"]) + len(r["authed_post"])
    svg = (REPO / "docs" / "architecture-console.svg").read_text()
    assert f"{total} route handlers" in svg, (
        f"the console diagram no longer states the handler count ({total}); it is "
        "generated by docs/gen_architecture_svg.py, so fix it there and regenerate.")

    # The falsified phrasing must not come back in any language. Scope is the
    # PARAGRAPH, and a paragraph that reports the claim as having been WRONG is exempt:
    # the corrected §13 quotes the old wording in order to name the mistake, and a
    # blanket substring ban would force the docs to stop recording it -- the opposite
    # of the fix. Same exemption shape as test_no_doc_claims_a_file_that_does_not_exist.
    retracted = ("false", "wrong", "earlier version", "previous version", "mistake",
                 "先前版本", "寫錯", "錯誤", "並不是")
    banned = ("every POST is authenticated", "Cognito on every POST",
              "每一條 POST 都在同一個地方被認證", "每一條** POST 都過 Cognito")
    for doc in ("ARCHITECTURE.md", "ARCHITECTURE.zh-TW.md"):
        text = (REPO / "docs" / doc).read_text()
        for para in re.split(r"\n\s*\n", text):
            if any(mark in para for mark in retracted):
                continue
            for phrase in banned:
                assert phrase not in para, (
                    f"{doc} asserts {phrase!r} as current fact, but "
                    f"{sorted(SESSION_POSTS)} are handled above the chokepoint by "
                    "design. Say 'every POST except the session routes' and name them.")
        # ...and the exception must be named, not merely omitted.
        assert "/api/login" in text and "/api/refresh" in text, (
            f"{doc} describes the auth chokepoint without naming the session routes that "
            "precede it. Omitting them reads as 'there are none', which is the claim "
            "that was wrong.")


def test_the_tabs_in_the_docs_match_the_frontend():
    """The "8 tabs" claim comes from the HTML, not from memory."""
    html = (REPO / "deploy" / "console" / "frontend.html").read_text()
    tabs = sorted(set(re.findall(r'data-tab="([a-z-]+)"', html)))
    panels = sorted(set(re.findall(r'data-tab-panel="([a-z-]+)"', html)))
    # "all" is a panel marker for content shown on every tab, not a tab of its own.
    assert tabs == sorted(p for p in panels if p != "all"), (
        f"tabs {tabs} and panels {panels} disagree -- a tab with no panel renders blank, "
        "a panel with no tab is unreachable.")
    svg = (REPO / "docs" / "architecture-console.svg").read_text()
    assert f"{len(tabs)} tabs" in svg, (
        f"the console diagram does not state the real tab count ({len(tabs)}: {tabs})")
    for doc in ("ARCHITECTURE.md", "ARCHITECTURE.zh-TW.md"):
        text = (REPO / "docs" / doc).read_text()
        assert re.search(rf"\b{len(tabs)}\b\s*(?:tabs|個(?:分)?頁|頁籤)", text), (
            f"docs/{doc} does not state the tab count derived from the frontend "
            f"({len(tabs)})")


#: Env vars read by the Lambda that are NOT part of its configuration contract: AWS
#: injects them, so documenting them as knobs would be misleading.
_AWS_INJECTED = {"AWS_REGION", "AWS_LAMBDA_FUNCTION_NAME"}


def test_the_console_readme_documents_every_env_var_it_reads():
    """deploy/console/README.md's env contract must cover what the handler reads.

    The table listed 11 of 28 vars, and the 17 missing ones included every knob that
    governs the cost gate -- APPROVAL_LIMIT_USD, CUMULATIVE_LIMIT_USD, BUDGET_MODE,
    APPROVER_GROUP, APPROVAL_KEY. An operator reading the contract would conclude the
    $2000 gate was hardcoded, i.e. that they could not change it and did not need to
    check it. An undocumented knob with a default is worse than a missing feature: it
    is a live setting nobody knows is settable.

    ACCOUNT_ID is documented by the prose line about STS resolution rather than a row.
    """
    src = LAMBDA.read_text()
    read = set(re.findall(r'os\.environ(?:\.get)?[(\[]\s*"([A-Z_]+)"', src)) - _AWS_INJECTED
    readme = (REPO / "deploy" / "console" / "README.md").read_text()
    # ACCOUNT_ID is an override for a value resolved from STS, not a knob to set; the
    # README says so in prose. Accepting the bare name (not a backticked row) keeps that
    # sentence sufficient while still failing if it is deleted.
    undocumented = sorted(v for v in read
                          if f"`{v}`" not in readme
                          and not (v == "ACCOUNT_ID" and "Account ID is resolved at runtime"
                                   in readme))
    assert not undocumented, (
        f"these env vars are read by lambda_function.py but absent from the README's env "
        f"contract: {undocumented}. Each has a default, so each is a live setting an "
        "operator cannot discover.")


def test_the_console_readme_tab_table_matches_the_frontend():
    """Every nav tab must appear in the README's tab table, by label.

    The table described five tabs while the nav had eight -- and the three it omitted
    (Architecture, Tasks, Cost) include the customer-facing half of the product. A
    reader would not learn the Tasks plane exists.
    """
    html = (REPO / "deploy" / "console" / "frontend.html").read_text()
    labels = re.findall(r'data-tab="[a-z-]+"[^>]*>\s*([A-Za-z][A-Za-z ]*)', html)
    labels = [l.strip() for l in labels if l.strip()]
    assert len(labels) >= 8, f"parsed only {labels} from the nav -- the parse is broken"
    readme = (REPO / "deploy" / "console" / "README.md").read_text()
    table = readme.split("## Tabs", 1)[1].split("##", 1)[0]
    missing = [l for l in labels if f"| {l} |" not in table]
    assert not missing, (
        f"these tabs exist in frontend.html but are not rows in the README tab table: "
        f"{missing}")
