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
    """Split every route handler by method and by side of the auth chokepoints.

    Two chokepoints, not one, because there are two things being gated and they are not
    the same question. The POST gate asks "may you act on the platform"; the consult gate
    asks "may you read a customer's engagement". Keying auth on the METHOD alone is what
    left four consult READS anonymous, so the GET list is split here the same way the
    POST list is -- a GET is only reported as `public_get` if nothing authenticated it.
    """
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

    # The consult-read gate: `if method == "GET" and _is_consult_path(path):` followed by
    # an _authed_user call. Located the same way, and asserted to exist -- a guard that
    # silently found no gate would classify every consult read as public and pass.
    cons = [i for i, l in enumerate(lines)
            if "_is_consult_path(path)" in l and 'method == "GET"' in l
            and "_authed_user" in "\n".join(lines[i:i + 6])]
    assert len(cons) == 1, (
        f"expected exactly one authenticated consult-GET gate in {LAMBDA.name}, found "
        f"{len(cons)} at lines {[i + 1 for i in cons]}. Zero means the consult plane's "
        "reads are anonymous again -- GET /api/tasks/{id} returns the customer's whole "
        "transcript and /approval returns cognito_sub and source_ip.")
    cons = cons[0]
    # Where that block's body ends: the first line at or below its own indent level.
    cons_indent = len(lines[cons]) - len(lines[cons].lstrip())
    cons_end = len(lines)
    for i in range(cons + 1, len(lines)):
        if lines[i].strip() and (len(lines[i]) - len(lines[i].lstrip())) <= cons_indent:
            cons_end = i
            break

    out = {"public_get": [], "authed_get": [], "session_post": [], "authed_post": []}
    for i, l in enumerate(lines):
        m = _ROUTE.search(l)
        if not m:
            continue
        path, meth = m.group("p"), m.group("m")
        if cons < i < cons_end:
            out["authed_get"].append(path)
        elif meth == "GET":
            out["public_get"].append(path)
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


def test_no_route_on_the_consult_prefix_is_ever_anonymous():
    """The customer plane is authenticated on BOTH methods, derived from the prefix.

    This is the assertion the old guard could not make, because it only ever asked which
    POSTs were unauthenticated. Four consult GETs -- /api/tasks, /api/tasks/{id} and that
    thread's /approval and /readiness panels -- were served anonymously on a public API
    Gateway URL for the platform's whole life. The doc's boast that "adding a route cannot
    accidentally add an unauthenticated write" was true, and it was the wrong claim to be
    checking: nothing said an unauthenticated READ of a customer's transcript was possible.

    Stated over the PREFIX rather than the four known paths, so the fifth panel added to a
    thread is covered by arithmetic instead of by whoever remembers this file exists.
    """
    r = _router()
    anonymous = sorted(p for p in r["public_get"] + r["session_post"]
                       if p == "/api/tasks" or p.startswith("/api/tasks"))
    assert not anonymous, (
        f"these consult-plane routes are served without authentication: {anonymous}. "
        "Everything under /api/tasks is one customer's engagement -- the thread item is "
        "their transcript and the approval record carries cognito_sub and source_ip. "
        "Move the route inside the authenticated consult block; do not add an exception.")
    assert r["authed_get"], (
        "no GETs were found inside the authenticated consult block -- either the block "
        "was removed or the parse broke, and both report a leak as clean.")


def test_the_consult_gate_is_a_prefix_not_a_list_of_paths():
    """`_is_consult_path` must match by prefix, and the router must use it.

    An enumeration of the four leaking routes would have closed today's hole and left the
    mechanism intact: route five arrives anonymous the same way, and this test would still
    pass. So the shape is pinned, not just the outcome.
    """
    src = LAMBDA.read_text()
    fn = src.split("def _is_consult_path(", 1)[1].split("\ndef ", 1)[0]
    assert "startswith(CONSULT_PREFIX" in fn, (
        "_is_consult_path no longer matches by prefix. A list of known task routes leaves "
        "the next one added to the thread anonymous, which is the bug, not the symptom.")
    # And every consult read must go through it, rather than re-deriving the membership
    # test at the router -- two copies of "what counts as consult" is one copy too many.
    assert src.count("_is_consult_path(path)") >= 1, (
        "the router does not consult _is_consult_path; a predicate nothing calls is not "
        "a gate.")


def test_a_consult_read_checks_the_group_and_not_only_the_token():
    """Authentication is not authorisation: the consult reads need the group check too.

    The write side has always called `_user_may_task` (DS_GROUP or APPROVER_GROUP). If the
    reads only checked that a token was valid, then every operator with a dashboard login
    -- including one provisioned purely to watch the Pipeline tab -- could read every
    customer transcript in the account. 401 and 403 also have to stay distinct, or an
    approver who is simply in the wrong group gets told their session expired and loops
    through a re-login that cannot help.
    """
    src = LAMBDA.read_text()
    block = src.split('if method == "GET" and _is_consult_path(path):', 1)[1]
    block = block.split("\n        raw = event.get", 1)[0]
    assert "_authed_user(headers)" in block, "the consult read gate does not authenticate"
    assert "_user_may_task(user)" in block, (
        "the consult read gate authenticates but does not check group membership, so any "
        "valid dashboard token can read every customer's transcript.")
    assert "401" in block and "403" in block, (
        "the consult read gate does not distinguish 401 from 403. The frontend routes them "
        "differently on purpose: 401 clears the session, 403 must not.")


def test_the_console_ui_sends_credentials_on_every_consult_read():
    """The four consult GETs in frontend.html must go through the authed helper.

    A server-side gate plus a bare `fetch` on the page is not a working feature: the whole
    Tasks tab 401s and the operator sees an empty rail. Derived by finding every fetch of
    an /api/tasks URL and requiring none of them to be raw.
    """
    html = (REPO / "deploy" / "console" / "frontend.html").read_text()
    raw = re.findall(r'fetch\(API\+"(/api/tasks[^"]*)"', html)
    raw += re.findall(r'fetch\(API\+"(/api/tasks)"', html)
    assert not raw, (
        f"these consult reads use a raw fetch with no Authorization header: {sorted(set(raw))}. "
        "They now return 401. Use authGet(), which attaches the token and reports refusal "
        "as {denied: reason} so the panel can say which wall it hit.")
    assert "async function authGet(" in html, "authGet is missing from the console UI"
    # A denial must be NAMED. The rail's empty state says "no consultations yet", which is
    # a different fact from "you are not signed in" and reads as reassuring when wrong.
    assert html.count("d.denied") + html.count("t.denied") >= 4, (
        "fewer than four consult call sites handle a {denied} result; one that ignores it "
        "renders a refusal as an empty panel.")


def test_the_docs_state_the_real_route_counts():
    """ARCHITECTURE (both languages) and the diagram must quote the router's numbers.

    The console diagram said "26 routes" while the router had 30 handlers, and the
    prose said "every POST" while three sit above the chokepoint. Both are checkable
    facts, so they are checked rather than proofread.
    """
    r = _router()
    total = (len(r["public_get"]) + len(r["authed_get"])
             + len(r["session_post"]) + len(r["authed_post"]))
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
