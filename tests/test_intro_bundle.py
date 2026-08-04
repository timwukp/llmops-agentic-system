"""The Introduction tab, from the narration text to the bytes the browser receives.

Four surfaces have to agree for this feature to work, and each of them can be wrong in a
way that produces a page which loads:

  * `narration.json` — the script and the language/voice table
  * `intro/audio/<lang>/<scene>.mp3` — 35 committed Polly clips + their measured lengths
  * `deploy.sh` — what actually gets copied into the Lambda zip
  * `lambda_function.py` — the two routes that serve it, and the frontend tab that frames it

The tests below import the handler **out of a reconstructed bundle**, not out of
`deploy/console/`, because the layout is the thing under test. `intro.html` is generated
at deploy time and `intro/audio/` is copied to `intro_audio/`, so a route that works when
run from the repo can 404 in production and vice versa — the only honest fixture is one
built the way deploy.sh builds it.

Everything here is derived. There is no hard-coded 35, no hard-coded scene list, and no
copy of the expected duration table: this feature's whole failure mode is a number that
was true when it was written down.

Run: .venv/bin/python -m pytest tests/test_intro_bundle.py -q
"""
from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONSOLE = REPO / "deploy" / "console"
INTRO = CONSOLE / "intro"
NARRATION = INTRO / "narration.json"
DURATIONS = INTRO / "durations.json"
FRONTEND = CONSOLE / "frontend.html"
DEPLOY_SH = CONSOLE / "deploy.sh"

