/*
 * Drive the /intro player's real JavaScript against a stub DOM.
 *
 *     node tests/test_intro_player.js
 *
 * Why this exists rather than "the HTML parses": every interesting failure in a page
 * like this is a LIVE failure. A selector that matches nothing, an audio element whose
 * onended is never cleared, a scene that stops advancing when the mp3 is missing -- all
 * of those parse perfectly and are invisible until someone watches five minutes of
 * narration and notices it stopped. So this loads the BUILT page, extracts the player
 * script verbatim, and runs it against the smallest DOM that the player actually uses.
 *
 * It is not a browser. It deliberately does NOT emulate layout, CSS, or real audio
 * decoding -- those are not where the logic is. What it does emulate is the four things
 * the player's correctness depends on:
 *
 *   1. a virtual clock, so 300 seconds of narration run in milliseconds;
 *   2. an Audio object that can be told to SUCCEED or to FAIL, because the fallback path
 *      is the one a reviewer never sees and the one that runs on a bad deploy;
 *   3. speechSynthesis that never fires `onend`, which is a real Safari behaviour and the
 *      exact case the tick() safety net exists for;
 *   4. requestAnimationFrame driven by hand, so a test can assert what the page looks
 *      like at t=13.4s of scene 4.
 *
 * Offline by construction: it reads one local file and opens no socket.
 */
"use strict";
const fs = require("fs");
const path = require("path");

const PAGE = process.env.INTRO_HTML || path.join(__dirname, "..", "build", "intro.html");

let pass = 0;
const fails = [];
function ok(cond, what) {
  if (cond) { pass++; console.log("  PASS  " + what); }
  else { fails.push(what); console.log("  FAIL  " + what); }
}
function eq(actual, expected, what) {
  ok(actual === expected, `${what} (got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)})`);
}

// ── the stub DOM ─────────────────────────────────────────────────────────────
// Elements know only what the player touches: classList, textContent/innerHTML, a
// dataset, children, and the handful of properties the transport sets.
class ClassList {
  constructor(el) { this.el = el; this.s = new Set(); }
  add(c) { this.s.add(c); }
  remove(c) { this.s.delete(c); }
  contains(c) { return this.s.has(c); }
  toggle(c, on) { if (on === undefined) on = !this.s.has(c); on ? this.s.add(c) : this.s.delete(c); }
  get value() { return [...this.s].join(" "); }
}
class El {
  constructor(tag) {
    this.tagName = (tag || "div").toUpperCase();
    this.classList = new ClassList(this);
    this.dataset = {};
    this.style = {};
    this.children = [];
    this.childNodes = this.children;
    this._text = "";
    this.attrs = {};
    this.clientWidth = 1180;
  }
  get firstChild() { return this.children[0]; }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  /* innerHTML has to actually BUILD children, not just record a string: the progress
   * bar is `sg.innerHTML = "<i></i>"` and tick() then writes to `sg.firstChild.style`.
   * A stub that only stored the string would make every segment's fill a no-op and the
   * test would pass on a page whose progress bar never moves. Only the empty-element
   * form is parsed -- that is the only form the player uses to create nodes; caption
   * markup is read back as a string. */
  set innerHTML(v) {
    this._html = v;
    this.children.length = 0;
    for (const m of String(v).matchAll(/<([a-z]+)\s*\/?>/gi)) this.appendChild(new El(m[1]));
  }
  get innerHTML() { return this._html || ""; }
  // className and classList are two views of one set in a browser; keep them so here,
  // or a test asserting classList.contains() silently misses what the page set.
  set className(v) {
    this.classList.s = new Set(String(v).split(/\s+/).filter(Boolean));
  }
  get className() { return this.classList.value; }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  // querySelectorAll on an element is only used for ".beat" inside one scene
  querySelectorAll(sel) {
    if (sel === ".beat") return this.beats || [];
    return [];
  }
}

/* Parse the built page's scenes into stub elements: one per <section class="scene">,
 * each carrying its real data-t beats. The timings under test are the ones in the
 * artifact, not a copy of them -- a test that re-declares the beats would pass while
 * the page shipped different numbers. */
