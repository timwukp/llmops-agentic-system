#!/usr/bin/env python3
"""Generate the two architecture SVGs in the house style (dark navy cards,
per-tier accent strokes, animated dashed wires, rounded corners, clickable cards).

Layout law (enforced by tests/check_svg_geometry.py): one corridor per wire,
distinct anchor edges per target — no two wires may intersect, no wire may pass
through a card. Regenerate + re-verify after every edit; never hand-edit the SVGs.
"""
import os

OUT = os.path.dirname(os.path.abspath(__file__))
REPO = "https://github.com/timwukp/llmops-agentic-system/blob/main/"

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


# ================= HIGH-LEVEL (1240 x 780) =================
W, H = 1240, 780
CW, CH = 180, 56
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="system-ui,-apple-system,\'Segoe UI\',Roboto,sans-serif">']
svg.append(STYLE)
svg.append(f'  <rect class="bg" x="0" y="0" width="{W}" height="{H}" rx="16"/>')
svg.append('  <text class="title" x="30" y="40" font-size="18">llmops-agentic-system — autonomous LLMOps on AgentCore</text>')
svg.append('  <text class="sub" x="30" y="60">teacher DeepSeek-R1 (Bedrock) → student Qwen3-1.7B (SageMaker QLoRA) · 7 harnesses · click any card</text>')

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
svg.append(card(850, 240, CW, CH, "cAws", "🎓", "SageMaker Training", "Qwen3-1.7B QLoRA job", "pipeline/training/train_qlora.py"))
svg.append(card(850, 388, CW, CH, "cAws", "🌐", "SageMaker Endpoint", "student inference · g5.xlarge", "docs/ARCHITECTURE.md"))

# harnesses -> AWS (data-prep->bedrock, finetune->training, deploy->endpoint; own corridors)
svg.append(wire(f"M{560+CW},{92+CH/2} L{846},{92+CH/2}", "wireG", "ahG"))
svg.append(wire(f"M{560+CW},{190+CH/2} L{800},{190+CH/2} L{800},{240+CH/2} L{846},{240+CH/2}", "wireG", "ahG"))
svg.append(wire(f"M{560+CW},{386+CH/2} L{800},{386+CH/2} L{800},{388+CH/2+14} L{846},{388+CH/2+14}", "wireG", "ahG"))

# training-complete resume: SageMaker Training -> (EventBridge rule) -> SFN. Own low corridor.
svg.append(card(850, 505, CW, CH, "cSpine", "📡", "EventBridge rule", "job state change → resume λ", "orchestration/resume_pipeline/handler.py"))
svg.append(wire(f"M{850+CW/2},{240+CH} L{850+CW/2},{505-4}", "wire", "ahB"))
svg.append(wire(f"M{850},{505+CH/2} L{286+CW/2},{505+CH/2} L{286+CW/2},{282+CH+4}", "wire", "ahB").replace('marker-end="url(#ahB)"', 'marker-end="url(#ahB)"'))

# Console (x=1080 vertical strip) reads everything — dim wires, no crossings (right margin corridor)
svg.append(card(1040, 505, 186, CH, "cOps", "🖥️", "llmops-admin console", "obs · evals · opts · cost", "deploy/console/README.md"))
svg.append(f'  <text class="sub" x="{1040+93}" y="{505-10}" text-anchor="middle" fill="#ff6b81">reads traces · reports · runs · spend</text>')

# ---- FinOps audit plane (its own row below the pipeline: it runs beside the
# state machine, not inside it — daily, spanning many finished runs) ----
svg.append(f'  <text class="bandT" x="30" y="550">AUDIT PLANE — reconciles what was actually spent · cannot stop a run · read-only billing IAM</text>')
svg.append(card(30, 560, CW, CH, "cTrig", "⏰", "finops-daily", "cron 09:00 UTC · billing reads only", "docs/COST.md"))
svg.append(card(286, 560, CW, CH, "cSpine", "🧮", "finops-reconcile λ", "period select · D-2 + re-settle", "orchestration/finops_reconcile/handler.py"))
svg.append(card(560, 582, CW, CH, "cAgent", "🧾", "llmops_finops", "auditor · reconcile / rates / report", "agents/finops/harness.json"))
svg.append(card(850, 582, CW, CH, "cAws", "💰", "Cost Explorer · Price List", "resource-level actuals · unit rates", "docs/COST.md"))
svg.append(wire(f"M{30+CW},{560+CH/2} L{282},{560+CH/2}", "wireP", "ahP"))
svg.append(wire(f"M{466},{560+CH/2} L{511},{560+CH/2} L{511},{582+CH/2} L{556},{582+CH/2}", "wireO", "ahO"))
svg.append(f'  <text class="sub" x="511" y="{560+CH/2-8}" text-anchor="middle">via harness-driver λ</text>')
svg.append(wire(f"M{740},{582+CH/2} L{846},{582+CH/2}", "wireG", "ahG"))

