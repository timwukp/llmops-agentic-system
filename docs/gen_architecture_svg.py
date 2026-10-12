#!/usr/bin/env python3
"""Generate the three architecture SVGs in the house style (dark navy cards,
per-tier accent strokes, animated dashed wires, rounded corners, clickable cards).

Layout law (enforced by tests/test_svg_geometry.py): one corridor per wire,
distinct anchor edges per target — no two wires may cross, no two may share a
corridor, and none may pass through a card. Regenerate + re-verify after every
edit; never hand-edit the SVGs.

Every label here is a claim about the running system, so each is derived from a
file in this repo and cross-checked against the deployed resource. That check has
caught the same failure mode three times now, and the third catch is the one worth
keeping, because what it falsified was this docstring:

  * "VPC-isolated in production" — no live harness carries a
    networkConfiguration, so all 7 run PUBLIC. ARCHITECTURE.md §11 says as much;
    the diagram was asserting the aspiration as shipped fact.
  * "git in dev, S3 mirror in prod" — corrected once to "all 19 mounts are `git`;
    the mirror exists but nothing is switched to it", which was true when written.
  * That correction was then falsified by the migration it described. All 19 skill
    mounts across all 7 live harnesses are `s3`; no `git` source exists anywhere.
    The band text and this paragraph both went on saying `git` afterwards — a
    corrected claim decaying back into a false one, with the correction's own prose
    vouching for it.

So stating the right value is not the fix; deriving it is.
tests/test_docs_claims.py::test_the_diagram_text_states_the_real_skill_source_kind
reads the harness configs and requires the band to name whichever kind is actually
configured, so the next migration fails a test instead of ageing a comment.
"""
import glob as _glob
import json as _json
import os
import re as _re

OUT = os.path.dirname(os.path.abspath(__file__))
REPO = "https://github.com/timwukp/llmops-agentic-system/blob/main/"


def _repo_root():
    """Locate the repo whose configs the labels are derived from.

    `docs/..` normally, but tests/test_svg_geometry.py copies this file into a temp
    dir and runs it with cwd=repo to compare bytes, so `docs/..` is that temp dir
    there. Falling back to cwd keeps that comparison meaningful; without it the
    generator would read no configs under test and could emit anything.
    """
    for root in (os.path.dirname(OUT), os.getcwd()):
        if os.path.isdir(os.path.join(root, "agents")):
            return root
    raise SystemExit(
        "cannot find the agents/ configs this generator derives its labels from; "
        "run it from the repo root (python3 docs/gen_architecture_svg.py)")


def skill_mounts():
    """(count, kind) of skill sources across every live harness config.

    Derived, not written down. The band used to state the kind as prose and it went
    on saying `git` for two days after every mount became `s3` — see the module
    docstring. A generator that reads the configs cannot make that mistake; a stale
    committed SVG is then caught twice over, by the geometry suite's byte comparison
    against this generator and by the docs-claims guard on the band text itself.
    """
    root, kinds = _repo_root(), {}
    cfgs = sorted(_glob.glob(os.path.join(root, "agents", "*", "harness*.json")))
    assert cfgs, f"no agents/*/harness*.json under {root}"
    for cfg in cfgs:
        with open(cfg) as fh:
            for skill in _json.load(fh).get("skills") or []:
                for kind in ("git", "s3", "path", "awsSkills"):
                    if kind in skill:
                        kinds[kind] = kinds.get(kind, 0) + 1
    total = sum(kinds.values())
    # A mixed fleet is a real condition, not a label problem: some harnesses would
    # read a pinned snapshot and others float on a branch. Say so rather than
    # picking a majority kind and printing a sentence that is half true.
    if len(kinds) != 1:
        return total, "+".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    return total, next(iter(kinds))


def single_run_limit():
    """The console's single-run budget reference, read out of the module that owns it.

    Same reason as `skill_mounts()`: the band used to state `$2000 gate` as prose, and on
    2026-08-02 the reference became $20,000 -- a diagram label that quotes a number nothing
    derives is a claim that ages into a false one, which is the failure this whole generator
    docstring is about. Parsed rather than imported because this script must stay runnable
    from a temp dir with no repo on sys.path (see `_repo_root`).
    """
    src = os.path.join(_repo_root(), "pipeline", "contracts", "cost_model.py")
    with open(src) as fh:
        for line in fh:
            if line.startswith("DEFAULT_SINGLE_RUN_LIMIT_USD"):
                # float() accepts the `20_000.0` underscore form directly; no stripping.
                return float(line.split("=", 1)[1].split("#")[0].strip())
    raise SystemExit(f"DEFAULT_SINGLE_RUN_LIMIT_USD not found in {src}")


def console_tabs():
    """Count the console's nav tabs by reading frontend.html.

    Was the literal `8 tabs`, and adding the Introduction tab made it false — caught by
    test_the_tabs_in_the_docs_match_the_frontend, which derives the same number from the
    same file. Hand-editing the SVG would have satisfied that guard while leaving the
    GENERATOR still emitting 8, so the next regeneration would quietly restore the wrong
    label: a fix that survives until someone runs the tool is not a fix. Same reason
    HARNESS_N and LIMIT_USD are derived rather than typed.

    Counted from `data-tab="…"` on the nav buttons, deduplicated because each tab's label
    also appears in the panel markers the guard cross-checks.
    """
    src = os.path.join(_repo_root(), "deploy", "console", "frontend.html")
    with open(src, encoding="utf-8") as fh:
        tabs = set(_re.findall(r'<button data-tab="([a-z-]+)"', fh.read()))
    if not tabs:
        raise SystemExit(f"no nav tabs found in {src} — the parse broke, not the console")
    return len(tabs)


SKILL_N, SKILL_KIND = skill_mounts()
LIMIT_USD = single_run_limit()
HARNESS_N = len(sorted(_glob.glob(os.path.join(_repo_root(), "agents", "*", "harness.json"))))
TAB_N = console_tabs()

