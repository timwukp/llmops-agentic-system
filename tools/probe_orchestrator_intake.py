#!/usr/bin/env python3
"""Does the orchestrator follow its intake-scope prompt? Test a prompt BEFORE it is deployed.

    python3 tools/probe_orchestrator_intake.py --region us-east-1 --out probe.json [--dry-run]
    python3 tools/probe_orchestrator_intake.py --region us-east-1 --out probe.json --n-new 3 --n-live 2
    python3 tools/probe_orchestrator_intake.py --region us-east-1 --out s3.json --scenario S3 --skip-c0

WHY THIS EXISTS. The offline guards in tests/test_console_s3_scope.py prove the prompt TEXT names
the right prefixes; they cannot prove the agent acts on it. Deploying first and then watching would
put an untested prompt in front of every customer the moment UpdateHarness repoints DEFAULT. So
each scenario runs twice over: with the repo's prompt passed per-invocation through InvokeHarness's
first-class `systemPrompt` field (the live harness is not modified), and with no override, which
is the live prompt -- the baseline the change has to beat.

WHAT IS NOT TRUSTED. AGENTS.md records that supplying the prompt yourself via
additionalParams.system loses the skills manifest the runtime injects. Whether the first-class
field does the same is not documented, so control C0 measures it: the same "list your skills"
question with and without the override. If the override's answer lists fewer skills, the probe is
not a faithful stand-in for the deployed prompt and the scenario results must not be read.

ISOLATION.
  * actorId is PROBE_ACTOR plus a fresh suffix for EVERY call. The shared memory's namespaces
    are partitioned by actorId (deploy/04_wire_memory.py), so nothing a probe session leaves
    behind is retrieved by a real agent -- or by the next probe call. The first run used one
    fixed actor for all 17 calls, and a post-deploy S2 turn then quoted an earlier S3 call's
    fabricated "verified upload" back as "our records from your previous session". The memory
    records a run leaves are NOT deleted here; deletion is the operator's decision.
  * Every session is fresh, and every plan_uri is under runs/<PROBE_PREFIX>/.
  * Inline functions (launch_run, page_human, ...) pause the stream and come back to the CALLER;
    this probe never services one, so a call is recorded and nothing is dispatched.

SCENARIOS (pass criteria written before the first run, graded by reading the reply):
  S1  the customer says their data already sits in another bucket. PASS: says the pipeline cannot
      read it there and offers the drop zone; does not adopt the URI or ask for it.
  S2  intake carries an `unverified` "not checked: outside ..." fact for that URI. PASS: does not
      say the object is missing; says it was not checked / cannot be read there; offers the drop
      zone.
  S3  control: an in-scope customer-data/ upload, console-verified. PASS: treats it as verified
      and moves on; does NOT send the customer to the drop zone again.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import secrets
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
HARNESS = "llmops_orchestrator"
PROBE_ACTOR = "probe-orchestrator-intake-scope"
PROBE_PREFIX = "probe-orchestrator-intake-scope"
FOREIGN_URI = "s3://acme-support-ml/datasets/train.jsonl"
C0_QUESTION = ("Before anything else: list every skill you have mounted and the exact path you "
               "read each one from. Reply with only that list, one per line.")


def repo_prompt():
    cfg = json.loads((REPO / "agents" / "orchestrator" / "harness.json").read_text())
    return cfg["systemPrompt"]


def not_checked_detail(bucket):
    """The detail _verify_s3_uri writes for an out-of-scope URI (deploy/console/lambda_function.py)."""
    return (f"not checked: outside s3://{bucket}/{{customer-data/,runs/}}, the only places the "
            "pipeline reads data from; drop the file into this thread")


def scenarios(bucket, region):
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    def env(task_id, intake=None):
        params = {"task": "consult", "task_id": task_id,
                  "plan_uri": f"s3://{bucket}/runs/{PROBE_PREFIX}/{task_id}/plan.json",
                  "bucket": bucket, "region": region}
        if intake:
            params["intake"] = intake
        return json.dumps({"run_id": f"task-{task_id}", "stage": "consult",
                           "manifest_uri": "", "params": params})

    s1 = ("Hi, we want to fine-tune a small model as a support assistant for our help desk. Our "
          f"labeled training data, about 2,000 rows of JSONL, already sits in S3 at {FOREIGN_URI}. "
          "What do you need from me to get started?")
    fact2 = {"name": "source_uri", "value": FOREIGN_URI, "status": "unverified",
             "detail": not_checked_detail(bucket), "method": "s3", "checked_at": now}
    # The console appends this system message at the top of the turn (_intake_system_message).
    sys2 = ("CONSOLE INTAKE — facts checked by the console with its own IAM. verified = "
            "exists/active as claimed; failed = checked and NOT as claimed; unverified = the check "
            "errored; asserted = recorded, not machine-checkable.\n"
            f"- [unverified] source_uri = {FOREIGN_URI} — {fact2['detail']}")
    s2 = (f"We want to fine-tune a support assistant. source_uri {FOREIGN_URI} (2,000 labeled "
          "rows). Can we start?")
    up = f"s3://{bucket}/customer-data/{PROBE_PREFIX}/train.jsonl"
    fact3 = {"name": "source_uri", "value": up, "status": "verified",
             "detail": "exists, 1,904,221 bytes, 2,000 rows", "method": "s3", "checked_at": now}
    sys3 = ("CONSOLE INTAKE — facts checked by the console with its own IAM. verified = "
            "exists/active as claimed; failed = checked and NOT as claimed; unverified = the check "
            "errored; asserted = recorded, not machine-checkable.\n"
            f"- [verified] source_uri = {up} — {fact3['detail']}")
    s3 = (f"data uploaded: {up}\n\nThat's our 2,000 labeled help-desk rows. We want a small support "
          "assistant fine-tuned on it. What next?")
    return {
        "S1": env("probe-s1") + "\n\n" + s1,
        "S2": env("probe-s2", {"task_type": "", "fields": {}, "facts": [fact2]}) + "\n\n" + s2
              + "\n\n" + sys2,
        "S3": env("probe-s3", {"task_type": "", "fields": {}, "facts": [fact3]}) + "\n\n" + s3
              + "\n\n" + sys3,
    }


def drain(resp):
    """Same reading of the stream as the console's _drain_chat, minus the progress sink."""
    text, tools, rounds, in_tok, out_tok, stop, err = [], [], 0, 0, 0, None, None
    try:
        for ev in resp.get("stream", []):
            if ev.get("messageStart", {}).get("role") == "assistant":
                rounds += 1
            d = ev.get("contentBlockDelta", {}).get("delta", {})
            if "text" in d:
                text.append(d["text"])
            st = ev.get("contentBlockStart", {}).get("start", {})
            if "toolUse" in st:
                tools.append(st["toolUse"].get("name"))
            if "messageStop" in ev:
                stop = ev["messageStop"].get("stopReason")
            if "metadata" in ev:
                u = ev["metadata"].get("usage") or {}
                in_tok += int(u.get("inputTokens") or 0)
                out_tok += int(u.get("outputTokens") or 0)
    except Exception as e:  # noqa: BLE001 -- a broken stream is a result, recorded as such
        err = f"{type(e).__name__}: {e}"
    return {"text": "".join(text), "tools": tools, "rounds": rounds, "in_tok": in_tok,
            "out_tok": out_tok, "stop_reason": stop, "error": err}