function parseScenes(html) {
  const out = new Map();
  const re = /<section class="scene" id="([^"]+)"([\s\S]*?)<\/section>/g;
  let m;
  while ((m = re.exec(html))) {
    const el = new El("section");
    el.id = m[1];
    el.beats = [...m[2].matchAll(/data-t="([0-9.]+)"/g)].map(b => {
      const beat = new El("div");
      beat.dataset.t = b[1];
      return beat;
    });
    out.set(m[1], el);
  }
  return out;
}

const html = fs.readFileSync(PAGE, "utf8");
const scenes = parseScenes(html);

// ── virtual clock + rAF ──────────────────────────────────────────────────────
let NOW = 0;                          // ms
const rafQueue = [];
/* End the clip that is currently playing. A helper rather than `audioLive.endNow()` at
 * the call site: if the page failed to start a clip at all, audioLive is null and the
 * bare call throws, aborting the run instead of reporting the failure. */
function endLiveClip(what) {
  if (!audioLive) { ok(false, `${what} -- but no clip was playing to end`); return false; }
  audioLive.endNow();
  return true;
}

function stepFrames(ms, perFrame = 16) {
  const end = NOW + ms;
  while (NOW < end) {
    NOW = Math.min(end, NOW + perFrame);
    if (audioLive) audioLive.currentTime = (NOW - audioLive._t0) / 1000;
    const q = rafQueue.splice(0);
    q.forEach(fn => fn(NOW));
  }
}

// ── stub Audio ───────────────────────────────────────────────────────────────
// AUDIO_MODE flips the whole page between "the mp3s are in the zip" and "they are not",
// which is the difference between the demo path and the degraded path.
let AUDIO_MODE = "ok";
let audioLive = null;
const audioSrcs = [];
class StubAudio {
  constructor(src) {
    this.src = src; audioSrcs.push(src);
    this.currentTime = 0; this.muted = false; this.paused = true;
    this._t0 = NOW;
  }
  play() {
    if (AUDIO_MODE === "fail") return Promise.reject(new Error("NotAllowedError"));
    this.paused = false; this._t0 = NOW - this.currentTime * 1000; audioLive = this;
    return Promise.resolve();
  }
  pause() { this.paused = true; if (audioLive === this) audioLive = null; }
  // the player never calls this; the test does, to simulate the clip running out
  endNow() { if (this.onended) this.onended(); }
  failNow() { if (this.onerror) this.onerror(); }
}

// ── stub speechSynthesis: speak() works, onend NEVER fires (the Safari case) ──
let spoken = [];
const speechSynthesis = {
  speak(u) { spoken.push({ text: u.text, lang: u.lang }); },
  cancel() {}, pause() {}, resume() {},
};
class SpeechSynthesisUtterance {
  constructor(t) { this.text = t; }
}

// ── document / window ────────────────────────────────────────────────────────
const byId = new Map();
for (const id of ["wrap", "stage", "gate", "gateLang", "goBtn", "segs", "bar",
                  "playBtn", "prevBtn", "nextBtn", "muteBtn", "langSel", "clock",
                  "cap", "ttsNote"]) {
  byId.set(id, new El(id === "langSel" || id === "gateLang" ? "select" : "div"));
}
for (const [id, el] of scenes) byId.set(id, el);

const document = {
  documentElement: new El("html"),
  getElementById: id => byId.get(id) || null,
  createElement: t => new El(t),
  addEventListener(type, fn) { if (type === "keydown") document._onkey = fn; },
  querySelector(sel) {
    // only form used: '#<sceneId> [data-title]'
    const m = /^#(\S+) \[data-title\]$/.exec(sel);
    if (m) {
      const s = byId.get(m[1]);
      if (!s) return null;
      if (!s._titleEl) s._titleEl = new El("div");
      return s._titleEl;
    }
    return null;
  },
  querySelectorAll(sel) {
    const m = /^#(\S+) \.beat$/.exec(sel);
    if (m) { const s = byId.get(m[1]); return s ? s.beats : []; }
    return [];
  },
};

