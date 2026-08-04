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

def _stage_size() -> tuple[int, int]:
    """The authored stage size, read from the page's own CSS.

    Not retyped, and not read from record_video.py either -- the recorder has its own
    STAGE_W/STAGE_H copy, and a guard that compares the video to the RECORDER's number would
    stay green if both drifted away from what the page actually lays out. `.stage` is the box
    every scene is positioned inside; fit() scales it, so this is the size at which scale == 1
    and nothing is resampled.
    """
    css = (INTRO / "page.template.html").read_text()
    m = re.search(r"\.stage\s*\{[^}]*?width:\s*(\d+)px;\s*height:\s*(\d+)px", css, re.S)
    assert m, ("could not find the .stage width/height in page.template.html — the authored "
               "size this guard compares the recording against is derived from there")
    return int(m.group(1)), int(m.group(2))


#: Recording at exactly the authored stage size means fit() computes scale = 1 and nothing is
#: resampled.
STAGE_W, STAGE_H = _stage_size()

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


def test_the_video_carries_an_audio_stream_and_the_authored_frame_size():
    """Track shape from the container, so this runs where merges are gated.

    This assertion was behind `skipif(not _have("ffprobe"))` and CI has no ffmpeg. Measured
    what that cost: a full-length copy remuxed with `-an` -- a perfectly playable SILENT film,
    the exact failure the module docstring names -- passed the whole module under CI conditions
    (7 passed, 3 skipped). Same defect as the skipped length check, one test along: an
    assertion gated on a tool the gating machine does not have is not an assertion.

    Handler type, codec fourcc and the tkhd frame size are all in `moov`, so none of this
    needed ffprobe in the first place. test_the_track_reader_agrees_with_ffprobe pins it
    against ffprobe where ffprobe exists.
    """
    video = [t for t in _tracks(VIDEO.read_bytes()) if t["handler"] == "vide"]
    audio = [t for t in _tracks(VIDEO.read_bytes()) if t["handler"] == "soun"]
    assert len(video) == 1, f"expected one video track, got {len(video)}"
    # The entire point of this artifact is that it is narrated. A silent mp4 plays fine and
    # is a different product.
    assert audio, "video has NO audio track — the narration was not muxed in"
    # fourcc as stored in the sample description, not ffprobe's friendly name: h264 is `avc1`
    # and aac is `mp4a`.
    assert video[0]["codec"] == "avc1", (
        f"video track codec is {video[0]['codec']!r}, not avc1/h264")
    assert audio[0]["codec"] == "mp4a", (
        f"audio track codec is {audio[0]['codec']!r}, not mp4a/aac")
    # CODED size, from the sample entry -- the pixels that actually exist in the file.
    assert (video[0]["w"], video[0]["h"]) == (STAGE_W, STAGE_H), (
        f"recorded at {video[0]['w']}x{video[0]['h']}, but the stage is authored at "
        f"{STAGE_W}x{STAGE_H} — anything else ships the diagrams resampled")
    # And the display size must agree with it, i.e. square pixels. Without this, a video coded
    # at 640x360 with SAR 59:32 reports the authored 1180 width in tkhd and passes while
    # carrying a third of the pixels -- measured, not hypothesised.
    assert (video[0]["disp_w"], video[0]["disp_h"]) == (video[0]["w"], video[0]["h"]), (
        f"coded {video[0]['w']}x{video[0]['h']} but displayed as "
        f"{video[0]['disp_w']}x{video[0]['disp_h']} — the pixel aspect is not square, so a "
        "player rescales every frame and the diagrams arrive distorted")
    # The audio track must be roughly as long as the video track. A 2-second beep satisfies
    # "has an audio track" while being just as silent as no track at all for 99% of the run.
    drift = video[0]["secs"] - audio[0]["secs"]
    assert abs(drift) < 2.0, (
        f"video track is {video[0]['secs']:.2f}s but the audio track is "
        f"{audio[0]['secs']:.2f}s ({drift:+.2f}s) — most of this film is silent")