STYLE = """
  <defs>
    <marker id="ahB" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M0,0 L8,4 L0,8 Z" fill="#5b8cff"/>
    </marker>
    <marker id="ahG" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M0,0 L8,4 L0,8 Z" fill="#3ecf8e"/>
    </marker>
    <marker id="ahO" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M0,0 L8,4 L0,8 Z" fill="#ffb454"/>
    </marker>
    <marker id="ahR" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M0,0 L8,4 L0,8 Z" fill="#ff6b81"/>
    </marker>
    <marker id="ahP" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M0,0 L8,4 L0,8 Z" fill="#b48cff"/>
    </marker>
    <style>
      .bg    { fill:#0b1020; }
      .card  { fill:#141b33; stroke-width:2; rx:12; }
      .cTrig { stroke:#b48cff; } .cSpine{ stroke:#5b8cff; } .cAgent{ stroke:#ffb454; }
      .cAws  { stroke:#3ecf8e; } .cOps  { stroke:#ff6b81; } .cDim  { stroke:#26305a; }
      .title { fill:#e7ecff; font-size:14px; font-weight:650; }
      .sub   { fill:#8592c0; font-size:10px; }
      .band  { fill:#101731; stroke:#26305a; stroke-width:1.5; }
      .bandT { fill:#8592c0; font-size:11px; font-weight:600; letter-spacing:1px; }
      .wire  { stroke:#5b8cff; stroke-width:2.5; fill:none; stroke-dasharray:7 6; animation:dash 1.1s linear infinite; }
      .wireG { stroke:#3ecf8e; stroke-width:2.5; fill:none; stroke-dasharray:7 6; animation:dash 1.1s linear infinite; }
      .wireO { stroke:#ffb454; stroke-width:2.5; fill:none; stroke-dasharray:7 6; animation:dash 1.1s linear infinite; }
      .wireR { stroke:#ff6b81; stroke-width:2.5; fill:none; stroke-dasharray:7 6; animation:dash 1.4s linear infinite; }
      .wireP { stroke:#b48cff; stroke-width:2.5; fill:none; stroke-dasharray:7 6; animation:dash 1.4s linear infinite; }
      .wireDim { stroke:#26305a; stroke-width:2; fill:none; stroke-dasharray:4 5; }
      @keyframes dash { to { stroke-dashoffset:-13; } }
      a { cursor:pointer; }
    </style>
  </defs>
"""


def card(x, y, w, h, cls, icon, name, sub, href=None):
    cx = x + w / 2
    inner = f'''    <g>
      <rect class="card {cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>
      <text class="title" x="{cx}" y="{y+24}" text-anchor="middle">{icon} {name}</text>
      <text class="sub" x="{cx}" y="{y+42}" text-anchor="middle">{sub}</text>
    </g>'''
    if href:
        return f'  <a href="{REPO}{href}" target="_top">\n{inner}\n  </a>\n'
    return inner + "\n"


def wire(d, cls="wire", marker="ahB"):
    return f'  <path class="{cls}" d="{d}" marker-end="url(#{marker})"/>\n'


# ================= HIGH-LEVEL (1240 x 1060) =================
W, H = 1240, 1060
CW, CH = 180, 56
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="system-ui,-apple-system,\'Segoe UI\',Roboto,sans-serif">']
svg.append(STYLE)
svg.append(f'  <rect class="bg" x="0" y="0" width="{W}" height="{H}" rx="16"/>')
svg.append('  <text class="title" x="30" y="40" font-size="18">llmops-agentic-system — autonomous LLMOps on AgentCore</text>')
svg.append('  <text class="sub" x="30" y="60">teacher DeepSeek-R1 (Bedrock) → student Qwen3-1.7B (SageMaker QLoRA) · 7 harnesses, all on Fable 5 · click any card</text>')

# Column 1 (x=30): triggers, stacked
trig_y = [92, 168, 244, 320]
trigs = [("⏰", "EventBridge Scheduler", "cron · nightly runs"),
         ("🐙", "GitHub Actions", "OIDC · fire-and-monitor"),
         ("🖥️", "Admin API", "Cognito · POST /runs"),
         ("🪝", "Webhook", "HMAC-verified")]
for (icon, name, sub), y in zip(trigs, trig_y):
    svg.append(card(30, y, CW, CH, "cTrig", icon, name, sub, "docs/TRIGGERS.md"))


# Column 2 (x=286): conductor on top, then spine, stacked
svg.append(card(286, 92, CW, CH, "cAgent", "🎼", "llmops_orchestrator", "goal → plan → dispatch · triage", "agents/orchestrator/harness.json"))
svg.append(card(286, 168, CW, CH, "cSpine", "🚀", "start_pipeline λ", "run_id · manifest seed", "orchestration/start_pipeline/handler.py"))
svg.append(card(286, 282, CW, CH, "cSpine", "🧭", "Step Functions", "stage DAG · waitForTaskToken", "orchestration/state_machine.asl.json"))
svg.append(card(286, 396, CW, CH, "cSpine", "🔌", "harness-driver λ", "InvokeHarness · inline-fn loop", "orchestration/harness_driver/handler.py"))

# triggers -> conductor (goal intake; each its own corridor into left edge, staggered entry y)
sx = 286  # conductor card left edge
for i, y in enumerate(trig_y):
    src_y = y + CH / 2
    dst_y = 92 + 12 + i * 11   # staggered anchors on left edge of the conductor
    mid_x = 232 + i * 12       # separate vertical corridors
    svg.append(wire(f"M{30+CW},{src_y} L{mid_x},{src_y} L{mid_x},{dst_y} L{sx-4},{dst_y}", "wireP", "ahP"))