# Self-iteration loop label (eval -> finetune remediation) — left-side corridor between columns
svg.append(wire(f"M{560},{288+CH/2} L{540},{288+CH/2} L{540},{190+CH/2+16} L{556},{190+CH/2+16}", "wireR", "ahR"))
svg.append(f'  <text class="sub" x="{560+CW/2}" y="{288-14}" text-anchor="middle" fill="#ff6b81">gate fail → remediate (≤3)</text>')

# escalation triage: EscalatedToHuman events -> conductor first (top-margin corridor, no crossings)
svg.append(wire(f"M{560+CW/2},{92-4+0} M0,0", "wireDim", "ahR"))  # placeholder no-op keeps numbering
svg.pop()
svg.append(wire(f"M{560+CW/2},{92} L{560+CW/2},{78} L{286+CW/2+30},{78} L{286+CW/2+30},{92-4}", "wireR", "ahR"))
svg.append(f'  <text class="sub" x="{460}" y="{72}" text-anchor="middle" fill="#ff6b81">escalations → conductor triage first · page human only if needed</text>')

# State band at bottom
yb = 680
svg.append(f'  <rect class="band" x="30" y="{yb}" width="{W-60}" height="72" rx="14"/>')
svg.append(f'  <text class="bandT" x="50" y="{yb+26}">STATE — S3 runs/&lt;run_id&gt;/manifest.json · finops/rates rate card · DynamoDB runs + stage-events + cost-actuals + cost-estimates · shared Memory</text>')
svg.append(f'  <text class="sub" x="50" y="{yb+48}">skills mounted from MLOps-agent-skills (git in dev, S3 mirror in prod) · VPC-isolated in production · least-privilege IAM · TEST-PROVEN gates per phase</text>')

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
svg.append(card(660, 150, CW + 40, CH, "cOps", "🧩", "inline functions", "stage_complete · job_launched · finops audit tools …", "agents/README.md"))
svg.append(card(660, 260, CW + 40, CH, "cDim", "🗃️", "shared BYO memory", "SEMANTIC + EPISODIC · cross-run", "deploy/04_wire_memory.py"))
svg.append(card(660, 370, CW + 40, CH, "cDim", "🛰️", "OTel traces", "always_on → console evals", "deploy/06_observability.py"))

# driver <-> harness (in and out, separate y)
svg.append(wire(f"M{30+CW},{240+CH/2-10} L{286-4},{240+CH/2-10}", "wireO", "ahO"))
svg.append(wire(f"M{286},{240+CH/2+12} L{30+CW+4},{240+CH/2+12}", "wireR", "ahR"))
svg.append(f'  <text class="sub" x="245" y="{240+CH/2-18}" text-anchor="middle">invoke</text>')
svg.append(f'  <text class="sub" x="250" y="{240+CH+26}" text-anchor="middle">pause: stopReason=tool_use</text>')

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

# ================= CONSOLE: the LLMOps Admin dashboard's own plumbing (1240 x 620) =================
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1240 620" font-family="system-ui,-apple-system,\'Segoe UI\',Roboto,sans-serif">']
svg.append(STYLE)
svg.append('  <rect class="bg" x="0" y="0" width="1240" height="620" rx="16"/>')
svg.append('  <text class="title" x="30" y="40" font-size="18">LLMOps Admin console — one Lambda, read-mostly, server-enforced gates</text>')
svg.append('  <text class="sub" x="30" y="60">deploy/console/ · self-contained HTML from cold start · public GETs, Cognito on every POST</text>')