const localStore = new Map();
const sandbox = {
  document,
  window: { ResizeObserver: null, addEventListener() {}, speechSynthesis },
  localStorage: {
    getItem: k => (localStore.has(k) ? localStore.get(k) : null),
    setItem: (k, v) => localStore.set(k, String(v)),
  },
  performance: { now: () => NOW },
  requestAnimationFrame: fn => { rafQueue.push(fn); return rafQueue.length; },
  cancelAnimationFrame: () => { rafQueue.length = 0; },
  Audio: StubAudio,
  SpeechSynthesisUtterance,
  speechSynthesis,
  console,
  Math, JSON, String, Number, Object, Array, Set, Map, Error, parseFloat, parseInt,
  isNaN, Boolean, Promise,
};
sandbox.window.document = document;
sandbox.window.localStorage = sandbox.localStorage;

// ── load and run the player ──────────────────────────────────────────────────
const js = html.slice(html.lastIndexOf("<script>") + "<script>".length,
                      html.lastIndexOf("</script>"));
const player = new Function(...Object.keys(sandbox), js + "\n;return {" +
  "SCENES,LANGS,DUR,NARR,TITLES,go,toggle,setLang,sceneDur,beatScale,estDur," +
  "get idx(){return idx}, get playing(){return playing}, get audio(){return audio}," +
  "get LANG(){return LANG}};")(...Object.values(sandbox));

const P = player;
const $ = id => byId.get(id);
const beatsIn = sid => byId.get(sid).beats.filter(b => b.classList.contains("in")).length;

console.log(`\nintro player · ${PAGE}\n`);

/* Everything below is inside an async IIFE for ONE reason: the degraded-path test has
 * to let a rejected play() promise settle, and a top-level await would make node parse
 * this file as an ES module -- which `require` above forbids. */