# conductor -> start (dispatch) -> SFN -> driver (vertical, distinct corridors)
svg.append(wire(f"M{286+CW/2},{92+CH} L{286+CW/2},{168-4}", "wireO", "ahO"))
svg.append(wire(f"M{286+CW/2},{168+CH} L{286+CW/2},{282-4}", "wire", "ahB"))
svg.append(wire(f"M{286+CW/2},{282+CH} L{286+CW/2},{396-4}", "wire", "ahB"))

# Column 3 (x=560): the five harnesses, stacked
h_y = [92, 190, 288, 386, 484]
agents = [("📥", "llmops_data_prep", "distill data · guardrails", "agents/data-prep/harness.json"),
          ("🏋️", "llmops_finetune", "QLoRA launch-and-release", "agents/finetune/harness.json"),
          ("🧾", "llmops_eval", "judge gates · vs teacher", "agents/eval/harness.json"),
          ("🚢", "llmops_deploy", "endpoint · smoke · teardown", "agents/deploy/harness.json"),
          ("📈", "llmops_monitor", "drift · cost sweep", "agents/monitor/harness.json")]
for (icon, name, sub, href), y in zip(agents, h_y):
    svg.append(card(560, y, CW, CH, "cAgent", icon, name, sub, href))

# driver -> each harness (fan out from right edge of driver, own corridor each, into left edges)
dv_x, dv_y = 286 + CW, 396 + CH / 2
for i, y in enumerate(h_y):
    dst_y = y + CH / 2
    mid_x = 490 + i * 13
    svg.append(wire(f"M{dv_x},{dv_y - 22 + i*11} L{mid_x},{dv_y - 22 + i*11} L{mid_x},{dst_y} L{556},{dst_y}", "wireO", "ahO"))

# Column 4 (x=850): AWS services
svg.append(card(850, 92, CW, CH, "cAws", "🧠", "Bedrock", "DeepSeek-R1 teacher · Fable 5", "docs/ARCHITECTURE.md"))
# The trainer link must name the file deploy/03_storage.py ensure_code() mirrors -- for
# months it named a second copy that no run could reach, so the diagram documented a
# trainer nothing deployed. tests/test_svg_geometry.py derives the path from ensure_code.
svg.append(card(850, 240, CW, CH, "cAws", "🎓", "SageMaker Training", "Qwen3-1.7B QLoRA job", "pipeline/training/distill/train_qlora.py"))
svg.append(card(850, 388, CW, CH, "cAws", "🌐", "SageMaker Endpoint", "student inference · g5.xlarge", "docs/ARCHITECTURE.md"))

# harnesses -> AWS (data-prep->bedrock, finetune->training, deploy->endpoint; own corridors)
svg.append(wire(f"M{560+CW},{92+CH/2} L{846},{92+CH/2}", "wireG", "ahG"))
# Enters Training's left edge at y=252, not at its mid-height y=268: the
# "gate fail → remediate" label occupies y 262..274 across x 738..873, so ANY
# horizontal leg at 268 struck through it -- moving the corner sideways cannot help,
# only going above the label can. Both legs now sit above y=262.
svg.append(wire(f"M{560+CW},{190+CH/2} L{800},{190+CH/2} L{800},{252} L{846},{252}", "wireG", "ahG"))
svg.append(wire(f"M{560+CW},{386+CH/2} L{800},{386+CH/2} L{800},{388+CH/2+14} L{846},{388+CH/2+14}", "wireG", "ahG"))

# training-complete resume: SageMaker Training -> EventBridge rule -> Step Functions.
#
# Both wires here used to pierce cards, invisibly to the old geometry check
# (it sampled only segment ENDPOINTS, so a wire spanning a card looked fine):
#   * the Training->rule drop ran down x=940, straight through the SageMaker
#     Endpoint card at (850,388);
#   * the rule->SFN return ran along y=533 from x=850 to x=376, crossing the
#     llmops_monitor card and ENDING INSIDE the driver card.
#
# The state machine is boxed in on all four sides -- conductor and start_pipeline
# above, driver below, the trigger fan-in left, the driver's five-way fan-out
# right -- so this feedback wire gets a dedicated lane rather than a shortcut:
# out of the rule's bottom, down the clear column at x=1035 (between the
# SageMaker column that ends at 1030 and the console that starts at 1040), west
# along y=545 (below every pipeline card, above the audit row at y=560), then up
# x=272 (between the trigger column ending at 210 and the spine at 286, and to
# the right of the fan-in corridors that stop at 268) into the state machine's
# left edge. The rule card moves beside Training instead of below the Endpoint,
# which is what forced the original wire through a card in the first place.
svg.append(card(1040, 240, CW, CH, "cSpine", "📡", "EventBridge rule", "job state change → resume λ", "orchestration/resume_pipeline/handler.py"))
svg.append(wire(f"M{850+CW},{260} L{1036},{260}", "wire", "ahB"))
svg.append(wire(f"M{1040+CW/2},{240+CH} L{1040+CW/2},{320} L{1035},{320} "
                f"L{1035},{545} L{272},{545} L{272},{310} L{282},{310}", "wire", "ahB"))

# Console (x=1080 vertical strip) reads everything — dim wires, no crossings (right margin corridor)
svg.append(card(1040, 505, 186, CH, "cOps", "🖥️", "llmops-admin console", "obs · evals · opts · cost", "deploy/console/README.md"))
# Left-anchored at the card's own left edge, not centred on it: centred, this caption
# began at x=1037 and the resume wire's descent down the x=1035 corridor ran along its
# left end. The corridor is deliberately just west of the console column (see the
# comment above), so any label centred on that column reaches back into it.
svg.append(f'  <text class="sub" x="1042" y="{505-10}" fill="#ff6b81">reads traces · reports · runs · spend</text>')