@pytest.mark.skipif(not _have("ffprobe"), reason="needs ffprobe to have something to agree with")
def test_the_track_reader_agrees_with_ffprobe():
    """Same reasoning as the mp3 and mvhd cross-checks: pin the no-ffprobe reader where it can be."""
    truth = {s["codec_type"]: s for s in _ffprobe("-show_streams")["streams"]}
    mine = {t["handler"]: t for t in _tracks(VIDEO.read_bytes())}
    assert set(mine) == {"vide", "soun"}, f"track reader found {sorted(mine)}"
    # ffprobe's width/height IS the coded size, which is why the reader is pinned on the sample
    # entry and not on tkhd -- the two disagree whenever the pixel aspect is not square.
    assert (mine["vide"]["w"], mine["vide"]["h"]) == (truth["video"]["width"],
                                                      truth["video"]["height"]), (
        f"reader says coded {mine['vide']['w']}x{mine['vide']['h']}, ffprobe says "
        f"{truth['video']['width']}x{truth['video']['height']}")
    assert truth["video"].get("sample_aspect_ratio", "1:1") in ("1:1", "0:1"), (
        f"SAR is {truth['video']['sample_aspect_ratio']} — non-square pixels; the coded-vs-"
        "display assertion in the test above is what catches this")
    for handler, kind in (("vide", "video"), ("soun", "audio")):
        assert abs(mine[handler]["secs"] - float(truth[kind]["duration"])) < 0.05, (
            f"{handler} track: reader says {mine[handler]['secs']:.3f}s, ffprobe says "
            f"{truth[kind]['duration']}s")
    # pix_fmt is NOT in the container in a form worth parsing (it is inside the avcC profile
    # bytes), so this one property stays ffprobe-only and is asserted here rather than being
    # silently dropped when the test above moved off ffprobe.
    assert truth["video"]["pix_fmt"] == "yuv420p", (
        f"pix_fmt is {truth['video']['pix_fmt']}, not yuv420p — QuickTime and Safari show a "
        "black frame for anything else")


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


