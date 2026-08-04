#!/usr/bin/env python3
"""Synthesize the Introduction tab's voice-over with Amazon Polly. A BUILD STEP.

Run this when `deploy/console/intro/narration.json` changes, commit the MP3s it writes,
then run `deploy/console/deploy.sh` -- which bundles them into the Lambda zip. The
console Lambda therefore needs NO Polly permission at runtime and makes no per-play
call: an mp3 is a static file inside the deployment package.

That is the whole reason this is a build step and not a route. The alternatives were
each worse in a specific way:

  * Synthesize on request -> the console's execution role needs `polly:SynthesizeSpeech`,
    and every listener pays for every replay. A page nobody visits costs nothing; a page
    that goes around a team costs per ear.
  * Presign an S3 object -> a URL that expires, so the page works when tested and is
    broken when shown, which is the failure mode that is hardest to notice in advance.
  * Browser speech synthesis alone -> no control over voice or pacing, absent in some
    browsers entirely, and it cannot be tested offline.

Bundling costs ~5 MB of zip and one re-run of this script when the script text changes.
The page still keeps browser TTS as a fallback for a missing file, so a synthesis that
was never run degrades to a robot voice rather than to silence.

Two outputs, both committed:
  intro/audio/<lang>/<scene>.mp3   the audio itself
  intro/durations.json             MEASURED seconds per clip, read by the page

The durations are measured from the returned MP3 frames rather than estimated from
character counts, because the page uses them to time its animation beats. An estimate
that is 15% long makes every beat land after the sentence that describes it -- the
reference implementation this page is modelled on ships estimates as a TTS-only
fallback and measured values for the real audio, and so does this.

Usage:
  python3 deploy/console/synth_narration.py                 # only what changed
  python3 deploy/console/synth_narration.py --force         # everything
  python3 deploy/console/synth_narration.py --dry-run       # print the calls, no AWS
  python3 deploy/console/synth_narration.py --lang ja       # one language
"""
import argparse
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
INTRO = HERE / "intro"
NARRATION = INTRO / "narration.json"
AUDIO = INTRO / "audio"
DURATIONS = INTRO / "durations.json"
#: What each clip was made from. Keyed by "<lang>/<scene>", value is a hash of the
#: (text, voice, engine) triple -- so editing one sentence re-synthesizes one clip and
#: leaves the other 34 files, and the other 34 lines of the git diff, untouched.
STAMPS = INTRO / "audio" / "_synth_stamps.json"

#: MPEG-1/2/2.5 Layer III, for measuring a clip without ffmpeg or a pip install. Polly
#: returns constant-bitrate MP3, but this walks every frame header and sums real frame
#: durations, so a VBR stream would also measure correctly rather than silently wrong.
_MPEG_VERSIONS = {0b11: 1, 0b10: 2, 0b00: 25}      # 0b01 is reserved
_SAMPLE_RATES = {1: (44100, 48000, 32000), 2: (22050, 24000, 16000),
                 25: (11025, 12000, 8000)}
_BITRATES_V1 = (None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None)
_BITRATES_V2 = (None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None)
#: Layer III carries 1152 samples per frame on MPEG-1 but only 576 on MPEG-2/2.5, so the
#: bytes-per-frame coefficient halves with it: 144000 vs 72000. Using the MPEG-1 number
#: for everything is a silent 2x error, not a parse failure -- it strides two frames at a
#: time, still lands on a valid sync word, and reports exactly half the real length. The
#: first run of this script did that: Polly's MP3 is MPEG-2 at 24 kHz, so every clip came
#: back at half its duration and the page would have cut each scene off mid-sentence. What
#: exposed it was not the parser but the content: 727 English characters in 21.2 seconds is
#: 342 words per minute, which no voice speaks. tests/test_intro_narration.py now pins both
#: coefficients against hand-built frame headers.
_SAMPLES_PER_FRAME = {1: 1152, 2: 576, 25: 576}
_FRAME_COEFF = {1: 144000, 2: 72000, 25: 72000}