# ---- FinOps audit plane (its own row below the pipeline: it runs beside the
# state machine, not inside it — daily, spanning many finished runs) ----
# y=556, not y=550: the resume wire runs west along y=545, and an 11px band label
# baselined at 550 has its ascenders at ~541 -- so the wire struck through the text.
# Nothing caught it, because no check read text against wires until
# `wire_through_text` in tests/test_svg_geometry.py. Four labels on main were being
# crossed this way, this one included.
#
# 556 and not 554, which is what the check first accepted: at 554 the estimated
# ascent line lands at 545.2 and clears the wire by 0.2px, i.e. it PASSES a check
# built on an estimate while rendering as a strikethrough. TEXT_WIRE_CLEARANCE_PX in
# the check now demands real separation, because a threshold of zero on an estimated
# box is a threshold on rounding.
#
# Which then showed that 556 does not fit either: the lane between the last pipeline
# card and the audit row is only 15px, and an 11px heading cannot sit in it with real
# clearance on both sides. So the whole audit row moves DOWN 16px (AY/AY2 below)
# rather than the heading being squeezed -- the canvas has the room, and shaving the
# clearance to make a check pass is how the strikethrough shipped in the first place.
AY, AY2 = 576, 598
svg.append(f'  <text class="bandT" x="30" y="{AY-8}">AUDIT PLANE — reconciles what was actually spent · cannot stop a run · read-only billing IAM</text>')
svg.append(card(30, AY, CW, CH, "cTrig", "⏰", "finops-daily", "cron 09:00 UTC · billing reads only", "docs/COST.md"))
svg.append(card(286, AY, CW, CH, "cSpine", "🧮", "finops-reconcile λ", "period select · D-2 + re-settle", "orchestration/finops_reconcile/handler.py"))
svg.append(card(560, AY2, CW, CH, "cAgent", "🧾", "llmops_finops", "auditor · reconcile / rates / report", "agents/finops/harness.json"))
svg.append(card(850, AY2, CW, CH, "cAws", "💰", "Cost Explorer · Prices", "resource-level actuals · unit rates", "docs/COST.md"))
svg.append(wire(f"M{30+CW},{AY+CH/2} L{282},{AY+CH/2}", "wireP", "ahP"))
svg.append(wire(f"M{466},{AY+CH/2} L{511},{AY+CH/2} L{511},{AY2+CH/2} L{556},{AY2+CH/2}", "wireO", "ahO"))
svg.append(f'  <text class="sub" x="511" y="{AY+CH/2-8}" text-anchor="middle">via harness-driver λ</text>')
svg.append(wire(f"M{740},{AY2+CH/2} L{846},{AY2+CH/2}", "wireG", "ahG"))

# Self-iteration loop (eval -> finetune remediation).
#
# This used to detour left to a corridor at x=540 and share 16px of the y=316
# approach with the driver->eval wire — two wires drawn on top of each other,
# which reads as one wire and loses a connection from the picture. The gap
# between the finetune and eval cards (y 246..288) is empty, so the honest route
# is straight up it: eval's TOP edge to finetune's BOTTOM edge, two edges no
# other wire uses.
svg.append(wire(f"M{560+CW/2},{288-4} L{560+CW/2},{190+CH+4}", "wireR", "ahR"))
# Left-anchored at x=660 rather than centred at x=806: centred, this label ran from
# x 738 to 873 and its last 23px lay ON TOP of the SageMaker Training card at x=850
# -- a free-standing label overlapping an unrelated card, which the geometry checks
# could not see because they only test wires against cards. `label_over_card` in
# tests/test_svg_geometry.py tests this now. x 655..845 is the one clear lane here:
# right of the remediation wire at x=650, left of the card at x=850, in the empty
# gap between the finetune and eval cards.
svg.append(f'  <text class="sub" x="660" y="{270}" fill="#ff6b81">gate fail → remediate (≤3)</text>')

# escalation triage: EscalatedToHuman events -> conductor first (top-margin corridor, no crossings)
svg.append(wire(f"M{560+CW/2},{92-4+0} M0,0", "wireDim", "ahR"))  # placeholder no-op keeps numbering
svg.pop()
svg.append(wire(f"M{560+CW/2},{92} L{560+CW/2},{78} L{286+CW/2+30},{78} L{286+CW/2+30},{92-4}", "wireR", "ahR"))
# On the subtitle's own line, east of where it ends, rather than in the 32px gap
# between the subtitle and the first card row. That gap has to hold this label AND
# the triage wire's horizontal run at y=78, and it cannot: centred at (460,72) the
# wire ran 5px under the text; raised to y=68 the label then overlapped the subtitle
# above it. Text-over-text is a fourth defect class the geometry checks were blind to
# -- cards-vs-cards, wires-vs-cards, wires-vs-text and labels-vs-cards were all
# checked by then, and two labels on top of each other was the combination left over.
# `overlapping_labels` in tests/test_svg_geometry.py covers it now.
svg.append(f'  <text class="sub" x="666" y="60" fill="#ff6b81">escalations → conductor triage first · page human only if needed</text>')