def _tracks(data: bytes) -> list[dict]:
    """Per-track handler, codec fourcc, frame size and duration, read from `moov`.

    Exists because the audio/frame-size assertions used to require ffprobe, which this repo's
    CI does not have -- so a silent film passed the gate. Every field here is a fixed offset in
    a box the file must already contain to be playable at all:

      tkhd  DISPLAY width/height as 16.16 fixed point, at +76 (version 0) or +88 (version 1)
      hdlr  handler type fourcc at +8 -- `vide` / `soun`
      mdhd  timescale + duration, at +12/+16 (v0) or +20/+24 (v1); PER TRACK, unlike mvhd
      stsd  sample description; format fourcc at +12, and for a visual entry the CODED
            width/height as two u16 at +40

    Coded and display size are BOTH read because they are different numbers and only one of
    them answers "were the diagrams resampled". Found by measuring: a control scaled to 640x360
    reported `639x360` from tkhd, which is 640 x SAR 2655/2656 -- so tkhd is display geometry.
    Feeding a `setsar=59/32` copy of the same 640-wide video made tkhd report **1180**, the
    authored width exactly, and the frame-size assertion would have passed on a video carrying
    a third of the authored pixels. The coded size is the one asserted against the stage;
    display is compared to it so a non-square SAR cannot pass either.

    Returns a list, not a dict keyed by handler: "how many video tracks" is one of the things
    worth asserting, and a dict would silently collapse two of them into one.
    """
    out: list[dict] = []
    for kind, s, e in _boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        for k2, s2, e2 in _boxes(data, s, e):
            if k2 != b"trak":
                continue
            t: dict = {"handler": None, "codec": None, "w": None, "h": None,
                       "disp_w": None, "disp_h": None, "secs": None}
            for k3, s3, e3 in _boxes(data, s2, e2):
                if k3 == b"tkhd":
                    off = s3 + (88 if data[s3] == 1 else 76)
                    w, h = struct.unpack(">II", data[off:off + 8])
                    # 16.16 fixed point, and DISPLAY geometry -- rounded, because a
                    # non-square SAR makes it fractional (639.759 for a 640-wide frame).
                    t["disp_w"], t["disp_h"] = round(w / 65536), round(h / 65536)
                elif k3 == b"mdia":
                    for k4, s4, e4 in _boxes(data, s3, e3):
                        if k4 == b"hdlr":
                            t["handler"] = data[s4 + 8:s4 + 12].decode("latin-1")
                        elif k4 == b"mdhd":
                            if data[s4] == 1:
                                ts, dur = struct.unpack(">IQ", data[s4 + 20:s4 + 32])
                            else:
                                ts, dur = struct.unpack(">II", data[s4 + 12:s4 + 20])
                            assert ts, "a track's mdhd timescale is zero — header unreadable"
                            t["secs"] = dur / ts
                        elif k4 == b"minf":
                            for k5, s5, e5 in _boxes(data, s4, e4):
                                if k5 != b"stbl":
                                    continue
                                for k6, s6, e6 in _boxes(data, s5, e5):
                                    if k6 == b"stsd":
                                        t["codec"] = data[s6 + 12:s6 + 16].decode("latin-1")
                                        # NOT conditioned on t["handler"]: that would depend on
                                        # hdlr having been walked before stsd, which is true of
                                        # this file but is not required by the spec. The +40 u16
                                        # pair is only meaningful for a VisualSampleEntry, so
                                        # the caller reads it for the vide track and ignores it
                                        # elsewhere (an audio entry yields the sample rate).
                                        t["w"], t["h"] = struct.unpack(
                                            ">HH", data[s6 + 40:s6 + 44])
            # Assert rather than tolerate a None: a track whose handler or duration could not be
            # read must fail here, not flow into a comparison that quietly skips it.
            for field in ("handler", "codec", "secs"):
                assert t[field] is not None, (
                    f"could not read {field} for a track — the mp4's moov is not the shape "
                    "this reader understands, so nothing below can be trusted")
            out.append(t)
    assert out, "no tracks found in moov — the file is not a readable mp4"
    return out


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
def test_both_readmes_reach_the_committed_video(readme):
    """A committed video no README references is weight nobody can reach.

    Both language versions, because `hooks/pre-commit` already requires the pair to land
    together and a reader of one should not get less than a reader of the other.

    Renamed from `..._and_the_live_page`: the live page it referred to was the console's
    execute-api hostname, deleted from this repo months ago, and then the in-repo pointer that
    replaced it was deleted too when the section was cut back to a player and a link. A guard
    named after a thing it no longer checks is read as checking it.
    """
    text = (REPO / readme).read_text()
    rel = "docs/media/intro-en.mp4"
    assert rel in text, f"{readme} does not reference {rel}"
    # A <video> tag is NOT required, and asserting one was wrong. This guard used to demand
    # `<video src="docs/media/intro-en.mp4">` on the grounds that whether GitHub renders it as a
    # player "cannot be verified before pushing". It can: POST /markdown with mode=gfm returns
    # exactly what the repo page will show, and it strips <video> to nothing -- for the
    # repo-relative path, a raw.githubusercontent.com URL, a <source> child and a release asset
    # alike. The rendered homepage HTML of this repo confirmed it: zero <video> elements. So the
    # guard was requiring a tag that provably does nothing while its message claimed the reader
    # could play it inline.
    #
    # What this does NOT mean, and what the message below used to imply: that nothing plays
    # inline. A bare user-attachments URL does, and both READMEs now carry one -- see
    # test_both_readmes_carry_the_inline_player_url. The two facts are compatible because that
    # URL is not a tag: GitHub's renderer recognises the LINK and builds the <video> element
    # itself. Writing the tag by hand is still erased, which is why this half of the guard stays.
    #
    # Code spans are stripped before matching. The READMEs no longer explain in prose that
    # `<video>` is stripped -- that paragraph was cut -- but the strip stays, because the reason
    # it went in is that the naive version of this assertion failed on its own documentation, and
    # the next person who documents the defect should not have to rediscover that.
    prose = re.sub(r"`[^`]*`", "", text)
    assert not re.search(r"<video\b", prose), (
        f"{readme} has a <video> tag — GitHub's sanitizer deletes it, so it renders as an empty "
        "gap. A bare user-attachments URL on its own line is the form that plays; link the "
        "committed mp4 with a poster image")
    # The poster link is now the ONLY path from either README to the committed file. It used to be
    # one of two (a poster image link and a plain "Download …" link), and that redundancy was
    # deliberate -- one careless rewrite could otherwise make 10 MB unreachable. The download line
    # was cut as verbose, so this single assertion is now load-bearing on its own; it is not a
    # weaker check than before, it is the same check with nothing behind it.
    poster = re.search(rf"\[!\[[^\]]*\]\([^)]*\)\]\({re.escape(rel)}\)", text)
    assert poster, (
        f"{readme} has no clickable poster image for the walkthrough — nothing in the section "
        f"reaches {rel}, and the inline player serves a different, smaller upload")


