#!/usr/bin/env python3
"""Record the /intro walkthrough as a narrated mp4, to upload as the README's player.

Why this exists. The live page is the canonical artifact -- five languages, real Polly
voices, a language picker. But a reader on GitHub sees a README, not a browser tab, and a
link is something you have to decide to click. This produces one file they can press play
on without leaving the page they are already on.

The output does NOT belong in this repo. It is ~10 MB, it was committed once, and it had to
be removed: a bare `https://github.com/user-attachments/assets/<uuid>` URL alone in its own
paragraph is the form GitHub promotes into a real <video> element, and GitHub hosts those
bytes. So upload what comes out of here and put the URL in both READMEs -- leave the tree
clean. tests/test_intro_video.py fails on any tracked .mp4/.mov/.webm/.mkv/.avi.

Why it is a BUILD step and not a test. It drives a real headless browser and shells out to
ffmpeg; it is the same category as synth_narration.py (which calls Polly). Tests in this repo
are offline by construction and must stay that way. Which leaves this script as very nearly
the only thing that checks its own output, so the drift check below is not belt-and-braces:
the container reader that used to re-verify the committed mp4 -- faststart, the audio mux, the
pixel format, recorded length against the narration -- was deleted with the file it read. Do
not widen --tolerance to make a run pass. What survives on the test side is one comparison of
STAGE_W/STAGE_H against the page's own `.stage` box, so a stage re-authored without updating
this file still fails.

How the picture and the sound are kept in sync -- this is the whole design:

  The page's clock IS the audio element (`curTime()` returns `audio.currentTime`). Beats
  fire off that clock, so if the browser stalls for 300 ms the animation stalls with the
  sound rather than sliding ahead of it. We therefore let the page play in real time and
  record it in real time, then mux the SAME mp3s the page just played. Sync is a property
  of using one clock, not of aligning two recordings afterwards.

  The alternative -- render frame N at t = N/fps and stitch -- sounds more precise and is
  worse: it discards the CSS transitions and SVG @keyframes entirely (they animate on the
  document timeline, which does not advance while a screenshot is being taken), so every
  beat would pop instead of fade, and the diagrams would be static.

  What could still drift is the recorder's own frame timestamps. So the recording is not
  trusted: the muxed output is measured with ffprobe against the summed mp3 durations plus
  TAIL_S, and a disagreement beyond --tolerance FAILS the build. A video that drifted is a
  video nobody can tell drifted by watching it once, which is exactly why it is measured
  rather than eyeballed.

The audio is not re-encoded from a fresh Polly call: it is the committed mp3s, so the video
cannot say something different from the page.

Usage (note the destination is OUTSIDE the repo -- see above):
    python3 deploy/console/intro/record_video.py --out /tmp/intro-en.mp4
    python3 deploy/console/intro/record_video.py --out /tmp/smoke.mp4 --scenes 1   # fast check
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
STAGE_W, STAGE_H = 1180, 664

#: Held after the last word so the closing beat's .5s fade finishes on screen instead of
#: being cut mid-transition. It is part of the expected length, not drift -- subtracting it
#: from the tolerance instead would mean loosening the drift check to accommodate a value we
#: chose on purpose, and a tolerance wide enough to hide a deliberate second is wide enough
#: to hide an accidental one.
TAIL_S = 0.9


def _ffprobe_seconds(path: pathlib.Path) -> float:
    """Duration in seconds, from ffprobe. Raises if ffprobe cannot read the file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the built page at /intro and the clips at /intro/audio/<lang>/<scene>.mp3.

    A plain static file server would not do: the page hard-codes AUDIO_BASE = "/intro/audio/"
    because that is the route the Lambda serves, and recording against a different URL shape
    would be recording a page that does not exist in production.
    """

    def __init__(self, *a, page: pathlib.Path, audio: pathlib.Path, **kw):
        self._page, self._audio = page, audio
        super().__init__(*a, **kw)

    def log_message(self, *a):  # keep the recording log readable
        pass

    def do_GET(self):
        if self.path in ("/intro", "/intro/"):
            return self._send(self._page.read_bytes(), "text/html; charset=utf-8")
        if self.path.startswith("/intro/audio/"):
            rel = self.path[len("/intro/audio/"):]
            # Refuse traversal rather than normalising it: this server exists for one
            # directory, and a build tool that can be talked into reading elsewhere is a
            # build tool that will be, eventually.
            if ".." in rel or rel.startswith("/"):
                return self._send(b"no", "text/plain", 404)
            f = self._audio / rel
            if not f.is_file():
                return self._send(b"missing", "text/plain", 404)
            return self._send(f.read_bytes(), "audio/mpeg")
        return self._send(b"not found", "text/plain", 404)

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(page: pathlib.Path, audio: pathlib.Path):
    """Start a loopback-only server; return (base_url, shutdown)."""
    handler = functools.partial(_Handler, page=page, audio=audio)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_port}", httpd.shutdown


def record(page_html: pathlib.Path, audio_dir: pathlib.Path, lang: str,
           scenes: list[str], out_dir: pathlib.Path,
           timeout_s: float) -> tuple[pathlib.Path, float]:
    """Play the page in a headless browser; return (raw video, lead-in seconds).

    The lead-in is why this returns a pair. Recording begins when the browser context is
    created, but the narration cannot begin until the page has loaded and the gate has been
    clicked -- roughly a second later, measured. Muxing the audio at t=0 would put the whole
    soundtrack about a second ahead of the picture for the entire five minutes: subtle enough
    to survive a glance, wrong for every beat. The caller trims exactly this much off the
    front so frame 0 of the published video is the frame the narration starts on.
    """
    from playwright.sync_api import sync_playwright

    base, shutdown = _serve(page_html, audio_dir)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                # The page gates on a click precisely because a blocked autoplay looks like
                # a broken page; here there is no human to click, so the policy is lifted.
                "--autoplay-policy=no-user-gesture-required",
                # Muted, but media time still advances -- and media time is the page's clock.
                # The soundtrack comes from the committed mp3s at mux time, not from this.
                "--mute-audio",
            ])
            ctx = browser.new_context(
                viewport={"width": STAGE_W, "height": STAGE_H},
                # Recording at the authored stage size means fit() computes scale = 1, so
                # nothing is resampled. Any other size would ship a blurred diagram.
                record_video_dir=str(out_dir),
                record_video_size={"width": STAGE_W, "height": STAGE_H},
            )
            pg = ctx.new_page()
            # Pin the language BEFORE the page reads localStorage, so the recording cannot
            # inherit whatever a previous run left there.
            pg.add_init_script(f"try {{ localStorage.setItem('introLang', {lang!r}); }} catch (e) {{}}")
            # Expose the page's current Audio element to the wait condition above. The page
            # keeps it in a module-scope variable, not in the DOM, so `querySelector('audio')`
            # finds nothing -- measured, not assumed. Wrapping the constructor observes the
            # real object without the page being modified for the benefit of the recorder:
            # a recording of an instrumented page is a recording of a different page.
            pg.add_init_script(
                "(() => { const O = window.Audio;"
                " window.Audio = function(...a) { const el = new O(...a);"
                " window._recAudio = el; return el; };"
                " window.Audio.prototype = O.prototype; })();")
            t_open = time.monotonic()
            pg.goto(base + "/intro", wait_until="load")
            pg.wait_for_selector("#goBtn", state="visible")
            pg.click("#goBtn")
            # Measure to the moment sound actually starts, not to the click: `audio.play()`
            # resolves asynchronously, and on a cold profile the first decode is the slowest.
            pg.wait_for_function(
                "() => window._recAudio && window._recAudio.currentTime > 0",
                timeout=30_000)
            lead_in = time.monotonic() - t_open

            # Wait on the page's own state rather than on a computed duration: if a clip
            # runs longer than durations.json claims, a timer would cut the last scene off
            # mid-sentence and nothing would say so.
            #
            # The condition is "the last scene we want has played to the end of its audio",
            # NOT "the Play button says Replay". The button only flips at the end of the
            # WHOLE deck (`go()` past the last scene), so keying on it makes a partial
            # recording -- the `--scenes N` smoke path -- wait forever. Learned by watching
            # exactly that timeout, on the run that was supposed to be the cheap check.
            last = scenes[-1]
            pg.wait_for_function(
                """(last) => {
                     const el = document.getElementById(last);
                     if (!el || !el.classList.contains('on')) return false;
                     const btn = document.getElementById('playBtn');
                     // End of the whole deck: the transport has stopped and offers a replay.
                     if (btn && btn.textContent.indexOf('Replay') >= 0) return true;
                     // Partial recording: this scene's own audio has finished. `_recAudio`
                     // is set by the init script below -- the page holds its Audio object in
                     // a closure, so there is nothing else to observe from out here.
                     const a = window._recAudio;
                     return !!(a && a.duration && a.currentTime >= a.duration - 0.05);
                   }""",
                arg=last, timeout=timeout_s * 1000)
            # A beat's fade is .5s; ending the recording on the frame the audio stopped
            # would clip the final reveal mid-transition.
            pg.wait_for_timeout(int(TAIL_S * 1000))
            video = pg.video.path()
            ctx.close()      # flush: the file is only complete after the context closes
            browser.close()
        return pathlib.Path(video), lead_in
    finally:
        shutdown()


def mux(raw_video: pathlib.Path, clips: list[pathlib.Path], out: pathlib.Path,
        lead_in: float, audio_s: float) -> None:
    """Concatenate the narration clips and mux them onto the recording as one mp4.

    `lead_in` is trimmed off the front and the output is cut to `audio_s + TAIL_S`, so the
    published file starts on the frame the first word lands and ends one fade after the last.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        listing = pathlib.Path(td) / "clips.txt"
        listing.write_text("".join(f"file '{c}'\n" for c in clips))
        track = pathlib.Path(td) / "narration.m4a"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-c:a", "aac", "-b:a", "96k", str(track)],
                       check=True)
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            # Trim the pre-narration lead-in off the VIDEO, so t=0 of the output is the
            # frame the first word lands on. -ss before -i seeks the input, which is both
            # faster and frame-accurate here because the source is re-encoded below anyway.
            "-ss", f"{lead_in:.3f}", "-i", str(raw_video), "-i", str(track),
            # yuv420p + faststart: without the pixel format Safari and QuickTime show a
            # black frame, and without faststart the moov atom lands at the end of the file
            # so a browser must download all 14 MB before it can show frame one.
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-preset", "slow",
            "-movflags", "+faststart", "-c:a", "copy",
            "-map", "0:v:0", "-map", "1:a:0",
            # Cut the trailing flush. Recording does not stop the instant the deck ends: the
            # video file is finalised when the browser context closes, and that write shows
            # up as roughly a second of extra footage past the tail -- measured, and it is at
            # the END, not the front (the front is handled by -ss above). -shortest would cut
            # to the audio and lose the deliberate tail, so the length is stated instead.
            "-t", f"{audio_s + TAIL_S:.3f}",
            str(out)], check=True)