def mp3_duration(data: bytes) -> float:
    """Seconds of audio in an MP3 byte string, by summing Layer III frame durations.

    Returns 0.0 for a stream with no parseable frame, which the caller treats as a hard
    failure: a zero-length clip would otherwise reach the page as a scene that advances
    instantly, and the cause (a truncated download) would be invisible there.
    """
    i, total, n = 0, 0.0, len(data)
    if data[:3] == b"ID3":                     # skip the tag Polly does not send, but
        size = 0                               # a future version or a re-encode might
        for b in data[6:10]:
            size = (size << 7) | (b & 0x7F)
        i = 10 + size
    while i + 4 <= n:
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
            i += 1
            continue
        ver = _MPEG_VERSIONS.get((data[i + 1] >> 3) & 0b11)
        layer = (data[i + 1] >> 1) & 0b11      # 0b01 == Layer III
        bi = (data[i + 2] >> 4) & 0b1111
        si = (data[i + 2] >> 2) & 0b11
        if ver is None or layer != 0b01 or si == 0b11:
            i += 1
            continue
        bitrate = (_BITRATES_V1 if ver == 1 else _BITRATES_V2)[bi]
        rate = _SAMPLE_RATES[ver][si]
        if not bitrate:
            i += 1
            continue
        pad = (data[i + 2] >> 1) & 1
        length = (_FRAME_COEFF[ver] * bitrate) // rate + pad
        if length <= 4:
            i += 1
            continue
        total += _SAMPLES_PER_FRAME[ver] / rate
        i += length
    return round(total, 2)


def stamp(text: str, voice: str, engine: str) -> str:
    return hashlib.sha256(
        "\x00".join((text, voice, engine)).encode("utf-8")).hexdigest()[:16]


def _write_durations(durations, langs, scenes) -> None:
    """Scene order, not insertion order, and a trailing newline.

    durations.json is committed, so its diff should show the clips that changed and
    nothing else. Re-keying by `scenes` also drops a stale entry for a scene that has
    been renamed out of narration.json, instead of leaving it to be served forever.
    """
    DURATIONS.write_text(json.dumps(
        {l: {s: durations[l][s] for s in scenes if s in durations.get(l, {})}
         for l in langs if l in durations},
        indent=2, ensure_ascii=False) + "\n")