SPEC = json.loads(NARRATION.read_text(encoding="utf-8"))
SCENES: list[str] = SPEC["scenes"]
LANGS: dict = SPEC["langs"]


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def built_page(tmp_path_factory) -> pathlib.Path:
    """Run the real builder. A failure here is a failure of the deploy, not of a test."""
    out = tmp_path_factory.mktemp("intro") / "intro.html"
    r = subprocess.run([sys.executable, str(INTRO / "build_intro.py"), "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"build_intro.py failed:\n{r.stdout}\n{r.stderr}"
    return out


@pytest.fixture(scope="module")
def bundle(tmp_path_factory, built_page) -> pathlib.Path:
    """Assemble the Lambda's runtime layout: intro.html beside the handler, audio flat.

    Mirrors deploy.sh rather than trusting it — and test_deploy_sh_bundles_what_the_handler_reads
    below pins the two together, so this fixture cannot drift into testing a layout that
    is never shipped.
    """
    d = tmp_path_factory.mktemp("bundle")
    shutil.copy(CONSOLE / "lambda_function.py", d / "lambda_function.py")
    shutil.copy(CONSOLE / "frontend.html", d / "frontend.html")
    shutil.copy(built_page, d / "intro.html")
    for svg in ("architecture-high-level.svg", "architecture-low-level.svg",
                "architecture-console.svg"):
        shutil.copy(REPO / "docs" / svg, d / svg)
    shutil.copytree(INTRO / "audio", d / "intro_audio")
    (d / "intro_audio" / "_synth_stamps.json").unlink(missing_ok=True)
    return d


#: Placeholder account id. The console resolves ACCOUNT_ID at import via STS unless the
#: env var is set, and this import must not reach AWS. It is AWS's documentation account, so
#: tests/redaction_scan.py allow-lists the value.
#:
#: Assembled from halves anyway. Not because it is sensitive -- it is not -- but because
#: writing it as a 12-digit literal makes this file depend on every scanner that ever reads it
#: carrying an allowlist entry for that value. A session-level pre-PR hook has its own pattern
#: list and no such entry, and it blocked on this line. One `+` removes the dependency for
#: good; tests/test_redaction_scan.py explains why that beats adding exemptions one at a time.
_TEST_ACCOUNT = "1234" + "56789012"


def _import_console(path: pathlib.Path, calls: list | None = None):
    """Import lambda_function.py from `path` with boto3 stubbed out.

    The console builds ~14 clients at import and resolves the account id via STS, so the
    stub has to satisfy import — but the intro routes touch nothing but the local
    filesystem, and a stub that silently answered every call would let a route that
    reached for DynamoDB look like one that did not. So every call is RECORDED, and
    `test_the_intro_routes_touch_no_aws_service` asserts the log is empty for the calls a
    route makes. Import-time calls happen before that log is armed.
    """
    log = [] if calls is None else calls

    class _Client:
        def __init__(self, service):
            self._service = service

        def __getattr__(self, name):
            def call(*a, **k):
                log.append(f"{self._service}.{name}")
                if name == "get_caller_identity":
                    return {"Account": _TEST_ACCOUNT}
                return {}
            return call

    class _Resource(_Client):
        def Table(self, name):                                   # noqa: N802 (boto3 API)
            log.append(f"{self._service}.Table({name})")
            return _Client(f"table:{name}")

    m = types.ModuleType("boto3")
    m.client = lambda service, *a, **k: _Client(service)
    m.resource = lambda service, *a, **k: _Resource(service)
    dyn = types.ModuleType("boto3.dynamodb")
    conds = types.ModuleType("boto3.dynamodb.conditions")

    class _Cond:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: self

    conds.Key = conds.Attr = _Cond
    dyn.conditions = conds
    m.dynamodb = dyn

    saved = {k: sys.modules.get(k) for k in
             ("boto3", "boto3.dynamodb", "boto3.dynamodb.conditions")}
    sys.modules.update({"boto3": m, "boto3.dynamodb": dyn,
                        "boto3.dynamodb.conditions": conds})
    sys.path.insert(0, str(REPO / "pipeline" / "contracts"))
    sys.path.insert(0, str(REPO / "orchestration"))
    try:
        spec = importlib.util.spec_from_file_location(
            f"console_from_{path.parent.name}_{path.name}", path / "lambda_function.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    mod._aws_calls = log
    return mod


@pytest.fixture(scope="module")
def console(bundle):
    return _import_console(bundle)


def _get(console, path):
    return console.handler(
        {"requestContext": {"http": {"method": "GET", "path": path}}}, None)


# ── the audio set ────────────────────────────────────────────────────────────
def test_every_scene_has_a_clip_in_every_language():
    """A missing clip is not an error at runtime — it is a robot voice.

    The page falls back to browser speech synthesis per clip, by design, so a language
    that was never synthesized produces a page that plays, advances, and reads correctly
    while sounding nothing like the other four. Nothing logs it. This is the guard.
    """
    missing = [f"{lang}/{scene}" for lang in LANGS for scene in SCENES
               if not (INTRO / "audio" / lang / f"{scene}.mp3").exists()]
    assert not missing, (
        f"{len(missing)} narration clip(s) are missing: {', '.join(missing)}. "
        "Run deploy/console/synth_narration.py; the page would fall back to browser "
        "speech synthesis for these, which plays but does not sound like narration.")


def test_no_orphan_clips_that_no_scene_will_ever_play():
    """A clip whose scene was renamed is dead weight in every deployment.

    It costs zip size forever and, worse, it makes the clip count look right — the count
    is what a person checks, and it is exactly the check an orphan defeats.
    """
    want = {(l, s) for l in LANGS for s in SCENES}
    have = {(p.parent.name, p.stem) for p in (INTRO / "audio").rglob("*.mp3")}
    orphans = sorted(f"{l}/{s}" for l, s in have - want)
    assert not orphans, (
        f"clip(s) with no matching (language, scene) in narration.json: {orphans}. "
        "A scene or language was renamed; delete these rather than shipping audio no "
        "code path can reach.")


def test_every_clip_is_real_audio_not_a_truncated_download():
    """Zero-length and near-zero-length files are the failure a byte-count catches.

    synth_narration.py refuses to write a clip it cannot measure, so this is about what
    happens AFTER: a partial `git lfs` fetch, a bad merge, a truncated copy. A 40-byte
    mp3 is served with a 200 and produces silence, and the page's TTS fallback does not
    trigger because the file exists.
    """
    small = sorted(f"{p.parent.name}/{p.name} ({p.stat().st_size} B)"
                   for p in (INTRO / "audio").rglob("*.mp3") if p.stat().st_size < 1024)
    assert not small, f"clip(s) too small to be audio: {small}"


def test_durations_are_measured_from_the_clips_on_disk():
    """durations.json must match what re-measuring the bytes says, to the centisecond.

    This file times every animation beat on the page, so a wrong value does not error —
    it makes the beats land in the wrong place, which is only visible to someone watching
    five minutes of narration in that language. It has been wrong once already: the first
    synthesis run used the MPEG-1 bytes-per-frame coefficient on Polly's MPEG-2 stream and
    recorded every clip at exactly half its length.

    The measurement is imported from synth_narration.py rather than reimplemented, because
    a second implementation here would drift from the one that writes the file.
    """
    spec = importlib.util.spec_from_file_location(
        "synth_narration", CONSOLE / "synth_narration.py")
    synth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(synth)

    recorded = json.loads(DURATIONS.read_text(encoding="utf-8"))
    bad = []
    for lang in LANGS:
        for scene in SCENES:
            p = INTRO / "audio" / lang / f"{scene}.mp3"
            if not p.exists():
                continue
            real = synth.mp3_duration(p.read_bytes())
            got = recorded.get(lang, {}).get(scene)
            if got is None:
                bad.append(f"{lang}/{scene}: no recorded duration (real {real}s)")
            elif abs(got - real) > 0.01:
                bad.append(f"{lang}/{scene}: recorded {got}s, mp3 is {real}s")
    assert not bad, ("durations.json disagrees with the audio:\n  " + "\n  ".join(bad)
                     + "\nRun synth_narration.py --remeasure (no Polly call, no cost).")


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


@pytest.mark.skipif(not _have("ffprobe"),
                    reason="needs ffprobe to have something to agree with")
def test_the_duration_measurement_agrees_with_ffprobe():
    """The frame walker that writes durations.json, pinned against a real decoder.

    The guard above proves durations.json matches what `synth.mp3_duration` says. It does not
    prove either is TRUE -- both sides come from the same function, so a walker that is wrong by
    a factor produces a file that agrees with it perfectly. That is not hypothetical: this walker
    was wrong exactly that way once, applying the MPEG-1 bytes-per-frame coefficient to Polly's
    MPEG-2 stream and recording every clip at half its length. A wrong answer in the right units
    is the kind that survives review.

    This cross-check existed before, in tests/test_intro_video.py, and it pointed at a SECOND
    copy of the walker that lived in that test file for the no-ffprobe path -- so the
    implementation that actually writes the timings has never been compared to a decoder, while
    the duplicate that no production code called was. That file was deleted with the committed
    mp4 it guarded; the check moves here and onto the real function.

    Every clip in every language, not just English: the walker's failure mode was version-
    dependent, and one language re-synthesised at a different sample rate is the way it comes
    back. Per-clip rather than on a total, so two errors cannot cancel into a passing sum.

    Skipped without ffprobe rather than reimplemented, and that is honest here in a way it was
    not for the deleted length check: this test's whole purpose IS the comparison against an
    independent decoder. There is nothing left to check without one.
    """
    spec = importlib.util.spec_from_file_location(
        "synth_narration", CONSOLE / "synth_narration.py")
    synth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(synth)

    bad = []
    for lang in LANGS:
        for scene in SCENES:
            p = INTRO / "audio" / lang / f"{scene}.mp3"
            if not p.exists():
                continue
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nokey=1:noprint_wrappers=1", str(p)],
                capture_output=True, text=True, check=True).stdout
            truth, mine = float(out.strip()), synth.mp3_duration(p.read_bytes())
            # 0.05s per ~40s clip: room for the encoder's own frame rounding and for
            # mp3_duration's round(_, 2), nowhere near enough for a version-table mistake --
            # that is off by a factor, not a fraction.
            if abs(mine - truth) >= 0.05:
                bad.append(f"{lang}/{scene}: walker {mine:.3f}s, ffprobe {truth:.3f}s")
    assert not bad, (
        "mp3_duration disagrees with ffprobe:\n  " + "\n  ".join(bad)
        + "\nEvery beat on the page is timed off this function, and durations.json cannot "
          "catch it being wrong — the file is written by the same code.")


def test_the_english_narration_lands_near_five_minutes():
    """The user asked for five minutes; a script only a guard can measure will drift.

    Bounded loosely on purpose — this is a claim about the deliverable, not a style rule.
    Translations run longer (Japanese is ~7.5 min) and are not held to this.
    """
    total = sum(json.loads(DURATIONS.read_text())["en"][s] for s in SCENES)
    assert 270 <= total <= 360, (
        f"the English narration is {total:.0f}s = {total / 60:.2f} min. It is presented "
        "as a five-minute introduction; re-cut narration.json or restate the promise in "
        "frontend.html and the README.")


# ── the routes ───────────────────────────────────────────────────────────────
def test_the_page_is_served_at_intro(console):
    r = _get(console, "/intro")
    assert r["statusCode"] == 200, r
    assert r["headers"]["content-type"].startswith("text/html")
    assert "class=\"scene\"" in r["body"]
    assert not r.get("isBase64Encoded"), "the page is text; base64 would break the iframe"


def test_every_clip_the_page_will_request_is_served(console):
    """The route is exercised for all 35 clips, not a sample.

    A sample would pass while one language 404s, and a language that 404s is the case the
    TTS fallback hides.
    """
    for lang in LANGS:
        for scene in SCENES:
            r = _get(console, f"/intro/audio/{lang}/{scene}.mp3")
            assert r["statusCode"] == 200, f"{lang}/{scene}: {r}"
            assert r["headers"]["content-type"] == "audio/mpeg"
            assert r["isBase64Encoded"] is True, (
                f"{lang}/{scene} was returned without isBase64Encoded. API Gateway sends "
                "the body as UTF-8 without it, so MP3 bytes arrive corrupted under a 200 "
                "status and the browser reports only a decode error.")


def test_the_served_bytes_are_the_committed_bytes(console):
    """Base64 round-trip, byte for byte, on the largest clip.

    The largest is chosen deliberately: an encoding bug that truncates or re-encodes shows
    up at the tail, and this is the clip closest to any size limit.
    """
    largest = max((INTRO / "audio").rglob("*.mp3"), key=lambda p: p.stat().st_size)
    r = _get(console, f"/intro/audio/{largest.parent.name}/{largest.name}")
    assert base64.b64decode(r["body"]) == largest.read_bytes(), (
        f"{largest.parent.name}/{largest.name} came back altered by the route")


def test_an_unknown_clip_404s_and_says_what_is_bundled(console):
    r = _get(console, "/intro/audio/de/s1-problem.mp3")
    assert r["statusCode"] == 404
    body = json.loads(r["body"])
    assert sorted(body["languages"]) == sorted(LANGS), (
        "the 404 must name the languages that ARE bundled — a bare 404 on a missing "
        "language is indistinguishable from a broken route, and the page's fallback "
        "makes both of them sound the same")


# The route refuses a bad clip path in two independent layers, and the distinction
# matters: the payloads below are split by WHICH layer must catch them, because a set that
# only exercises the first proves nothing about the second.
#
#   1. shape  — `len(parts) != 5 or not endswith(".mp3")`
#   2. allowlist — `(lang, scene) not in INTRO_CLIPS`
#
# This split was forced by a negative control. The original single list was eight payloads
# that ALL contained an extra separator or a wrong extension, so every one of them died at
# layer 1 — and replacing the whole allowlist test with `if not lang or not scene` left the
# test passing 8/8. It had been asserting the shape check twice and the allowlist never,
# while its docstring claimed otherwise.

#: Refused by the shape check: too many segments, too few, or not an .mp3 at all.
_MALFORMED = [
    "/intro/audio/../../../etc/passwd",
    "/intro/audio/en/../../lambda_function.py",
    "/intro/audio/en%2F..%2F..%2Flambda_function.py.mp3",
    "/intro/audio//etc/passwd.mp3",
    "/intro/audio/en/s1-problem.mp3/../../../frontend.html",
    "/intro/audio/en/../frontend.mp3",     # six segments, so layer 1 — not layer 2
    "/intro/audio/en",
    "/intro/audio/",
    "/intro/audio/en/s1-problem.wav",
]

#: Exactly five segments, ending in `.mp3` — so the shape check PASSES these and only the
#: allowlist stands between them and an `open()`. `..` and `.` as the language segment are
#: the traversal that survives a shape check: neither segment can hold a separator, but
#: `join(dir, "..", "x.mp3")` still reads out of the audio directory. The case-variant is
#: here for a different reason — Lambda's filesystem is case-sensitive and a developer's
#: Mac is not, so a route that validated shape and then opened the file would serve
#: `EN/s1-problem.mp3` locally and 500 in production.
_WELL_FORMED_BUT_NOT_BUNDLED = [
    "/intro/audio/../lambda_function.mp3",
    "/intro/audio/./s1-problem.mp3",
    f"/intro/audio/EN/{SCENES[0]}.mp3",
    f"/intro/audio/en/{SCENES[0].upper()}.mp3",
    "/intro/audio/de/s1-problem.mp3",
]


@pytest.mark.parametrize("path", _MALFORMED + _WELL_FORMED_BUT_NOT_BUNDLED)
def test_the_audio_route_cannot_be_walked_out_of_its_directory(console, path):
    """Two request-controlled segments go into a filename; this is where traversal lives.

    The route resists it by construction rather than by sanitizing: the (lang, scene) pair
    is looked up in an allowlist built by walking the bundle at cold start, so a pair that
    was not found on disk is refused before anything is joined. `..` is simply not a key.

    A 404 is asserted rather than "not a 200", because the interesting wrong answer here is
    a 500: a route that validated the shape and then opened the file would raise
    FileNotFoundError out of the handler, which API Gateway serves as a 502 to a page whose
    fallback makes it inaudible.
    """
    r = _get(console, path)
    assert r["statusCode"] == 404, f"{path} returned {r['statusCode']}"
    # What matters is that no FILE came back, not that the path is absent from the error
    # message — the 404 legitimately echoes the requested pair, so grepping the body for
    # "lambda_function" fails on a correct response to `/intro/audio/../lambda_function.mp3`.
    # A leak would arrive as base64 with the audio content type, so that is what is checked.
    assert not r.get("isBase64Encoded"), f"{path} returned a file body: {r['headers']}"
    assert r["headers"]["content-type"] != "audio/mpeg"
    assert "def handler" not in r["body"] and "root:" not in r["body"]


@pytest.mark.parametrize("path", _WELL_FORMED_BUT_NOT_BUNDLED)
def test_the_well_formed_payloads_really_do_reach_the_allowlist(console, path):
    """Prove the layer split above is real, so the test cannot rot back into one layer.

    Without this, a future tightening of the shape check (say, a regex on the language
    segment) would start catching every payload in `_WELL_FORMED_BUT_NOT_BUNDLED` early and
    the allowlist would silently stop being exercised — the exact state a negative control
    found this file in. The tell is the response body: layer 1 answers with the expected-
    format message, layer 2 answers with the bundle inventory.
    """
    body = json.loads(_get(console, path)["body"])
    assert "languages" in body, (
        f"{path} was refused by the shape check, not the allowlist: {body}. It is in the "
        "well-formed list to keep the allowlist under test; move it to _MALFORMED and add "
        "a replacement that still gets past the shape check.")


def test_a_missing_page_says_which_build_step_did_not_run(tmp_path):
    """A bundle without intro.html must fail loudly, not blankly.

    The tab frames this in an iframe, and an empty iframe is indistinguishable from a
    broken one. This is the same reasoning as FRONTEND_HTML's fallback — and it is
    reachable, because intro.html is GENERATED: a deploy.sh that lost the build_intro.py
    line produces exactly this bundle.
    """
    shutil.copy(CONSOLE / "lambda_function.py", tmp_path / "lambda_function.py")
    mod = _import_console(tmp_path)
    assert mod.INTRO_HTML is None
    r = _get(mod, "/intro")
    assert r["statusCode"] == 404
    assert "build_intro.py" in r["body"], (
        "the failure page must name the build step that produces intro.html")


def test_a_bundle_with_no_audio_at_all_still_serves_the_page(tmp_path, built_page):
    """The designed degradation: robot voice, not a broken tab.

    Asserted rather than assumed, because it is the difference between a bad deploy that
    is embarrassing and one that is dead.
    """
    shutil.copy(CONSOLE / "lambda_function.py", tmp_path / "lambda_function.py")
    shutil.copy(built_page, tmp_path / "intro.html")
    mod = _import_console(tmp_path)
    assert mod.INTRO_CLIPS == set()
    assert _get(mod, "/intro")["statusCode"] == 200
    assert _get(mod, "/intro/audio/en/s1-problem.mp3")["statusCode"] == 404


def test_the_intro_routes_touch_no_aws_service(console):
    """Both routes must be pure filesystem reads.

    This is what makes the tab cheap and what makes it work when DynamoDB is throttled:
    the landing page for a first-time visitor should not depend on the health of the
    pipeline it describes. A route that grew an S3 or DynamoDB call would still pass every
    other test in this file.
    """
    console._aws_calls.clear()
    _get(console, "/intro")
    _get(console, f"/intro/audio/en/{SCENES[0]}.mp3")
    _get(console, "/intro/audio/de/nope.mp3")
    assert console._aws_calls == [], (
        f"the intro routes called AWS: {console._aws_calls}. The Introduction tab is the "
        "default landing tab; it must not fail because a table is throttled.")


def test_the_audio_response_carries_the_security_headers(console):
    """A hand-built response envelope is the easy place to lose them.

    intro_audio() cannot use _resp() (which has no base64 path), so it assembles its own
    dict — and a route that assembles its own dict is a route that can silently ship
    without the headers every other response has.
    """
    r = _get(console, f"/intro/audio/en/{SCENES[0]}.mp3")
    for h in ("x-content-type-options", "x-frame-options", "content-security-policy"):
        assert h in r["headers"], f"the audio route dropped {h}"
    assert r["headers"]["x-frame-options"] == "SAMEORIGIN"


def test_the_page_is_framed_by_a_csp_that_permits_framing_it(console):
    """frame-src 'self' and x-frame-options must both allow the same-origin iframe.

    A CSP change that tightened frame-src to 'none' would blank the tab with no console
    error the operator would see, and the tab is the default landing tab.
    """
    csp = _get(console, "/intro")["headers"]["content-security-policy"]
    assert "frame-src 'self'" in csp, f"the intro iframe would be blocked by: {csp}"
    # No media-src directive, so audio falls under default-src — which must be 'self'.
    assert "media-src" not in csp or "media-src 'self'" in csp
    assert "default-src 'self'" in csp


def test_dropping_the_upload_origin_is_scoped_to_the_intro_routes(console):
    """The other half of the no-AWS-call fix, and the half that can regress silently.

    The intro routes pass `csp_upload=False`, which is what removes the SSM lookup — but a
    change that made that the DEFAULT would strip the S3 origin from `connect-src` on every
    OTHER response, and the dataset upload would then be blocked by our own header. That
    failure reads as a broken S3 permission, not as a CSP, and it cost hours the first time.
    So both directions are pinned: absent on intro, present everywhere else.

    DATA_BUCKET is set on the live function, so `data_bucket()` normally resolves without
    SSM at all — this asserts the CSP's SHAPE, which is what the browser enforces, and is
    true either way the bucket was resolved.
    """
    console.os.environ["DATA_BUCKET"] = "llmops-csp-probe"
    console._BUCKET_CACHE = None
    try:
        intro = _get(console, "/intro")["headers"]["content-security-policy"]
        other = console._resp(200, {})["headers"]["content-security-policy"]
    finally:
        console.os.environ.pop("DATA_BUCKET", None)
        console._BUCKET_CACHE = None
    assert "llmops-csp-probe" not in intro, (
        f"the intro CSP names an S3 origin the page never fetches: {intro}")
    assert "connect-src 'self';" in intro, f"intro connect-src is not bare 'self': {intro}"
    assert "llmops-csp-probe" in other, (
        "csp_upload=False leaked into the default: every other response just lost the "
        f"upload origin, so a dataset PUT is now blocked by our own header. {other}")


# ── the tab ──────────────────────────────────────────────────────────────────
def _frontend() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def test_introduction_is_the_first_tab_and_the_default_landing_tab():
    """Position and default are two separate claims; both were asked for.

    'A new sub-page on the left of Architecture' is only delivered if the button precedes
    Architecture's AND a first-time visitor lands there.
    """
    html = _frontend()
    tabs = re.findall(r'<button data-tab="([a-z]+)"', html)
    assert tabs[:2] == ["intro", "architecture"], (
        f"tab order is {tabs}; Introduction must sit immediately left of Architecture")

    known = re.search(r'const KNOWN_TABS = \[([^\]]+)\]', html)
    assert known, "KNOWN_TABS not found — a tab absent from it cannot be linked to"
    listed = re.findall(r'"([a-z]+)"', known.group(1))
    assert listed[0] == "intro", f"KNOWN_TABS starts with {listed[0]!r}, expected 'intro'"
    assert set(listed) == set(tabs), (
        f"KNOWN_TABS {sorted(listed)} != the buttons {sorted(tabs)}. A tab missing from "
        "KNOWN_TABS silently ignores its own ?tab= link and its remembered state.")

    # Searched from KNOWN_TABS onward, not from the top of the file: every tab button
    # carries an onclick="showTab('…')", and the first of those matched instead — which
    # made this assertion about a button's markup rather than about the landing logic.
    # The expression itself spans two lines, hence DOTALL and an explicit terminator.
    landing = re.search(r'showTab\((.*?)\);', html[known.end():], re.S)
    assert landing, "the showTab() call that picks the landing tab was not found"
    fallback = landing.group(1).rsplit(":", 1)[-1].strip()
    assert fallback == '"intro"', (
        f"a first-time visitor lands on {fallback}, expected \"intro\". The Introduction "
        "tab exists to be the first thing someone sees.")


def test_the_iframe_is_lazy_and_only_loaded_once():
    """`if (f && !f.src)` is load-once; a bare assignment restarts the narration.

    Setting .src on every visit would also re-fetch 85 KB and throw away the viewer's
    place in a five-minute video every time they glanced at another tab.
    """
    html = _frontend()
    assert 'id="introFrame"' in html
    assert not re.search(r'<iframe[^>]*id="introFrame"[^>]*\ssrc=', html), (
        "introFrame must not carry a src attribute — that fetches the page for every "
        "operator who never opens the tab")
    assert re.search(r'if\s*\(f\s*&&\s*!f\.src\)\s*f\.src\s*=', html), (
        "the src must be set only when unset, or switching tabs restarts the narration")
    assert 'allow="autoplay"' in html, (
        "without allow=autoplay the iframe's permission policy blocks the player's own "
        "gesture-gated play(), which looks exactly like a broken page")


def test_leaving_the_tab_pauses_the_narration():
    """display:none does not pause audio — the two halves of this must both exist.

    A listener with no sender, or a sender with no listener, is the failure this repo has
    shipped before: each half looks complete in review.
    """
    html = _frontend()
    assert re.search(r'postMessage\(\s*\{\s*intro\s*:\s*"pause"\s*\}', html), (
        "showTab must tell the iframe to pause when the intro tab is left, or the "
        "narration keeps talking over whatever tab the operator moved to")
    tpl = (INTRO / "page.template.html").read_text(encoding="utf-8")
    assert 'e.data.intro === "pause"' in tpl, (
        "the page has no listener for the pause message the console sends")
    assert "e.origin !== location.origin" in tpl, (
        "the message listener must check the origin — it accepts input from outside the "
        "document")


# ── deploy.sh ────────────────────────────────────────────────────────────────
def test_deploy_sh_bundles_what_the_handler_reads():
    """The bundle fixture above and deploy.sh must describe the same layout.

    Every route test here passes against a directory this file assembled. If deploy.sh
    copies the audio somewhere else, all of them still pass and the live route 404s — so
    the two are pinned to each other by name, including the `intro_audio` directory the
    handler's cold-start walk is keyed on.
    """
    sh = DEPLOY_SH.read_text(encoding="utf-8")
    assert "build_intro.py" in sh and '--out "$BUILD/intro.html"' in sh, (
        "deploy.sh must generate intro.html into the build dir; it is not committed")
    assert 'intro/audio/." "$BUILD/intro_audio/' in sh, (
        "deploy.sh must copy intro/audio into intro_audio/, the directory name "
        "INTRO_AUDIO_DIR is built from")
    lam = (CONSOLE / "lambda_function.py").read_text(encoding="utf-8")
    assert '"intro_audio"' in lam, (
        "the handler no longer reads intro_audio/ — deploy.sh and the handler have drifted")
    assert "_synth_stamps.json" in sh, (
        "the build record should be stripped from the bundle, not shipped to production")


def test_deploy_sh_refuses_a_package_over_the_lambda_limit():
    """11 MB of audio moves the zip close enough to 50 MB that this must be checked.

    Lambda rejects the upload at the END of a multi-minute build, with a validation error
    that does not mention the size. Adding a sixth language should fail with a number.
    """
    sh = DEPLOY_SH.read_text(encoding="utf-8")
    assert "50 MB" in sh and "ZIP_MB" in sh, (
        "deploy.sh must check the package size before update-function-code")


def test_the_narration_speaks_no_volatile_count():
    """The suite sizes must not be in the audio, in any language.

    817 tests / 124 negative controls / 10 shell assertions move on nearly every pull
    request — this very change adds tests. A number in an MP3 cannot be guarded, cannot be
    grepped, and costs a five-language Polly run to correct. Every other number in the
    narration describes something that HAPPENED and is therefore fixed.

    Matching digits rather than the exact figures on purpose: pinning '817' would pass the
    day someone re-records it as 'eight hundred and thirty'.
    """
    volatile = re.compile(
        r"(\d{3,}|[a-z\- ]{0,20}hundred[a-z\- ]{0,30})\s*"
        r"(tests|negative controls|assertions|個測試|個負面|테스트|テスト)", re.I)
    hits = []
    for lang, byscene in SPEC["text"].items():
        for scene, text in byscene.items():
            for m in volatile.finditer(text):
                hits.append(f"{lang}/{scene}: …{m.group(0)}…")
    assert not hits, (
        "the narration states a suite size, which is the fastest-churning number in the "
        "repo and the one number no guard can read once it is an MP3:\n  "
        + "\n  ".join(hits))