# ---- GOVERNANCE / OPERATIONS row (y 690..746): the live subsystems the drawing
# omitted. Every wire here was traced in the source before it was drawn, and three
# wires the plan for this row named DO NOT EXIST:
#
#   * monitor-sweep -> SNS. monitor_sweep/handler.py builds an SNS client (line 63)
#     and never publishes. Its only outbound calls are invoke(DRIVER_FN) and a
#     record_outcome write to EVENTS_TABLE. All three publishes live in the DRIVER
#     (handle_escalate, page_human, flag_variance), so the topic hangs off the
#     driver, not the sweep.
#   * monitor-sweep -> Macie. The macie2 reads are in the DATA-PREP harness prompt
#     (read-only: list/describe-classification-job, list-findings). The sweep never
#     touches Macie; the scan covers customer-data/ uploads, which is a data-prep
#     concern, so Macie is drawn against the upload prefix it actually scans.
#   * the four triggers -> conductor. Three of the four go straight to
#     start-pipeline (verified: 08_triggers.py:263 nightly, run-pipeline.yml:50
#     `aws lambda invoke --function-name llmops-start-pipeline`, and the console's
#     START_FN default). Only the Tasks tab reaches the orchestrator, and it does so
#     by invoke_harness on the data plane, not through the driver.
#
# Drawing a wire that does not exist is the same defect class as a label that has
# decayed -- it is just harder to catch, because no test reads a picture. So the row
# is deliberately sparse: four cards, four wires, each one traced to a line number.
# The pipeline's interior is sealed on every side, and the geometry suite proved it
# rather than my guessing: the audit-plane row (cards at y 576..654) plus the
# resume wire's westward run at y=545 (x 272..1035) plus the finops schedule wire
# at y=604 (x 210..282) leave no lane by which a card on a bottom row can reach the
# driver. Every route attempted produced a real CROSS or THROUGH-CARD.
#
# So this row draws only the edges that are BOTH true and local, and states the
# cross-plane ones as text. That is not a cop-out; it is the pattern this file
# already uses one row up, where reconcile λ -> llmops_finops carries the sub-label
# "via harness-driver λ" instead of a wire snaking through the spine. A wire I cannot
# draw cleanly becomes a sentence; a wire that is not true does not get drawn at all:
#
#   * monitor-sweep -> SNS does not exist. monitor_sweep/handler.py builds an SNS
#     client (line 63) and never publishes. Its only outbound calls are
#     invoke(DRIVER_FN) (145-146) and a record_outcome write to EVENTS_TABLE (115).
#     All three publishes are the DRIVER's: handle_escalate, page_human, flag_variance.
#   * monitor-sweep -> Macie does not exist either. The macie2 reads (list/describe
#     -classification-job, list-findings, read-only) are in the DATA-PREP harness
#     prompt, over the customer-data/ uploads it audits.
yg = 690
svg.append(f'  <text class="bandT" x="30" y="{yg-12}">GOVERNANCE / OPERATIONS — runs on its own schedule, outside any run\'s lifetime · the sweep looks for spend nobody has claimed</text>')
svg.append(card(30, yg, CW, CH, "cTrig", "⏰", "monitor-sweep-daily", "cron 08:00 UTC · ENABLED", "deploy/08_triggers.py"))
svg.append(card(286, yg, CW, CH, "cSpine", "🧹", "monitor-sweep λ", "account-wide idle scan", "orchestration/monitor_sweep/handler.py"))
svg.append(card(560, yg, CW, CH, "cOps", "🗂️", "stage-events row", "sweep outcome · never a runs row", "orchestration/monitor_sweep/handler.py"))

# Both wires are row-local horizontals on y=688, an entirely new corridor: schedule
# -> sweep λ -> its stage-events write. These are the sweep's only two real edges
# besides the driver invoke, which the card's own sub-label names.
svg.append(wire(f"M{30+CW},{yg+CH/2} L{282},{yg+CH/2}", "wireP", "ahP"))
svg.append(wire(f"M{286+CW},{yg+CH/2} L{556},{yg+CH/2}", "wire", "ahB"))

# The two facts that have no clean wire, said plainly rather than drawn wrongly.
# Wrapped by hand to the 1240 canvas: the geometry suite checks wires and cards, not
# text width, so an over-long line runs off the edge and every check still passes.
# The widths below are measured against W-30, not eyeballed.
_GOV_NOTES = [
    ('📣 SNS llmops-escalations (1 confirmed subscriber) is published by the ', "#ff6b81", 'harness-driver λ',
     ' — escalate ·'),
    ('page_human · flag_variance — not by the sweep, which only invokes it. 🔍 Macie job ', None, '',
     'llmops-customer-data-pii'),
    ('(SCHEDULED daily, scoped customer-data/) is read read-only by the ', "#ffb454", 'data-prep harness',
     ', whose audit'),
    ('must state "no Macie job covers this data" whenever none does.', None, '', ''),
]
for i, (pre, colour, mid, post) in enumerate(_GOV_NOTES):
    span = f'<tspan fill="{colour}">{mid}</tspan>' if colour else mid
    svg.append(f'  <text class="sub" x="560" y="{yg + 78 + i*18}">{pre}{span}{post}</text>')

# State band at bottom. Table list matches the five that actually exist:
# llmops-pipeline-runs, -stage-events, -tasks, -cost-actuals, -cost-estimates.
#
# The band title is TWO lines, one per store, because as one line it was ~1216px
# wide on a 1240 canvas starting at x=50 -- i.e. it ran off the right edge, and had
# done so on main. Every geometry check passed the whole time: none of them read
# text. `overflowing_text` in tests/test_svg_geometry.py now does, so a line that
# does not fit fails the suite instead of being noticed by eye or not at all.
yb = 860
svg.append(f'  <rect class="band" x="30" y="{yb}" width="{W-60}" height="104" rx="14"/>')
svg.append(f'  <text class="bandT" x="50" y="{yb+24}">STATE — S3: runs/&lt;run_id&gt;/manifest.json · finops/rates rate card · customer-data/ uploads</text>')
svg.append(f'  <text class="bandT" x="50" y="{yb+44}">DynamoDB: runs + stage-events + tasks + cost-actuals + cost-estimates · AgentCore: shared Memory</text>')
# The kind is interpolated from the configs (see skill_mounts) rather than typed,
# because typing it is precisely what decayed. The trailing clause says what the
# kind MEANS operationally, since "s3" alone does not tell a reader that no harness
# reaches GitHub at session start any more.
_MOUNT_MEANS = {
    "s3": "a pinned snapshot in the platform bucket (ensure_skills), so no harness reads GitHub at session start",
    "git": "read from GitHub at session start; the S3 mirror exists (ensure_skills) but nothing is switched to it yet",
}
svg.append(f'  <text class="sub" x="50" y="{yb+68}">skills mounted from MLOps-agent-skills — all {SKILL_N} mounts across all {HARNESS_N} harnesses are {SKILL_KIND}: '
           f'{_MOUNT_MEANS.get(SKILL_KIND, "a MIXED fleet — some pinned, some floating on a branch; that is a partial migration, not a steady state")}</text>')