def _report_totals(durations, langs, scenes) -> None:
    for lang in langs:
        have = durations.get(lang, {})
        if len(have) == len(scenes):
            total = sum(have.values())
            print(f"{lang:4s} {total:6.1f}s = {total / 60:.2f} min")
        else:
            print(f"{lang:4s} INCOMPLETE: {len(have)}/{len(scenes)} clips measured "
                  "-- the page will fall back to browser TTS for the rest")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="re-synthesize every clip, ignoring the stamps")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be synthesized; call no AWS API")
    ap.add_argument("--lang", action="append", default=None,
                    help="restrict to one language (repeatable)")
    ap.add_argument("--remeasure", action="store_true",
                    help="re-derive durations.json from the MP3s already on disk; "
                         "calls no AWS API and re-bills nothing")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    spec = json.loads(NARRATION.read_text())
    scenes, langs = spec["scenes"], spec["langs"]
    want = args.lang or list(langs)
    for lang in want:
        if lang not in langs:
            print(f"FATAL: --lang {lang} is not in narration.json ({', '.join(langs)})")
            return 2

    stamps = json.loads(STAMPS.read_text()) if STAMPS.exists() else {}
    durations = json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {}

    # --remeasure exists because the measurement can be wrong while the audio is right.
    # It was: the first pass used the MPEG-1 bytes-per-frame coefficient for Polly's
    # MPEG-2 stream and recorded every clip at half its real length. Re-synthesizing to
    # fix a parser bug would re-bill 35 clips to change nothing but a JSON file.
    if args.remeasure:
        n = 0
        for lang in want:
            for scene in scenes:
                path = AUDIO / lang / f"{scene}.mp3"
                if not path.exists():
                    print(f"  {lang}/{scene}: no mp3 on disk, skipping")
                    continue
                secs = mp3_duration(path.read_bytes())
                if secs <= 0:
                    print(f"FATAL: {path} has no parseable MP3 frame")
                    return 1
                was = durations.get(lang, {}).get(scene)
                durations.setdefault(lang, {})[scene] = secs
                n += 1
                if was != secs:
                    print(f"  {lang}/{scene:16s} {was} -> {secs}")
        _write_durations(durations, langs, scenes)
        print(f"re-measured {n} clip(s) from disk; no Polly call made")
        _report_totals(durations, langs, scenes)
        return 0

    todo = []
    for lang in want:
        for scene in scenes:
            cfg, text = langs[lang], spec["text"][lang][scene]
            key = f"{lang}/{scene}"
            want_stamp = stamp(text, cfg["voice"], cfg["engine"])
            path = AUDIO / lang / f"{scene}.mp3"
            fresh = (not args.force and path.exists()
                     and stamps.get(key) == want_stamp
                     and durations.get(lang, {}).get(scene))
            if fresh:
                continue
            todo.append((lang, scene, cfg, text, want_stamp, path))

    if not todo:
        print(f"nothing to synthesize: all {len(want) * len(scenes)} clips are current")
        return 0

    print(f"{len(todo)} clip(s) to synthesize"
          + (" (dry run)" if args.dry_run else f" via Polly in {args.region}"))
    for lang, scene, cfg, text, _, path in todo:
        print(f"  {lang}/{scene:16s} {cfg['voice']:8s} {cfg['engine']:11s} "
              f"{len(text):4d} chars -> {path.relative_to(HERE)}")
    if args.dry_run:
        return 0

    import boto3                     # imported here so --dry-run needs no credentials
    polly = boto3.client("polly", region_name=args.region)

    # Preflight every (voice, engine) pair against the live catalogue BEFORE the first
    # synthesize call. An unsupported engine fails per-clip, so without this a run would
    # write some files, fail on clip 12, and leave the audio set half-old and half-new --
    # and `engine` is exactly the field that goes stale, because Polly adds generative
    # support to voices over time and it reads like a preference rather than a fact.
    # Hiujin is neural-only today; asking it for generative is a hard error.
    catalogue = {v["Id"]: set(v["SupportedEngines"])
                 for v in polly.describe_voices()["Voices"]}
    bad = []
    for lang in want:
        cfg = langs[lang]
        have = catalogue.get(cfg["voice"])
        if have is None:
            bad.append(f"  {lang}: voice {cfg['voice']!r} does not exist in {args.region}")
        elif cfg["engine"] not in have:
            bad.append(f"  {lang}: voice {cfg['voice']} does not support engine "
                       f"{cfg['engine']!r}; it supports {sorted(have)}")
    if bad:
        print("FATAL: narration.json asks for a voice/engine Polly does not offer:")
        print("\n".join(bad))
        return 1

    for lang, scene, cfg, text, want_stamp, path in todo:
        # Polly's own cap is 3000 billed characters per call. Refuse rather than let it
        # truncate: a clip that stops mid-sentence is audible but not obviously WRONG,
        # and the scene would still advance on schedule.
        if len(text) > 3000:
            print(f"FATAL: {lang}/{scene} is {len(text)} chars; Polly's limit is 3000. "
                  "Split the scene or shorten the line.")
            return 1
        # No LanguageCode. Polly's codes are not the browser's BCP-47 tags -- Zhiyu is
        # `cmn-CN`, not `zh-CN`; Hiujin is `yue-CN`, not `zh-HK` -- so passing the tag the
        # page needs for SpeechSynthesis is a ValidationException, which is what a first
        # pass of this script did for all 14 Chinese clips. The parameter only means
        # anything for a bilingual voice, and each of these five is monolingual, so
        # omitting it lets every voice use its own language. See narration.json's
        # `_langs_comment`.
        audio = polly.synthesize_speech(
            Text=text, OutputFormat="mp3", VoiceId=cfg["voice"], Engine=cfg["engine"],
        )["AudioStream"].read()
        secs = mp3_duration(audio)
        if secs <= 0:
            print(f"FATAL: {lang}/{scene} returned {len(audio)} bytes with no parseable "
                  "MP3 frame; refusing to write a clip whose length is unknown")
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        stamps[f"{lang}/{scene}"] = want_stamp
        durations.setdefault(lang, {})[scene] = secs
        print(f"  wrote {path.relative_to(HERE)}  {len(audio) / 1024:6.1f} KiB  {secs:6.2f}s")

    # Sorted, indented, newline-terminated: these two files are committed, so their diff
    # should show only the clips that actually changed.
    STAMPS.write_text(json.dumps(stamps, indent=2, sort_keys=True) + "\n")
    _write_durations(durations, langs, scenes)
    _report_totals(durations, langs, scenes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