# Left column: the operator path in
svg.append(card(30, 120, CW, CH, "cOps", "🧑‍💻", "Operator browser", "single HTML · no CDN · CSP self", "deploy/console/frontend.html"))
svg.append(card(30, 240, CW, CH, "cTrig", "🚪", "HTTP API Gateway", "routes / and /api/*", "deploy/console/deploy.sh"))
svg.append(card(30, 360, CW, CH, "cTrig", "🔐", "Cognito user pool", "sign-in · approver group", "docs/COST.md"))
svg.append(wire(f"M{30+CW/2},{120+CH} L{30+CW/2},{240-4}", "wireP", "ahP"))
svg.append(wire(f"M{30+CW/2},{240+CH} L{30+CW/2},{360-4}", "wireDim", "ahP").replace("wireDim", "wireP"))
svg.append(f'  <text class="sub" x="{30+CW/2}" y="{360-10}" text-anchor="middle">POSTs carry access token</text>')

# Center: the console Lambda
svg.append(card(286, 240, CW+20, CH, "cSpine", "🖥️", "llmops-admin λ", "frontend + API in one handler", "deploy/console/lambda_function.py"))
svg.append(wire(f"M{30+CW},{240+CH/2} L{282},{240+CH/2}", "wireP", "ahP"))

# Read plane (top right): four sources, each its own corridor from the Lambda's top edge
svg.append(f'  <text class="bandT" x="620" y="100">READ PLANE — public GETs, aggregated server-side</text>')
reads = [("🛰️", "AgentCore planes", "fleet · evals · optimizations", "deploy/06_observability.py", 620, 120),
         ("📈", "CloudWatch + spans", "metrics · aws/spans sessions", "deploy/06_observability.py", 620, 200),
         ("🗄️", "DynamoDB", "runs · events · cost tables", "docs/COST.md", 620, 280),
         ("🪣", "S3", "reports · finops/rates rate card", "docs/COST.md", 620, 360)]
for icon, name, sub, href, x, y in reads:
    svg.append(card(x, y, CW+20, CH, "cAws", icon, name, sub, href))
lam_r = 286 + CW + 20
# Corridor nesting so the fan-out never self-crosses: upward wires take corridors
# nearest-first from the top, downward wires nearest-first from the bottom.
corridors = [540, 556, 588, 572]
for i, (_, _, _, _, x, y) in enumerate(reads):
    corr = corridors[i]
    src_y = 240 + 8 + i * 12
    svg.append(wire(f"M{lam_r},{src_y} L{corr},{src_y} L{corr},{y+CH/2} L{x-4},{y+CH/2}", "wireG", "ahG"))

# Write plane (bottom): the three POST actions and what stands between them and effect
svg.append(f'  <text class="bandT" x="286" y="450">WRITE PLANE — every POST authed; the cost gate is enforced server-side, not in the UI</text>')
WW = CW + 60  # write-plane cards carry longer route names
svg.append(card(286, 470, WW, CH, "cOps", "▶️", "POST /runs", "start pipeline run", "orchestration/start_pipeline/handler.py"))
svg.append(card(566, 470, WW, CH, "cOps", "⚖️", "POST /api/cost-approval", "approver group · never self-approve", "docs/COST.md"))
svg.append(card(846, 470, WW, CH, "cOps", "💸", "POST /api/finops-run", "reconcile · pricing_refresh · report", "orchestration/finops_reconcile/handler.py"))
lam_cx = 286 + (CW+20)/2
svg.append(wire(f"M{lam_cx-40},{240+CH} L{lam_cx-40},{470-4}", "wireR", "ahR"))
svg.append(wire(f"M{lam_cx+40},{240+CH} L{lam_cx+40},{440} L{566+WW/2},{440} L{566+WW/2},{470-4}", "wireR", "ahR"))
svg.append(wire(f"M{286+CW+20},{240+CH/2+16} L{500},{240+CH/2+16} L{500},{424} L{846+WW/2},{424} L{846+WW/2},{470-4}", "wireR", "ahR"))
svg.append(f'  <text class="sub" x="{846+WW/2}" y="{470+CH+22}" text-anchor="middle">async → finops-reconcile λ → harness-driver λ → llmops_finops</text>')

svg.append('</svg>')
open(os.path.join(OUT, "architecture-console.svg"), "w").write("\n".join(svg))
print("SVGs written")
