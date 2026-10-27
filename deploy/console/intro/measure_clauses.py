#!/usr/bin/env python3
"""Measure when each English clause starts in its clip. A BUILD STEP, offline (ffmpeg only).

Run it after synth_narration.py re-synthesizes an English clip, and commit what it writes:

  intro/clauses.json   per scene: the clip's sha256, the text's sha256, and for every clause
                       (text up to a , . : ; or ?) its character offset and MEASURED start

Why it exists. The beats were authored in "English seconds" placed at each sentence's character
offset x the clip length, i.e. on the assumption that the voice speaks at an even rate. The
English voice is Polly's generative Matthew, which does not: it pauses at lists, slows on numbers
and runs short clauses together. Measured against the v2 clips, scene 6 drew "deploy", "test" and
"teardown" 2-3 s before they were said and "$0.45" 2.7 s early; scene 5 ran 1-2 s early
throughout. The owner watched all five languages on 2026-10-27 and reported the English as the one
that did not match. The other four languages scale the same authored times by their clip length
and were judged right, so they keep that path unchanged; only English reads a measured time.

How. A clause boundary is followed by a pause, so ffmpeg's silencedetect gives candidate starts.
There are usually a few more pauses than clauses (the voice breathes inside a long clause, and
pauses inside "Silver and Gold"), so a candidate is not simply the next pause.
Clause j is assigned the pause that keeps every clause's measured length closest to its length in
characters at the scene's average rate, by dynamic programming over the monotone assignments.
Taking the pause nearest each clause's estimated start instead is what the first attempt did, and
it shifted half of scene 6 by one clause: one extra pause early in the scene, and every later
clause took its neighbour's start.

The page builder (build_intro.py) refuses a clauses.json whose recorded sha256 no longer matches
the committed clip or the narration text, so a re-synthesized clip cannot ship with the old
clip's timings.

Usage:
  python3 deploy/console/intro/measure_clauses.py              # English, every scene
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
NARRATION = HERE / "narration.json"
DURATIONS = HERE / "durations.json"
CLAUSES = HERE / "clauses.json"
AUDIO = HERE / "audio"

#: A clause is text up to and including one of these. The same split is used to read
#: clauses.json back, so it is defined once.
CLAUSE = re.compile(r"[^,.:;?]+[,.:;?]?\s*")
#: Quieter than this for at least PAUSE_MIN seconds is a pause. -40 dB and 80 ms separate
#: Matthew's clause pauses (0.17-0.37 s on the v2 clips) from the gaps inside words.
NOISE_DB, PAUSE_MIN = -40, 0.08


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clauses(text: str) -> list:
    """(character offset, clause text) for every clause of a scene's narration."""
    return [(m.start(), m.group().strip()) for m in CLAUSE.finditer(text) if m.group().strip()]


def pause_ends(path: pathlib.Path) -> list:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=noise={NOISE_DB}dB:d={PAUSE_MIN}", "-f", "null", "-"],
        capture_output=True, text=True, check=True).stderr
    return [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", out)]


def align(lengths: list, pauses: list, dur: float) -> list:
    """Start seconds for each clause: clause 0 at 0, the others at pauses, in order.

    Minimises the summed |measured length - expected length| over all clauses, where expected
    is characters x the scene's average seconds per character.
    """
    rate = dur / sum(lengths)
    pts = [0.0] + [p for p in pauses if 0.05 < p < dur - 0.05]
    m, n = len(lengths), len(pts)
    if n < m:
        raise SystemExit(f"only {n - 1} pauses for {m} clauses: lower PAUSE_MIN or NOISE_DB")
    inf = float("inf")
    cost = [[inf] * n for _ in range(m)]
    back = [[0] * n for _ in range(m)]
    cost[0][0] = 0.0
    for j in range(1, m):
        for i in range(j, n):
            for k in range(j - 1, i):
                c = cost[j - 1][k] + abs((pts[i] - pts[k]) - rate * lengths[j - 1])
                if c < cost[j][i]:
                    cost[j][i], back[j][i] = c, k
    i = min(range(m - 1, n), key=lambda i: cost[m - 1][i] + abs((dur - pts[i]) - rate * lengths[-1]))
    starts = [0.0] * m
    for j in range(m - 1, 0, -1):
        starts[j] = pts[i]
        i = back[j][i]
    return starts


def measure(lang: str = "en") -> dict:
    spec = json.loads(NARRATION.read_text(encoding="utf-8"))
    durations = json.loads(DURATIONS.read_text(encoding="utf-8"))
    out = {}
    for sid in spec["scenes"]:
        text = spec["text"][lang][sid]
        clip = AUDIO / lang / f"{sid}.mp3"
        cl = clauses(text)
        starts = align([len(t) for _, t in cl], pause_ends(clip), durations[lang][sid])
        out[sid] = {"mp3_sha256": sha256(clip.read_bytes()),
                    "text_sha256": sha256(text.encode("utf-8")),
                    "clauses": [[o, round(s, 2), t] for (o, t), s in zip(cl, starts)]}
    return out


def main() -> int:
    doc = {"_comment": [
        "MEASURED start of every English clause, written by measure_clauses.py; do not edit.",
        "Beats name a clause with data-at=\"<index>\" or \"<index>+<seconds>\" and build_intro.py",
        "writes the resolved time as data-en, which the player uses for English only."],
        "en": measure("en")}
    CLAUSES.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    n = sum(len(s["clauses"]) for s in doc["en"].values())
    print(f"wrote {CLAUSES.relative_to(HERE.parent.parent.parent)}: {n} English clauses, "
          f"{len(doc['en'])} scenes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