def poster(video: pathlib.Path, out: pathlib.Path, at: float) -> None:
    """Grab one frame as the <video> poster.

    Taken a few seconds in, not at 0:00: the first frame is a scene whose beats have not
    revealed yet, i.e. a nearly empty slide, which is the least informative frame in the
    whole deck to greet a reader with.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(at), "-i", str(video),
                    "-frames:v", "1", str(out)], check=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="mp4 to write")
    ap.add_argument("--lang", default="en", help="narration language (default en)")
    ap.add_argument("--scenes", type=int, default=0,
                    help="record only the first N scenes (smoke test); 0 = all")
    ap.add_argument("--poster", default="", help="also write a poster PNG here")
    ap.add_argument("--tolerance", type=float, default=2.0,
                    help="max allowed seconds of drift between video and narration")
    ap.add_argument("--keep-raw", default="", help="also keep the raw webm here")
    args = ap.parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"FATAL: {tool} not on PATH", file=sys.stderr)
            return 2

    spec = json.loads((HERE / "narration.json").read_text())
    if args.lang not in spec["langs"]:
        print(f"FATAL: unknown language {args.lang!r}; have {list(spec['langs'])}",
              file=sys.stderr)
        return 2
    scene_ids = list(spec["scenes"])
    if args.scenes:
        scene_ids = scene_ids[:args.scenes]

    audio_dir = HERE / "audio"
    clips = [audio_dir / args.lang / f"{s}.mp3" for s in scene_ids]
    missing = [str(c.relative_to(REPO)) for c in clips if not c.is_file()]
    if missing:
        print("FATAL: narration clip(s) missing: " + ", ".join(missing), file=sys.stderr)
        return 2

    # The expected length is MEASURED from the clips, not read from durations.json: that
    # file is an input to the page, and using it here would mean a stale entry silently
    # relaxes the very check meant to catch drift.
    expected = sum(_ffprobe_seconds(c) for c in clips)
    print(f"narration: {len(clips)} clip(s), {expected:.2f}s of audio in {args.lang}")

    with tempfile.TemporaryDirectory() as td:
        built = pathlib.Path(td) / "intro.html"
        subprocess.run([sys.executable, str(HERE / "build_intro.py"),
                        "--out", str(built)], check=True)

        vid_dir = pathlib.Path(td) / "vid"
        vid_dir.mkdir()
        # Generous: the browser plays in real time, so the floor is `expected`.
        raw, lead_in = record(built, audio_dir, args.lang, scene_ids, vid_dir,
                              timeout_s=expected + 120)
        # A lead-in outside this band means the measurement itself is not measuring what it
        # thinks it is (0 would mean audio was already playing before the click; 10s would
        # mean the click was not what started it), and every frame's alignment depends on it.
        if not 0.05 <= lead_in <= 10.0:
            print(f"FATAL: implausible lead-in {lead_in:.2f}s — the audio start could not be "
                  "located, so the trim would misalign the whole video", file=sys.stderr)
            return 1
        print(f"recorded {raw.name}: {_ffprobe_seconds(raw):.2f}s of video "
              f"(lead-in before narration: {lead_in:.2f}s, trimmed)")

        out = pathlib.Path(args.out)
        mux(raw, clips, out, lead_in=lead_in, audio_s=expected)
        if args.keep_raw:
            shutil.copy2(raw, args.keep_raw)
        if args.poster:
            poster(out, pathlib.Path(args.poster), at=min(12.0, expected / 3))

    got = _ffprobe_seconds(out)
    # What this check proves, and what it does not. Because mux() cuts the output to
    # `audio + TAIL_S`, a match here does NOT prove the front is aligned -- that rests on
    # lead_in having been measured to the first nonzero `audio.currentTime`, which is why it
    # is measured there and not guessed. What it DOES catch is the recording coming out
    # SHORT: ffmpeg cannot invent footage, so a page that stalled, a clip that never played,
    # or a deck that ended early all land here as a deficit. Both failure modes are silent to
    # a viewer who watches once, so both are measured rather than eyeballed.
    drift = got - (expected + TAIL_S)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {got:.2f}s video, "
          f"{expected:.2f}s narration + {TAIL_S:.1f}s tail, drift {drift:+.2f}s)")
    if abs(drift) > args.tolerance:
        # Fail rather than warn. A drifting video looks fine for the first scene and is
        # visibly wrong by the last, which is far too late for anyone to catch it.
        print(f"FATAL: {abs(drift):.2f}s of drift exceeds the {args.tolerance}s tolerance "
              "— picture and narration would not stay together", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