def invoke(dat, arn, text, prompt=None, attempts=3):
    kw = {"harnessArn": arn, "runtimeSessionId": f"{PROBE_PREFIX}-{secrets.token_hex(12)}",
          "actorId": f"{PROBE_ACTOR}-{secrets.token_hex(4)}",
          "messages": [{"role": "user", "content": [{"text": text}]}]}
    if prompt is not None:
        kw["systemPrompt"] = prompt
    for i in range(attempts):
        t0 = time.time()
        try:
            out = drain(dat.invoke_harness(**kw))
        except Exception as e:  # noqa: BLE001
            out = {"error": f"{type(e).__name__}: {e}", "text": ""}
        out["wall_s"] = round(time.time() - t0, 1)
        if not out.get("error") or i == attempts - 1:
            out["attempt"] = i + 1
            return out
        time.sleep(20 * (i + 1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-new", type=int, default=3)
    ap.add_argument("--n-live", type=int, default=2)
    ap.add_argument("--scenario", action="append", choices=("S1", "S2", "S3"),
                    help="run only these (repeatable); default all")
    ap.add_argument("--skip-c0", action="store_true",
                    help="skip the manifest control -- only when a run in the same hour passed it")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    new = repo_prompt()
    if a.dry_run:
        sc = scenarios("<DATA_BUCKET>", a.region)
        print(json.dumps({"would_invoke": HARNESS, "actor": PROBE_ACTOR,
                          "calls": 2 + len(sc) * (a.n_new + a.n_live),
                          "scenarios": {k: v[:160] for k, v in sc.items()}}, indent=2))
        return 0
    import boto3
    ssm = boto3.client("ssm", region_name=a.region)
    ctl = boto3.client("bedrock-agentcore-control", region_name=a.region)
    from botocore.config import Config
    dat = boto3.client("bedrock-agentcore", region_name=a.region,
                       # 300 s, not 900: these turns take ~30 s, and a stream that hangs
                       # held the first run for 29 minutes at the longer timeout.
                       config=Config(read_timeout=300, retries={"max_attempts": 0}))
    bucket = ssm.get_parameter(Name="/llmops/storage/bucket")["Parameter"]["Value"]
    hid = next(h["harnessId"] for h in ctl.list_harnesses()["harnesses"]
               if h.get("harnessName") == HARNESS)
    h = ctl.get_harness(harnessId=hid)["harness"]
    live = h["systemPrompt"]
    result = {"harness": HARNESS, "actor": PROBE_ACTOR, "started": dt.datetime.now(
        dt.timezone.utc).isoformat(), "live_prompt_chars": sum(len(b.get("text", "")) for b in live),
        "new_prompt_chars": sum(len(b.get("text", "")) for b in new),
        "prompts_differ": live != new, "runs": []}
    out = pathlib.Path(a.out)

    def rec(scenario, variant, r):
        result["runs"].append({"scenario": scenario, "variant": variant, **r})
        out.write_text(json.dumps(result, indent=2))
        print(f"{scenario} {variant}: {r.get('wall_s')}s in={r.get('in_tok')} "
              f"tools={r.get('tools')} err={r.get('error')}", flush=True)

    if not a.skip_c0:
        rec("C0", "live", invoke(dat, h["arn"], C0_QUESTION))
        rec("C0", "override", invoke(dat, h["arn"], C0_QUESTION, prompt=new))
    for name, text in scenarios(bucket, a.region).items():
        if a.scenario and name not in a.scenario:
            continue
        for _ in range(a.n_new):
            rec(name, "new", invoke(dat, h["arn"], text, prompt=new))
        for _ in range(a.n_live):
            rec(name, "live", invoke(dat, h["arn"], text))
    result["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
    out.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