svg.append(f'  <text class="sub" x="50" y="{yb+86}">network: all 7 harnesses run PUBLIC (no networkConfiguration); the VPC is built but no VPC harness variant ships · least-privilege IAM, no *FullAccess · TEST-PROVEN gates per phase</text>')

svg.append('</svg>')
open(os.path.join(OUT, "architecture-high-level.svg"), "w").write("\n".join(svg))

# ================= LOW-LEVEL: inside a worker harness (1240 x 560) =================
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1240 560" font-family="system-ui,-apple-system,\'Segoe UI\',Roboto,sans-serif">']
svg.append(STYLE)
svg.append('  <rect class="bg" x="0" y="0" width="1240" height="560" rx="16"/>')
svg.append('  <text class="title" x="30" y="40" font-size="18">Inside a worker harness — drawn from the live configs, not aspiration</text>')
svg.append('  <text class="sub" x="30" y="60">agents/*/harness.json · Fable 5 · timeoutSeconds 840 · sliding-window 150 · launch-and-release</text>')

# Driver on the left
svg.append(card(30, 240, CW, CH, "cSpine", "🔌", "harness-driver λ", "toolUse ⇄ toolResult", "orchestration/harness_driver/handler.py"))

# Harness box (big) in the middle
svg.append('  <rect class="band" x="286" y="100" width="640" height="380" rx="14"/>')
svg.append('  <text class="bandT" x="306" y="128">AGENTCORE HARNESS SESSION (microVM · shell · filesystem)</text>')

svg.append(card(316, 150, CW, CH, "cAgent", "🧠", "Fable 5 agent loop", "Strands · maxIterations 100", "agents/README.md"))
svg.append(card(316, 260, CW, CH, "cDim", "📚", "mounted skills", "MLOps-agent-skills llmops/*", "agents/data-prep/harness.json"))
svg.append(card(316, 370, CW, CH, "cDim", "🐚", "shell + code interpreter", "aws cli · python", "agents/README.md"))
svg.append(card(660, 150, CW + 40, CH, "cOps", "🧩", "inline functions", "stage_complete · job_launched · audit …", "agents/README.md"))
svg.append(card(660, 260, CW + 40, CH, "cDim", "🗃️", "shared BYO memory", "SEMANTIC + EPISODIC · cross-run", "deploy/04_wire_memory.py"))
svg.append(card(660, 370, CW + 40, CH, "cDim", "🛰️", "OTel traces", "always_on → console evals", "deploy/06_observability.py"))

# driver <-> harness (in and out, separate y)
svg.append(wire(f"M{30+CW},{240+CH/2-10} L{286-4},{240+CH/2-10}", "wireO", "ahO"))
svg.append(wire(f"M{286},{240+CH/2+12} L{30+CW+4},{240+CH/2+12}", "wireR", "ahR"))
svg.append(f'  <text class="sub" x="245" y="{240+CH/2-18}" text-anchor="middle">invoke</text>')
# Centred at x=230, not 250: at 250 the estimated right edge reached x=318 and grazed
# the mounted-skills card that starts at x=316. Two px of overlap is not a legibility
# problem by itself, but the reason to move it is that the check cannot tell 2px of
# real overlap from 2px of width-estimate error -- and a label that has to be argued
# about is a label in the wrong place.
svg.append(f'  <text class="sub" x="230" y="{240+CH+26}" text-anchor="middle">pause: stopReason=tool_use</text>')

# inside wiring: agent loop -> skills -> shell (vertical), agent -> inline fns (horizontal)
svg.append(wire(f"M{316+CW/2},{150+CH} L{316+CW/2},{260-4}", "wireDim", "ahB").replace("wireDim", "wire"))
svg.append(wire(f"M{316+CW/2},{260+CH} L{316+CW/2},{370-4}", "wireDim", "ahB").replace("wireDim", "wire"))
svg.append(wire(f"M{316+CW},{150+CH/2} L{660-4},{150+CH/2}", "wireO", "ahO"))

# AWS on the right
svg.append(card(986, 150, 220, CH, "cAws", "☁️", "SageMaker / Bedrock", "jobs · endpoints · converse", "docs/ARCHITECTURE.md"))
svg.append(card(986, 260, 220, CH, "cAws", "🪣", "S3 manifest", "single source of truth", "pipeline/contracts/manifest.schema.json"))
# shell -> SageMaker/Bedrock: low corridor y=452 then up the RIGHT margin (x=1222,
# outside all cards) into the card's right edge — avoids crossing the memory->S3 wire.
svg.append(wire(f"M{316+CW/2},{370+CH} L{316+CW/2},{452} L{1222},{452} L{1222},{150+CH/2} L{986+220+4},{150+CH/2}", "wireG", "ahG"))
svg.append(wire(f"M{660+CW+40},{260+CH/2} L{982},{260+CH/2}", "wireG", "ahG"))

svg.append('</svg>')
open(os.path.join(OUT, "architecture-low-level.svg"), "w").write("\n".join(svg))

