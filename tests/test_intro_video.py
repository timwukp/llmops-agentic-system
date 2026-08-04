#!/usr/bin/env python3
"""The committed intro video is what the README claims it is.

Why the video is guarded at all. It is a 10 MB binary produced by a headless browser that no
test can run (the recorder needs Chromium and ffmpeg; this suite is offline by construction).
So the artifact is checked instead of the process -- and it needs checking precisely because
nothing else will: a truncated, silent, or drifted video looks exactly like a working one in
a directory listing, and the way anyone finds out is a reader pressing play.

What each property here would have caught, stated so the test is not just a shape assertion:

  - length vs the narration mp3s: a recording that stopped early. This is the one that fails
    if a scene stalls, and it is derived from the SAME clips the video was muxed from, so it
    cannot go stale when the narration is re-synthesised.
  - an audio stream at all: the mux losing the -map, which yields a perfectly playable silent
    film. The whole point of this video is that it is narrated.
  - the stage's authored dimensions: a recording made at some other viewport, where fit()
    scales the stage and every diagram ships resampled.
  - yuv420p + faststart: the two encoder flags whose absence is invisible locally and breaks
    playback for someone else -- a black frame in QuickTime/Safari, and a full 10 MB download
    before frame one on GitHub.
  - the README actually referencing it: a video committed and then orphaned by a later doc
    edit is 10 MB of repo weight nobody can reach.
  - no dollar figure in the poster's scene: the no-amount instruction that applies to the
    narration applies to the frame we greet readers with too.

Duration and codec facts come from ffprobe when it is available, and the file's own headers
when it is not -- a CI runner without ffmpeg must still check what it can rather than skip
the whole module and report green.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import struct
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
VIDEO = REPO / "docs" / "media" / "intro-en.mp4"
POSTER = REPO / "docs" / "media" / "intro-poster.png"
INTRO = REPO / "deploy" / "console" / "intro"
LANG = "en"

#: Authored stage size, from page.template.html. Recording at exactly this means fit()
#: computes scale = 1 and nothing is resampled.
STAGE_W, STAGE_H = 1180, 664

#: Must match record_video.TAIL_S. Imported rather than retyped below.
def _tail_s() -> float:
    src = (INTRO / "record_video.py").read_text()
    m = re.search(r"^TAIL_S = ([0-9.]+)$", src, re.M)
    assert m, "record_video.py no longer defines TAIL_S; this guard's arithmetic is derived from it"
    return float(m.group(1))


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _ffprobe(*args: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", *args,
                          str(VIDEO)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _clip_seconds() -> float:
    """Summed length of the narration clips this video was built from.

    Uses ffprobe when present. Otherwise falls back to parsing the mp3 frame headers, so the
    single most important property -- "the video is as long as the narration" -- is still
    checked on a machine without ffmpeg instead of silently skipped.
    """
    clips = [INTRO / "audio" / LANG / f"{s}.mp3"
             for s in json.loads((INTRO / "narration.json").read_text())["scenes"]]
    missing = [str(c) for c in clips if not c.is_file()]
    assert not missing, f"narration clip(s) missing: {missing}"
    if _have("ffprobe"):
        total = 0.0
        for c in clips:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                  "format=duration", "-of",
                                  "default=nokey=1:noprint_wrappers=1", str(c)],
                                 capture_output=True, text=True, check=True).stdout
            total += float(out.strip())
        return total
    return sum(_mp3_seconds(c) for c in clips)


#: Layer III bitrates in kbps, indexed by the header's 4-bit field. MPEG-1 and MPEG-2/2.5 use
#: DIFFERENT tables, and Polly emits MPEG-2 -- see _mp3_seconds.
_BITRATES_V1 = (None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None)
_BITRATES_V2 = (None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None)

#: Sample rates by MPEG version id (bits 4-3 of byte 1): 3=MPEG-1, 2=MPEG-2, 0=MPEG-2.5.
_RATES = {3: (44100, 48000, 32000, None),
          2: (22050, 24000, 16000, None),
          0: (11025, 12000, 8000, None)}


def _mp3_seconds(path: pathlib.Path) -> float:
    """Duration by walking MPEG audio frame headers. Used only when ffprobe is absent.

    A real frame walk and not size/bitrate: Polly's output is CBR today, and a guard that
    silently becomes wrong if that changes is worse than one that is absent.

    It must handle MPEG-2, not just MPEG-1, and that is not a hypothetical robustness note --
    the first version of this function accepted only MPEG-1 Layer III. Polly's `mp3` output is
    24 kHz mono, which is **MPEG-2** Layer III, so every frame failed the version check, the
    walk fell through to byte-by-byte resync, and it returned **11.7s for 303.8s of audio**:
    4% of the truth, in the right units, with no error. Had ffprobe simply been missing from
    CI, the drift check would have compared a real video length against a fabricated narration
    length and failed for a reason that was not true.

    Three things differ by version and all three matter here: the bitrate table, the sample
    rate table, and the frame geometry (MPEG-1 Layer III is 1152 samples per frame and
    `144*br/sr` bytes; MPEG-2/2.5 is 576 and `72*br/sr`). Cross-checked against ffprobe on all
    seven clips by test_the_mp3_fallback_agrees_with_ffprobe.
    """
    b = path.read_bytes()
    i, dur = 0, 0.0
    # Skip an ID3v2 tag: its body is not frame data and 0xFF bytes inside it would desync the
    # walk. The size field is syncsafe -- seven significant bits per byte.
    if b[:3] == b"ID3" and len(b) >= 10:
        size = 0
        for byte in b[6:10]:
            size = (size << 7) | (byte & 0x7F)
        i = 10 + size
    while i + 4 <= len(b):
        if b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
            i += 1
            continue
        ver, layer = (b[i + 1] >> 3) & 0x03, (b[i + 1] >> 1) & 0x03
        rates = _RATES.get(ver)
        if layer != 1 or rates is None:      # not Layer III, or the reserved version id
            i += 1
            continue
        br = (_BITRATES_V1 if ver == 3 else _BITRATES_V2)[(b[i + 2] >> 4) & 0x0F]
        sr = rates[(b[i + 2] >> 2) & 0x03]
        if not br or not sr:                 # 'free' or reserved bitrate/rate
            i += 1
            continue
        pad = (b[i + 2] >> 1) & 0x01
        spf = 1152 if ver == 3 else 576
        size = (spf // 8) * br * 1000 // sr + pad
        if size <= 4:
            i += 1
            continue
        dur += spf / sr
        i += size
    assert dur > 0, f"no MPEG audio frames found in {path}"
    return dur


@pytest.mark.skipif(not _have("ffprobe"), reason="needs ffprobe to have something to agree with")
def test_the_mp3_fallback_agrees_with_ffprobe():
    """The no-ffprobe path is checked against the with-ffprobe path, clip by clip.

    Without this the fallback is code that only ever runs where nothing can check it. It ran
    wrong for exactly that reason: 11.7s reported for 303.8s of audio, because it assumed
    MPEG-1 and Polly emits MPEG-2. Per-clip rather than on the total, so two errors cannot
    cancel out into a passing sum.
    """
    clips = [INTRO / "audio" / LANG / f"{s}.mp3"
             for s in json.loads((INTRO / "narration.json").read_text())["scenes"]]
    for c in clips:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nokey=1:noprint_wrappers=1", str(c)],
                             capture_output=True, text=True, check=True).stdout
        truth, mine = float(out.strip()), _mp3_seconds(c)
        # 0.1s per ~42s clip: enough for the encoder's own frame rounding, nowhere near enough
        # for a version-table mistake (which is off by a factor, not a fraction).
        assert abs(mine - truth) < 0.1, (
            f"{c.name}: frame walk says {mine:.3f}s, ffprobe says {truth:.3f}s — the fallback "
            "would report a fabricated narration length on a runner without ffmpeg")


def test_the_video_is_committed_and_is_not_a_stub():
    assert VIDEO.is_file(), f"{VIDEO.relative_to(REPO)} is missing"
    size = VIDEO.stat().st_size
    # A five-minute h264 recording is megabytes. Anything tiny is a git-lfs pointer, a
    # truncated upload, or a placeholder -- all of which a directory listing shows as present.
    assert size > 1_000_000, f"video is only {size} bytes — truncated or a pointer file"
    # Not a limit for its own sake: this is a public repo, and every clone pays for it.
    assert size < 25_000_000, (
        f"video is {size / 1e6:.1f} MB — too heavy for a repo every reader clones; "
        "re-encode or move it out of git")


def test_the_video_is_a_real_mp4_with_a_moov_atom_up_front():
    """faststart: the moov atom must precede mdat, or playback waits for the whole download."""
    head = VIDEO.read_bytes()[:4096]
    assert head[4:8] == b"ftyp", "not an ISO base media file (no ftyp box at offset 4)"
    # Walk top-level boxes rather than searching for the bytes anywhere: b"moov" can appear
    # inside compressed video data by chance, which would make a naive `in` check pass on a
    # file whose moov is genuinely at the end.
    data = VIDEO.read_bytes()
    order = [kind for kind, _, _ in _boxes(data, 0, len(data))]
    # Both boxes asserted present before either index() is taken. Found by driving a 4 KB
    # truncated file through this test: it raised `ValueError: b'mdat' is not in list` from
    # inside the comparison instead of failing with a message. A guard that crashes still goes
    # red, but it reports a Python bug rather than the state of the artifact, and the next
    # person reads it as the test being broken.
    for box in (b"moov", b"mdat"):
        assert box in order, (
            f"no top-level {box.decode()} box found (boxes seen: {order}) — the file is "
            "truncated or is not the mp4 it claims to be")
    assert order.index(b"moov") < order.index(b"mdat"), (
        f"moov comes after mdat (boxes: {order}) — ffmpeg was run without -movflags "
        "+faststart, so a browser must download the whole file before showing frame one")


@pytest.mark.skipif(not _have("ffprobe"), reason="ffprobe not installed")
def test_the_video_carries_an_audio_stream_and_the_authored_frame_size():
    streams = _ffprobe("-show_streams")["streams"]
    video = [s for s in streams if s["codec_type"] == "video"]
    audio = [s for s in streams if s["codec_type"] == "audio"]
    assert len(video) == 1, f"expected one video stream, got {len(video)}"
    # The entire point of this artifact is that it is narrated. A silent mp4 plays fine and
    # is a different product.
    assert audio, "video has NO audio stream — the narration was not muxed in"
    assert video[0]["codec_name"] == "h264", f"unexpected video codec {video[0]['codec_name']}"
    assert video[0]["pix_fmt"] == "yuv420p", (
        f"pix_fmt is {video[0]['pix_fmt']}, not yuv420p — QuickTime and Safari show a black "
        "frame for anything else")
    assert (video[0]["width"], video[0]["height"]) == (STAGE_W, STAGE_H), (
        f"recorded at {video[0]['width']}x{video[0]['height']}, but the stage is authored at "
        f"{STAGE_W}x{STAGE_H} — anything else ships the diagrams resampled")


def _boxes(data: bytes, start: int, end: int):
    """Yield (kind, payload_start, payload_end) for the boxes between two offsets."""
    off = start
    while off + 8 <= end:
        (size,) = struct.unpack(">I", data[off:off + 4])
        kind, body = data[off + 4:off + 8], off + 8
        if size == 1:
            (size,) = struct.unpack(">Q", data[off + 8:off + 16])
            body = off + 16
        elif size == 0:
            size = end - off          # "to end of file", per the spec
        if size < 8:
            return
        yield kind, body, min(off + size, end)
        off += size


def _mvhd_seconds(data: bytes) -> float:
    """Duration from the mp4's own movie header, without ffprobe.

    `mvhd` carries a timescale and a duration in those units. Read from the container rather
    than trusted from the build log for the same reason the recorder measures the clips instead
    of reading durations.json: a length that is asserted by the thing being checked is not a
    check. This exists so the single most important property here -- that the video is as long
    as its narration -- is verified on a runner with no ffmpeg (this repo's CI is one) instead
    of skipped into a green tick.
    """
    for kind, s, e in _boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        for k2, s2, e2 in _boxes(data, s, e):
            if k2 != b"mvhd":
                continue
            version = data[s2]
            if version == 1:
                timescale = struct.unpack(">I", data[s2 + 20:s2 + 24])[0]
                duration = struct.unpack(">Q", data[s2 + 24:s2 + 32])[0]
            else:
                timescale = struct.unpack(">I", data[s2 + 12:s2 + 16])[0]
                duration = struct.unpack(">I", data[s2 + 16:s2 + 20])[0]
            assert timescale, "mvhd timescale is zero — the header is not readable"
            return duration / timescale
    raise AssertionError("no moov/mvhd box — the file is not a readable mp4")


@pytest.mark.skipif(not _have("ffprobe"), reason="needs ffprobe to have something to agree with")
def test_the_mvhd_reader_agrees_with_ffprobe():
    """Same reasoning as the mp3 fallback: the no-ffprobe reader is checked where it can be.

    Otherwise the length check that runs in CI is the one branch nobody has ever compared
    against a known-good answer.
    """
    truth = float(_ffprobe("-show_format")["format"]["duration"])
    mine = _mvhd_seconds(VIDEO.read_bytes())
    assert abs(mine - truth) < 0.05, (
        f"mvhd says {mine:.3f}s, ffprobe says {truth:.3f}s — the container reader used on "
        "runners without ffmpeg does not agree with the one used here")


def test_the_video_is_as_long_as_the_narration_it_was_built_from():
    """The drift check, re-derived from the clips rather than trusted from build time.

    Deliberately NOT skipped when ffprobe is absent. Both sides of the comparison can be read
    from the bytes -- the narration from its MPEG frame headers, the video from `mvhd` -- and
    this is the assertion that catches a scene that never played. Skipping the one check that
    matters on the one machine that gates merges is how a video ships silently truncated.
    """
    expected = _clip_seconds() + _tail_s()
    got = (float(_ffprobe("-show_format")["format"]["duration"]) if _have("ffprobe")
           else _mvhd_seconds(VIDEO.read_bytes()))
    drift = got - expected
    # 2s over 5 minutes. Wide enough for container rounding and one dropped keyframe, far too
    # narrow for a scene that failed to play (the shortest clip is 40s).
    assert abs(drift) < 2.0, (
        f"video is {got:.2f}s but the narration plus tail is {expected:.2f}s "
        f"({drift:+.2f}s) — a scene did not play, or the recording was cut")


def test_the_poster_exists_and_is_a_png():
    assert POSTER.is_file(), f"{POSTER.relative_to(REPO)} is missing"
    head = POSTER.read_bytes()[:8]
    assert head == b"\x89PNG\r\n\x1a\n", "poster is not a PNG"
    assert POSTER.stat().st_size > 20_000, "poster is suspiciously small — likely a blank frame"


@pytest.mark.parametrize("readme", ["README.md", "README.zh-TW.md"])
def test_both_readmes_reach_the_video_and_the_live_page(readme):
    """A committed video no README references is weight nobody can reach.

    Both language versions, because `hooks/pre-commit` already requires the pair to land
    together and a reader of one should not get less than a reader of the other.
    """
    text = (REPO / readme).read_text()
    rel = "docs/media/intro-en.mp4"
    assert rel in text, f"{readme} does not reference {rel}"
    # BOTH forms, not either. The README argues in prose that the plain link is deliberate
    # redundancy because whether GitHub renders a repo-relative <video> as a player cannot be
    # verified before pushing -- so if the tag is flattened to nothing AND the link is gone, the
    # reader has no way to the file. Requiring only one mention let a control that deleted the
    # play link pass, because the <video src=...> attribute still matched the substring.
    assert re.search(rf"<video[^>]*src=\"{re.escape(rel)}\"", text), (
        f"{readme} has no <video> tag for the walkthrough — nothing will play inline")
    assert re.search(rf"\[[^\]]*\]\({re.escape(rel)}\)", text), (
        f"{readme} has no plain markdown link to {rel} — if GitHub does not render the "
        "<video> tag as a player, the reader is left with no way to reach the file")
    # The live page is the canonical five-language artifact; the mp4 is English only, so the
    # README must not leave a non-English reader without the pointer.
    #
    # Matched as an absolute URL ending in /intro, NOT as the bare substring "/intro". The
    # first version asserted `"/intro" in text` -- which the video's own path
    # (docs/media/intro-en.mp4) satisfies, so deleting the live link entirely still passed.
    # Found by mutating the link away and watching the test stay green on that assertion.
    assert re.search(r"https?://\S+/intro\b", text), (
        f"{readme} does not link the live /intro page — the mp4 is English only, so a reader "
        "who needs one of the other four languages would have nowhere to go")


def _section_containing(text: str, needle: str) -> str:
    """The `## …` section the needle sits in, from its heading to the next heading.

    Sliced by heading rather than by a character window on purpose. A window wide enough to
    cover this section also reaches into the next one -- and the next one is `## What it is`,
    which states the platform's own configured reporting reference in prose that predates this
    change and is deliberately out of scope. A guard that fails on text it was never meant to
    police is a guard someone deletes.
    """
    # Asserted, not `.index`. A README with the reference deleted made this raise
    # `ValueError: substring not found`, which reports as a broken test rather than as a README
    # that no longer reaches the video -- the same crash-instead-of-assert problem the moov
    # check had.
    assert needle in text, (
        f"{needle!r} is not in this README, so the walkthrough section cannot be located")
    i = text.index(needle)
    start = text.rfind("\n## ", 0, i)
    assert start != -1, "the video reference is not inside a ## section"
    end = text.find("\n## ", i)
    return text[start:end if end != -1 else len(text)]


def test_the_video_section_names_no_budget_amount():
    """The no-amount rule covers the video's framing too, in both languages.

    The narration itself contains no figure; this catches a caption, a poster alt-text, or a
    "what you'll see" summary reintroducing one right next to the player, where a reader would
    read it as the product's number rather than one team's setting.
    """
    bad = re.compile(r"\$\s?20[,.]?000|20[,.]?000\s*(?:USD|dollars|美元)|兩萬|二萬|2\s*萬")
    for readme in ("README.md", "README.zh-TW.md"):
        section = _section_containing((REPO / readme).read_text(), "intro-en.mp4")
        hit = bad.search(section)
        assert not hit, (
            f"{readme}: the walkthrough section names a budget amount ({hit.group(0)!r}); "
            "the reporting reference is set by each team and no figure belongs here")