(async () => {

// ── 1 · first paint ──────────────────────────────────────────────────────────
console.log("first paint");
eq(P.SCENES.length, 7, "7 scenes");
eq(P.LANG, "en", "defaults to English");
eq(Object.keys(P.LANGS).length, 5, "5 languages offered");
eq($("langSel").children.length, 5, "language <select> is populated");
eq($("gateLang").children.length, 5, "the gate's picker is populated too");
eq($("segs").children.length, 7, "one progress segment per scene");
ok($(P.SCENES[0]).classList.contains("on"), "scene 1 is visible behind the gate");
ok(!$("gate").classList.contains("off"), "the gate is up: no audio without a gesture");
eq(P.idx, -1, "nothing is playing yet");
ok($("clock").textContent.endsWith("/ 5:04"), `clock shows the English total (${$("clock").textContent})`);

// The segments must be proportional to real measured durations, not equal width: a
// 68s Japanese scene and a 41s English one are not the same slice of five minutes.
const grows = $("segs").children.map(c => parseFloat(c.style.flexGrow));
ok(Math.abs(grows.reduce((a, b) => a + b, 0) - 1) < 1e-6, "segment widths sum to 1");
ok(new Set(grows.map(g => g.toFixed(4))).size > 1, "segment widths differ by scene length");

// ── 2 · the happy path: mp3 plays, scene advances on ended ───────────────────
console.log("\nplaying with the bundled mp3s");
AUDIO_MODE = "ok";
P.go(0, true);
eq(P.idx, 0, "go(0) selects scene 1");
ok($("gate").classList.contains("off"), "the gate is dismissed on play");
eq(audioSrcs[audioSrcs.length - 1], "/intro/audio/en/s1-problem.mp3",
   "requests the English scene-1 clip from the bundled path");
eq($("playBtn").textContent, "❚❚ Pause", "the button shows Pause while playing");

stepFrames(20000);
ok(beatsIn("s1-problem") > 4, `beats fire as the clock advances (${beatsIn("s1-problem")} in at 20s)`);
ok($("cap").innerHTML.length > 10, "a caption is showing");
ok($("clock").textContent.startsWith("0:2"), `clock tracks the audio (${$("clock").textContent})`);

const seg0 = $("segs").children[0].firstChild;
ok(parseFloat(seg0.style.width) > 40 && parseFloat(seg0.style.width) < 55,
   `scene 1's segment is ~47% full at 20s of 42.5s (${seg0.style.width})`);

// Every beat must be IN by the end of the clip -- that is the whole contract of
// authoring beats in English seconds.
stepFrames(23000);
eq(beatsIn("s1-problem"), byId.get("s1-problem").beats.length,
   "every scene-1 beat has fired by the end of the clip");

P.go(0, true);            // reset for the advance test
stepFrames(1000);
endLiveClip("onended advances to scene 2");   // the mp3 runs out
eq(P.idx, 1, "onended advances to scene 2");
ok($(P.SCENES[1]).classList.contains("on"), "scene 2 is visible");
ok(!$(P.SCENES[0]).classList.contains("on"), "scene 1 is hidden");
eq(beatsIn("s1-problem"), 0, "leaving a scene resets its beats, so a replay replays");

// ── 3 · jumping must not be overtaken by the clip it interrupted ─────────────
console.log("\njumping between scenes");
P.go(2, true);
stepFrames(500);
const interrupted = audioLive;
P.go(5, true);                 // user clicks segment 6 while scene 3 is playing
if (interrupted) interrupted.endNow();   // the old handler, if still attached, fires here
eq(P.idx, 5, "the interrupted clip's onended cannot drag the page back a scene");
eq(audioSrcs[audioSrcs.length - 1], "/intro/audio/en/s6-results.mp3",
   "the new scene's clip is the one loaded");

// ── 4 · pause / resume ───────────────────────────────────────────────────────
console.log("\npause and resume");
P.go(0, true);
stepFrames(8000);
const tAtPause = $("clock").textContent;
P.toggle();
eq(P.playing, false, "toggle pauses");
eq($("playBtn").textContent, "▶ Play", "the button offers Play again");
stepFrames(6000);
eq($("clock").textContent, tAtPause, "the clock does not advance while paused");
P.toggle();
eq(P.playing, true, "toggle resumes");
stepFrames(3000);
ok($("clock").textContent !== tAtPause, "the clock advances again after resume");

// ── 5 · the degraded path: no mp3 in the zip ─────────────────────────────────
// This is the case a reviewer never sees and a bad deploy always hits.
console.log("\nno mp3 available (bad deploy / blocked autoplay)");
AUDIO_MODE = "fail";
spoken = [];
P.go(0, true);
stepFrames(50);                       // let the rejected play() promise settle
await new Promise(r => setImmediate(r));
eq(P.audio, null, "the audio element is dropped after play() rejects");
eq(spoken.length, 1, "browser speech takes over");
// `(spoken[0] || {}).lang` rather than `spoken[0].lang`: when the fallback is what
// broke, spoken is EMPTY, and indexing it throws -- which aborts the run and skips
// every later section, hiding whatever else the same change broke. A test harness must
// report a failure, not become one. (Found by mutating the play() catch away.)
eq((spoken[0] || {}).lang, "en-US", "the utterance carries the BCP-47 tag, not Polly's code");
ok(!$("ttsNote").classList.contains("off"), "the page SAYS it is using browser speech");

// speechSynthesis.onend never fires in this stub -- exactly the Safari behaviour. The
// page must still advance, or the narration dies on scene 1 with no error anywhere.
stepFrames(45000);
ok(P.idx >= 1, `the TTS safety net advances the scene without onend (now on ${P.idx})`);
ok(beatsIn("s1-problem") === 0 || P.idx > 1, "scene 1 was left behind, beats reset");

AUDIO_MODE = "ok";

// ── 6 · language switching ───────────────────────────────────────────────────
console.log("\nswitching language");
P.go(3, true);
stepFrames(4000);
P.setLang("ja");
eq(P.LANG, "ja", "language changed");
eq(P.idx, 3, "the scene is kept across a language change");
eq(audioSrcs[audioSrcs.length - 1], "/intro/audio/ja/s4-build.mp3",
   "the Japanese clip for the SAME scene is loaded");
eq(localStore.get("introLang"), "ja", "the choice is remembered");
eq(document.documentElement.lang, "ja-JP", "<html lang> follows the narration");

/* setLang rebuilds the progress segments synchronously, so THOSE must already carry
 * Japanese proportions. Checked by re-deriving every fraction from the page's own DUR
 * table rather than by a threshold on one segment: translations pace roughly
 * proportionally, so most scenes' share of the total barely moves (s4 is 0.1435 of the
 * narration in BOTH languages, to four decimals). A threshold on s4 is therefore
 * unfalsifiable -- it passed even when setLang was mutated to skip buildSegs() entirely.
 * The full vector does move: s7 goes 0.1404 -> 0.1537 because Japanese spends longer on
 * the console walkthrough. */
const jaTotal = P.SCENES.reduce((a, s) => a + P.sceneDur(s, "ja"), 0);
const wrongScene = P.SCENES.filter((sid, i) => Math.abs(
  parseFloat($("segs").children[i].style.flexGrow) - P.sceneDur(sid, "ja") / jaTotal) > 1e-6);
eq(wrongScene.length, 0,
   `every segment was rebuilt to its Japanese share (off: ${wrongScene.join(",") || "none"})`);

// The clock, by contrast, is repainted by requestAnimationFrame, so while the page is
// playing it necessarily lags a language change by one frame (16ms in a browser). Step
// one frame rather than assert a paint that has not happened -- and then insist it did
// happen, because a total that stayed English would mean the whole progress model still
// thought the narration was 5:04 long.
stepFrames(20);
ok($("clock").textContent.endsWith("/ 7:27"), `the total is Japanese, not English (${$("clock").textContent})`);

// Beat scaling: Japanese s4 is 64.18s against English 43.61s, so a beat authored at
// 20s must fire ~29.4s in. Without scaling, the last beat of a Japanese scene would
// land 20 seconds before the sentence that describes it.
const k = P.beatScale("s4-build");
ok(k > 1.4 && k < 1.5, `Japanese beats are scaled by ${k.toFixed(3)}`);
eq(P.sceneDur("s4-build", "en"), 43.61, "English s4 duration is the measured one");
eq(P.sceneDur("s4-build", "ja"), 64.18, "Japanese s4 duration is the measured one");

P.go(3, true);
stepFrames(21000);
const enBeatsAt21 = 12;   // authored beats at or before 21s in s4
ok(beatsIn("s4-build") < enBeatsAt21,
   `at 21s the Japanese scene is behind the English beat schedule (${beatsIn("s4-build")} in)`);
stepFrames(31000);        // 52s: past 34s * 1.47
eq(beatsIn("s4-build"), byId.get("s4-build").beats.length,
   "and every beat has still fired by the end of the longer Japanese clip");

// every language must resolve a duration for every scene, or the page silently
// falls back to a character-count estimate for that clip
console.log("\nall five languages");
for (const lang of Object.keys(P.LANGS)) {
  const miss = P.SCENES.filter(s => !(P.DUR[lang] && P.DUR[lang][s]));
  eq(miss.length, 0, `${lang}: every scene has a measured duration`);
  const t = P.SCENES.reduce((a, s) => a + P.sceneDur(s, lang), 0);
  ok(t > 240 && t < 480, `${lang}: total ${(t / 60).toFixed(2)} min is in the 4-8 min band`);
  ok(P.SCENES.every(s => (P.NARR[lang] || {})[s]), `${lang}: narration text for all 7 scenes`);
  ok(P.SCENES.every(s => (P.TITLES[lang] || {})[s]), `${lang}: a title for all 7 scenes`);
}

// ── 7 · running off the end ──────────────────────────────────────────────────
console.log("\nthe end");
P.setLang("en");
P.go(6, true);
stepFrames(500);
endLiveClip("the last clip ends");
eq(P.playing, false, "the page stops after the last scene");
eq($("playBtn").textContent, "↻ Replay", "and offers a replay");

console.log(`\n${pass} passed, ${fails.length} failed`);
if (fails.length) { fails.forEach(f => console.log("  - " + f)); process.exit(1); }
})();