# ================= CONSOLE: the LLMOps Admin dashboard's own plumbing (1240 x 760) =================
#
# Layout law here is a band-per-plane, because the dashboard's whole design claim
# is that the three planes have different rules: GETs are public, every POST is
# authed at ONE chokepoint, and the customer-facing consult plane is the only one
# that talks to an agent. Bands keep each plane's wires inside its own horizontal
# strip, so no wire needs to cross another plane to reach its target.
#
# Verified live against the deployment (2026-08-01), unauthenticated:
#   GET  /api/overview, /api/tasks, /api/cost-overview           -> 200
#   POST /api/tasks, /api/start-run, /api/cost-approval,
#        /api/data-upload-url, /api/finops-run                   -> 401
# so "public GETs, authed POSTs" is measured, not asserted.
#
# Counts are read off the router, not remembered: 30 handlers = 13 GET + 3
# session POST + 14 authed POST, and /api/tasks/{id}/ fans into 3 sub-actions
# (message, accept, close). An earlier version of this diagram said "26 routes"
# and "Cognito on EVERY POST"; both were wrong in the same direction -- flattering.
# /api/login, /api/refresh and /api/refresh/revoke are handled BEFORE the
# chokepoint on purpose, and that is not a gap: requiring a live session to log
# in, or to recover one after a page reload, is a contradiction. They are the only
# three, they mint or revoke sessions rather than acting on the platform, and
# tests/test_console_routes.py derives all four numbers from the router so the
# next added POST cannot slip in above the chokepoint unnoticed.
CH2 = 760
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1240 {CH2}" font-family="system-ui,-apple-system,\'Segoe UI\',Roboto,sans-serif">']
svg.append(STYLE)
svg.append(f'  <rect class="bg" x="0" y="0" width="1240" height="{CH2}" rx="16"/>')
svg.append('  <text class="title" x="30" y="40" font-size="18">LLMOps Admin console — one Lambda, read-mostly, server-enforced gates</text>')
svg.append(f'  <text class="sub" x="30" y="60">deploy/console/ · {TAB_N} tabs · self-contained HTML from cold start · public GETs · Cognito on every POST except the 3 session routes that establish it</text>')

# ---- the operator path in (left column, top to bottom) ----
svg.append(card(30, 96, CW, CH, "cOps", "🧑‍💻", "Operator browser", "one HTML file · no CDN · CSP self", "deploy/console/frontend.html"))
svg.append(card(30, 196, CW, CH, "cTrig", "🚪", "HTTP API Gateway", "routes / and /api/*", "deploy/console/deploy.sh"))
svg.append(card(30, 296, CW, CH, "cTrig", "🔐", "Cognito user pool", "access token + httpOnly refresh", "deploy/console/README.md"))
svg.append(wire(f"M{30+CW/2},{96+CH} L{30+CW/2},{196-4}", "wireP", "ahP"))
svg.append(wire(f"M{30+CW/2},{196+CH} L{30+CW/2},{296-4}", "wireP", "ahP"))
# Below the Cognito card, not between the cards: at y=286 this caption was crossed
# first by the very wire it annotates (centred on x=120) and then, once moved east,
# by the consult drop at x=254. The band y 278..289 is pierced by five verticals
# (120, 254, and the three write-plane exits at 316/356/396), so no x in it is
# clear. Under the card at y=370 nothing crosses at all.
svg.append(f'  <text class="sub" x="30" y="{296+CH+18}">POSTs carry the access token</text>')

# ---- the one handler ----
LAMX, LAMY, LAMW = 286, 196, CW + 20
svg.append(card(LAMX, LAMY, LAMW, CH, "cSpine", "🖥️", "llmops-admin λ", "frontend + 32 route handlers in one λ", "deploy/console/lambda_function.py"))
svg.append(wire(f"M{30+CW},{196+CH/2} L{LAMX-4},{196+CH/2}", "wireP", "ahP"))
lam_r, lam_cx = LAMX + LAMW, LAMX + LAMW / 2

# ---- READ PLANE (top right): four sources, nested corridors, no crossings ----
svg.append('  <text class="bandT" x="640" y="90">READ PLANE — public GETs, aggregated server-side</text>')
reads = [("🛰️", "AgentCore planes", "fleet · evals · optimizations", "deploy/06_observability.py", 640, 108),
         ("📈", "CloudWatch + spans", "metrics · aws/spans sessions", "deploy/06_observability.py", 640, 180),
         ("🗄️", "DynamoDB", "runs · stage-events · tasks · cost", "docs/COST.md", 640, 252),
         ("🪣", "S3", "reports · rate card · customer-data", "docs/COST.md", 640, 324)]
for icon, name, sub, href, x, y in reads:
    svg.append(card(x, y, CW + 40, CH, "cAws", icon, name, sub, href))
# Corridor nesting, which is a rule and not a fiddle: exits are ordered top-down
# along the Lambda's right edge, and each wire's corridor is assigned so that the
# wire travelling FURTHEST from its exit gets the NEAREST corridor. Upward and
# downward wires are nested separately.
#
# Assigning corridors in plain card order instead (left-to-right for top-to-
# bottom) crosses every downward pair: a wire that exits higher but turns down
# later runs its horizontal leg straight across the vertical leg of the wire
# below it. Verified — that naive version produced CROSS at (552,242) and
# (578,280) before this nesting replaced it.
CORRIDORS = [500, 526, 552, 578]
exits = [LAMY + 10 + i * 12 for i in range(len(reads))]
ups = [i for i, r in enumerate(reads) if r[5] + CH / 2 < exits[i]]
downs = [i for i, r in enumerate(reads) if r[5] + CH / 2 >= exits[i]]
# furthest-travelling wire first in each group -> nearest free corridor
order = (sorted(ups, key=lambda i: reads[i][5]) +
         sorted(downs, key=lambda i: -reads[i][5]))
corridor_of = {}
for slot, i in enumerate(order):
    corridor_of[i] = CORRIDORS[slot]