ASSET_URL = re.compile(
    r"^https://github\.com/user-attachments/assets/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.M)


def test_both_readmes_carry_the_inline_player_url():
    """The one form of link GitHub turns into a player, in both languages, identical.

    This is the guard the sibling test above could not be. That one forbids a hand-written
    <video> tag; forbidding the thing that does not work says nothing about keeping the thing
    that does. A rewrite that deletes this URL leaves both guards green and the repo page with
    no player -- which is exactly the state the READMEs were in until it was added.

    Measured, not assumed, via POST /markdown mode=gfm: a bare user-attachments URL ALONE IN
    ITS PARAGRAPH renders <details open> + <video controls>. Hence `^...$` with re.M and the
    blank-line check -- the same URL wrapped in `[text](url)` or trailing prose in the same
    paragraph is a different input, and this repo does not get to guess which ones GitHub
    promotes. It also cannot be verified offline (the asset is behind a signed JWT), so the
    check is on the FORM that was verified online, held to the letter.
    """
    urls = set()
    for readme in ("README.md", "README.zh-TW.md"):
        text = (REPO / readme).read_text()
        found = ASSET_URL.findall(text)
        assert found, (
            f"{readme} has no bare https://github.com/user-attachments/assets/<uuid> line — "
            "that URL alone in a paragraph is the ONLY form GitHub renders as a player, so "
            "without it this page shows a poster and two download links and nothing plays")
        for url in found:
            i = text.index(url)
            before, after = text[:i], text[i + len(url):]
            assert before.endswith("\n\n"), (
                f"{readme}: the player URL is not alone in its paragraph (text precedes it on "
                "the same block) — GitHub only promotes a URL that stands by itself")
            assert after.startswith("\n\n") or after.strip() == "", (
                f"{readme}: prose follows the player URL inside the same paragraph — GitHub "
                "only promotes a URL that stands by itself")
        urls.update(found)
    assert len(urls) == 1, (
        f"the two READMEs point at different uploads ({sorted(urls)}) — a reader of one would "
        "watch a different film from a reader of the other")


def test_the_walkthrough_section_states_no_number_it_does_not_derive():
    """No retyped size or encoder setting anywhere in the walkthrough section, either language.

    This replaces a guard that asserted the section DID state "10.7 MB, CRF 26" next to the
    download link, and the replacement is not a weakening -- it is the same defect approached
    from the other side. Those two numbers, and the paragraph explaining that the inline player
    is a smaller re-encode of the committed file, were cut as verbose. The class of failure they
    created has not gone anywhere: a size or a CRF written into a README is measured on the day
    it is typed and looks measured forever, and this section is the likeliest place for one to
    come back, because "how big is the download" is the obvious thing to want to add.

    So the rule the section now lives by is: don't state either. Anyone who reintroduces one has
    to make it derived, and this guard is where they find that out -- with a message that says
    which number and why. The alternative, deleting the old guard outright, would leave the next
    "helpful" 10.7 MB to rot silently, which is exactly how the first one got there.

    Scoped to the walkthrough section, not the whole README: `docs/COST.md` and the cost prose
    elsewhere legitimately carry figures, and a guard that fails on text it was never meant to
    police is a guard someone deletes.
    """
    # Both patterns are about a claim ABOUT THE FILE, so both are anchored on units, not on bare
    # digits -- "5:04" and "0/16" are facts about the film and the gate, not retyped measurements.
    bad = re.compile(r"\b\d+(?:\.\d+)?\s*[MK]B\b|\bCRF[\s-]*\d+", re.I)
    for readme in ("README.md", "README.zh-TW.md"):
        section = _section_containing((REPO / readme).read_text(), "intro-en.mp4")
        hit = bad.search(section)
        assert not hit, (
            f"{readme}: the walkthrough section states {hit.group(0)!r} — a file size or encoder "
            "setting typed into prose is measured once and reads as measured forever (the last "
            "one said 10.7 MB and stayed while the file was re-encoded). Derive it from "
            "docs/media/intro-en.mp4 or record_video.py, or leave it out")


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