for i, (_, _, _, _, x, y) in enumerate(reads):
    corr, src_y = corridor_of[i], exits[i]
    svg.append(wire(f"M{lam_r},{src_y} L{corr},{src_y} L{corr},{y+CH/2} L{x-4},{y+CH/2}", "wireG", "ahG"))

# ---- WRITE PLANE (middle): every POST through one auth chokepoint ----
# Placed at x=430, not x=286, and shortened: this band is full of vertical corridors
# (the three lambda exits at x 316/356/396, the three card entries at 414/694/974,
# and the consult drop at 254), so the long heading at x=286 was struck through by
# two of them. The only clear gap in the row is x 430..690, which is 260px -- hence
# the shorter wording. The dropped clause is not dropped: "server-side" moved onto
# the gate label below, which is where a reader looks for it anyway.
svg.append('  <text class="bandT" x="430" y="424">WRITE PLANE — one auth chokepoint</text>')
WW = CW + 76
wy = 444
svg.append(card(286, wy, WW, CH, "cOps", "▶️", "POST /api/start-run", "dispatch a pipeline run", "orchestration/start_pipeline/handler.py"))
svg.append(card(286 + WW + 24, wy, WW, CH, "cOps", "⚖️", "POST /api/cost-approval", "approver group · never self-approve", "docs/COST.md"))
svg.append(card(286 + 2 * (WW + 24), wy, WW, CH, "cOps", "💸", "POST /api/finops-run", "reconcile · pricing_refresh · report", "orchestration/finops_reconcile/handler.py"))
# Each write card is entered on its TOP edge from its own vertical corridor off a
# shared horizontal lane at y=404 -- above the write cards, below the read cards.
# Exits are staggered along the Lambda's bottom edge, left-to-right in the same
# order as the targets, so the three wires never need to swap sides.
for i in range(3):
    tx = 286 + i * (WW + 24) + WW / 2
    ex = LAMX + 30 + i * 40
    svg.append(wire(f"M{ex},{LAMY+CH} L{ex},{404-i*10} L{tx},{404-i*10} L{tx},{wy-4}", "wireR", "ahR"))
svg.append(f'  <text class="sub" x="{286 + 2*(WW+24) + WW/2}" y="{wy+CH+20}" text-anchor="middle">async → finops-reconcile λ → harness-driver λ → llmops_finops</text>')
# Left-anchored on the card's left edge rather than centred under it: centred, this
# label began at x~253 and the consult drop down x=254 struck through it.
svg.append(f'  <text class="sub" x="286" y="{wy+CH+20}">${LIMIT_USD:,.0f} reference, server-side not in the UI: advisory now, blocking by env</text>')

# ---- CONSULT PLANE (bottom): the Tasks tab -- the only plane that talks to an agent ----
#
# This plane was missing from the diagram entirely, which mattered: it is the
# customer-facing half of the product (a consultation thread that produces a
# priced, KMS-signed plan) and the only console path that invokes a harness.
svg.append('  <text class="bandT" x="286" y="570">CONSULT PLANE — Tasks tab: one thread per engagement · the only console path that invokes an agent</text>')
# Four cards across a 1240 viewBox: three at WW=256 plus a fourth would overflow
# the canvas (286 + 3*280 + 170 = 1296) and the verdicts panel at x=1040 landed
# ON TOP of the third card, which ends at x=1102. Overlapping cards is the one
# failure the geometry check reports as THROUGH-CARD on a wire that is in fact
# drawn correctly, so the fix is the layout, not the wire. CWW is sized so all
# four fit with real gutters: 286 + 3*(212+18) + 212 = 1188 < 1210.
CWW, GUT = 212, 18
cy = 590
cx = [286 + i * (CWW + GUT) for i in range(4)]
svg.append(card(cx[0], cy, CWW, CH, "cAgent", "💬", "POST /api/tasks/*", "create · message · accept · close", "deploy/console/lambda_function.py"))
svg.append(card(cx[1], cy, CWW, CH, "cAgent", "🎼", "llmops_orchestrator", "consult · priced plan · launch_run", "agents/orchestrator/harness.json"))
svg.append(card(cx[2], cy, CWW, CH, "cAws", "🔑", "KMS-signed acceptance", "hash-chained · approver identity", "orchestration/harness_driver/conductor_tools.py"))
# The consult wire drops down the clear column at x=254 -- left of every card in
# the write and consult bands (both start at x=286), right of the Cognito card
# that ends at x=210 -- and enters the first consult card on its LEFT edge.
# Descending inside the band at LAMX+10 (my first attempt) put the vertical leg
# straight through both the /api/start-run card and the /api/tasks card, since
# those cards are 256px wide and start at exactly x=286.
svg.append(wire(f"M{LAMX-4},{LAMY+CH-14} L{254},{LAMY+CH-14} L{254},{cy+CH/2} L{cx[0]-4},{cy+CH/2}", "wireR", "ahR"))
for i in range(3):
    svg.append(wire(f"M{cx[i]+CWW},{cy+CH/2} L{cx[i+1]-4},{cy+CH/2}", "wireO", "ahO"))
svg.append(f'  <text class="sub" x="{cx[1]+CWW/2}" y="{cy+CH+20}" text-anchor="middle">via harness-driver λ · streamed reply · presigned customer-data upload</text>')

# ---- what the run view shows about verdicts (the two-bounded-queries fix, drawn) ----
svg.append(card(cx[3], cy, CWW, CH, "cOps", "⚖️", "verdicts panel", "delivered · parked · never delivered", "deploy/console/frontend.html"))

svg.append(f'  <text class="sub" x="30" y="{CH2-22}">Two bounded queries feed the run timeline: stage events are read sk &lt; "A", parked verdicts read sk begins_with "directive#" — a prefix is not a filter, so they are separate reads, not one filtered list.</text>')

svg.append('</svg>')
open(os.path.join(OUT, "architecture-console.svg"), "w").write("\n".join(svg))
print("SVGs written")
