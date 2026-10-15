"""Derive every S3 read the admin console makes, and hold its IAM policy to exactly that.

`S3Manifests` granted s3:GetObject + s3:ListBucket on "*" -- every object in every bucket
of the account -- to a console that reads five things from one bucket. The grant was not
wrong by accident: intake verification checked any s3:// URI a chat message named, in any
bucket, so the breadth was load-bearing for exactly one code path, and that path reported
"verified" for objects the pipeline's own role cannot read.

So the reads are read out of `lambda_function.py`'s AST, not listed here. Each S3 read
call resolves to an object-key pattern in one of three ways, and anything else fails:

  * a literal key or an f-string with a literal head (`runs/{run_id}/manifest.json`
    -> `runs/*`), on `Bucket=data_bucket()`;
  * a local name assigned from such a literal (`prefix = f"models-mirror/{..}/"`);
  * a name whose last assignment comes from `_scoped_key(uri, <PREFIX_TUPLE>)`, which
    pins the data bucket and the prefix -- the pattern is then each prefix + `*`.

Both directions are asserted, as `test_every_aws_call_a_handler_makes_is_in_its_role`
does for the pipeline Lambdas: a read with no grant dies on AccessDenied in production,
and a grant with no read is how a scoped role widens again without anyone noticing.
"""
from __future__ import annotations

import ast
import fnmatch
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
LAMBDA = REPO / "deploy" / "console" / "lambda_function.py"
POLICY = REPO / "deploy" / "console" / "iam-policy.json"
BUCKET_ARN = "arn:aws:s3:::llmops-agentic-ACCOUNT_ID-REGION"

#: client method -> the IAM action it needs. HeadObject is authorised by s3:GetObject.
READ_OPS = {"get_object": "s3:GetObject", "head_object": "s3:GetObject",
            "list_objects_v2": "s3:ListBucket"}
#: Methods the console calls that are not reads, and so are out of this file's scope
#: (the writes are scoped by S3TasksWrite / S3CustomerDataWrite). Any OTHER method fails
#: the test, so a new read (a paginator, select_object_content, a presigned GET) has to be
#: modelled here before it can ship.
NON_READ_OPS = {"put_object", "generate_presigned_url"}
#: Functions in OTHER modules the console hands its `s3` client to, with what the callee
#: reads through it. service_launch_run reads the plan_uri the agent passes to launch_run;
#: that read is not guarded in conductor_tools (the harness driver shares it), so for the
#: console it is the role that bounds it to the plan prefixes.
HANDOFFS = {"conductor_tools.service_launch_run": ("s3:GetObject", "_PLAN_S3_PREFIXES")}

GUARD = "_scoped_key"


def _tree():
    return ast.parse(LAMBDA.read_text())


def _module_constants(tree):
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return out


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _innermost_function(tree, call):
    best = None
    for fn in _functions(tree):
        if fn.lineno <= call.lineno <= fn.end_lineno:
            if best is None or fn.lineno >= best.lineno:
                best = fn
    return best


def _literal_pattern(node):
    """Key pattern for a literal / f-string-with-literal-head, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (isinstance(node, ast.JoinedStr) and node.values
            and isinstance(node.values[0], ast.Constant)):
        head = node.values[0].value
        # The head must reach at least one "/" -- f"{x}/manifest.json" is not a prefix.
        if "/" in head:
            return head[:head.index("/") + 1] + "*"
    return None


def _guard_scope(fn, call, name, consts):
    """The prefix tuple `name` was bound from by _scoped_key, by its LAST assignment
    above `call`; follows one hop through `scoped = _scoped_key(..)`."""
    def last_binding(var, before):
        hit = None
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and n.lineno < before:
                for t in n.targets:
                    names = [t] if isinstance(t, ast.Name) else (
                        list(t.elts) if isinstance(t, ast.Tuple) else [])
                    if any(isinstance(x, ast.Name) and x.id == var for x in names):
                        if hit is None or n.lineno >= hit.lineno:
                            hit = n
        return hit

    asg = last_binding(name, call.lineno)
    if asg is None:
        return None
    v = asg.value
    if isinstance(v, ast.Name):                      # bucket, key = scoped
        asg = last_binding(v.id, asg.lineno)
        v = asg.value if asg is not None else None
    if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == GUARD
            and len(v.args) == 2 and isinstance(v.args[1], ast.Name)):
        return v.args[1].id if v.args[1].id in consts else None
    return None


def _root_name(node):
    while isinstance(node, ast.Subscript):           # scoped[1]
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_data_bucket_call(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "data_bucket")


def _derive_reads():
    """[(lineno, function, iam_action, key_pattern, how)] for every S3 read; plus the
    list of reads that could not be resolved, which the test reports as failures."""
    tree = _tree()
    consts = _module_constants(tree)
    reads, unresolved = [], []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name) and call.func.value.id == "s3"):
            continue
        op = call.func.attr
        if op not in READ_OPS:
            continue
        fn = _innermost_function(tree, call)
        kw = {k.arg: k.value for k in call.keywords}
        key = kw.get("Prefix" if op == "list_objects_v2" else "Key")
        where = f"line {call.lineno} ({fn.name if fn else '<module>'})"
        pat = _literal_pattern(key)
        if pat is None and isinstance(key, ast.Name) and fn is not None:
            for n in ast.walk(fn):                   # prefix = f"models-mirror/.."
                if (isinstance(n, ast.Assign) and n.lineno < call.lineno
                        and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                        and n.targets[0].id == key.id):
                    pat = _literal_pattern(n.value) or pat
        if pat is not None:
            if not _is_data_bucket_call(kw.get("Bucket")):
                unresolved.append(f"{where}: literal key {pat!r} on a bucket that is not "
                                  "data_bucket()")
                continue
            reads.append((call.lineno, fn.name, READ_OPS[op], pat, "literal"))
            continue
        name = _root_name(key) if key is not None else None
        scope = _guard_scope(fn, call, name, consts) if (fn and name) else None
        bname = _root_name(kw.get("Bucket")) if kw.get("Bucket") is not None else None
        bscope = _guard_scope(fn, call, bname, consts) if (fn and bname) else None
        if scope is None or bscope != scope:
            unresolved.append(f"{where}: s3.{op} key/bucket not derived from a literal or "
                              f"from {GUARD}(uri, <PREFIX_TUPLE>)")
            continue
        for p in consts[scope]:
            reads.append((call.lineno, fn.name, READ_OPS[op], p + "*", scope))
    return reads, unresolved, consts


def _policy_s3():
    doc = json.loads(POLICY.read_text())
    out = []
    for s in doc["Statement"]:
        acts = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        if any(a.lower().startswith("s3:") for a in acts):
            res = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
            out.append((s, acts, res))
    return out


def _get_grants():
    pats = []
    for s, acts, res in _policy_s3():
        if "s3:GetObject" in acts:
            for r in res:
                assert r.startswith(BUCKET_ARN + "/"), (s["Sid"], r)
                pats.append(r[len(BUCKET_ARN) + 1:])
    return pats


def _list_grants():
    pats = []
    for s, acts, res in _policy_s3():
        if "s3:ListBucket" in acts:
            assert res == [BUCKET_ARN], (
                f"{s['Sid']}: ListBucket must name the data bucket ARN only, got {res}")
            prefixes = (s.get("Condition") or {}).get("StringLike", {}).get("s3:prefix")
            assert prefixes, (
                f"{s['Sid']}: an unconditioned ListBucket lists every key name in the "
                "bucket; scope it with StringLike s3:prefix")
            pats.extend(prefixes if isinstance(prefixes, list) else [prefixes])
    return pats


def _all_reads():
    reads, unresolved, consts = _derive_reads()
    tree = _tree()
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        passes_s3 = any(isinstance(a, ast.Name) and a.id == "s3"
                        for a in list(call.args) + [k.value for k in call.keywords])
        if passes_s3:
            callee = ast.unparse(call.func)
            if callee in HANDOFFS:
                action, scope = HANDOFFS[callee]
                for p in consts[scope]:
                    reads.append((call.lineno, callee, action, p + "*", scope))
    return reads, unresolved


# ── the derivation ────────────────────────────────────────────────────────────

def test_every_s3_read_resolves_to_a_key_pattern():
    _, unresolved = _all_reads()
    assert not unresolved, (
        "S3 reads whose key the derivation cannot bound. Route the key through "
        f"{GUARD}(uri, <PREFIX_TUPLE>) or use a literal key on data_bucket():\n  "
        + "\n  ".join(unresolved))


def test_the_derivation_still_finds_reads():
    """A walker that silently matches nothing would pass every assertion below."""
    reads, _ = _all_reads()
    actions = {a for _, _, a, _, _ in reads}
    hows = {h for _, _, _, _, h in reads}
    assert actions == {"s3:GetObject", "s3:ListBucket"}, actions
    assert "literal" in hows and len(hows) >= 3, hows
    fns = {f for _, f, _, _, _ in reads}
    assert {"run_detail", "_verify_s3_uri", "accept_task"} <= fns, fns


def test_every_s3_client_method_the_console_calls_is_modelled():
    tree = _tree()
    used = {c.func.attr for c in ast.walk(tree)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and isinstance(c.func.value, ast.Name) and c.func.value.id == "s3"}
    unknown = used - set(READ_OPS) - NON_READ_OPS
    assert not unknown, f"s3 methods with no IAM mapping here: {sorted(unknown)}"
    presigned = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Attribute) and c.func.attr == "generate_presigned_url"]
    for c in presigned:
        method = c.args[0].value if c.args and isinstance(c.args[0], ast.Constant) else None
        assert method == "put_object", (
            f"line {c.lineno}: presigned {method!r}. A presigned GET is a read signed with "
            "this role's credentials and must be modelled as one.")


def test_every_handoff_of_the_s3_client_is_declared():
    """A helper in another file that reads through the console's client is a read this
    file's AST cannot see (service_launch_run is the live one)."""
    tree = _tree()
    passed_to = {ast.unparse(c.func) for c in ast.walk(tree) if isinstance(c, ast.Call)
                 and any(isinstance(a, ast.Name) and a.id == "s3"
                         for a in list(c.args) + [k.value for k in c.keywords])}
    assert passed_to == set(HANDOFFS), (
        f"console hands its s3 client to {sorted(passed_to)}; declared {sorted(HANDOFFS)}")


# ── the policy, both directions ───────────────────────────────────────────────

def test_no_s3_read_is_granted_on_a_wildcard_resource():
    for s, acts, res in _policy_s3():
        reads = [a for a in acts if a in ("s3:GetObject", "s3:ListBucket")
                 or a.endswith("*")]
        if reads:
            assert "*" not in res and not any(r.endswith(":::*") for r in res), (
                f"{s['Sid']} grants {reads} on {res}")
            assert not any(r.startswith("arn:aws:s3:::*") for r in res), (s["Sid"], res)


def test_s3_read_actions_are_exactly_get_and_list():
    """No s3:Get* / s3:* shorthand: it would grant GetObjectAcl, GetBucketPolicy, ..."""
    for s, acts, _ in _policy_s3():
        for a in acts:
            assert "*" not in a, f"{s['Sid']}: wildcard action {a}"


@pytest.mark.parametrize("action,grants", [
    ("s3:GetObject", _get_grants), ("s3:ListBucket", _list_grants)])
def test_every_derived_read_is_granted(action, grants):
    reads, _ = _all_reads()
    have = grants()
    missing = sorted({f"{p}  <- line {ln} {fn} ({how})"
                      for ln, fn, a, p, how in reads
                      if a == action and not any(fnmatch.fnmatchcase(p, g) for g in have)})
    assert not missing, (
        f"the console makes {action} reads its role does not grant -- each dies on "
        "AccessDenied in production:\n  " + "\n  ".join(missing))


@pytest.mark.parametrize("action,grants", [
    ("s3:GetObject", _get_grants), ("s3:ListBucket", _list_grants)])
def test_nothing_is_granted_that_the_console_does_not_read(action, grants):
    reads, _ = _all_reads()
    pats = {p for _, _, a, p, _ in reads if a == action}
    unused = [g for g in grants() if not any(fnmatch.fnmatchcase(p, g) for p in pats)]
    assert not unused, (
        f"{action} granted on {unused} and no console read matches. Either a read was "
        "removed and its grant outlived it, or the grant is wider than the code.")


def test_scope_tuples_are_top_level_prefixes():
    """A tuple entry without a trailing slash ("runs") would also admit "runsX/..."."""
    consts = _module_constants(_tree())
    for name in ("_PLAN_S3_PREFIXES", "_INTAKE_S3_PREFIXES", "_UPLOAD_S3_PREFIXES"):
        assert consts[name] and all(p.endswith("/") and p.count("/") == 1
                                    for p in consts[name]), (name, consts[name])
    assert consts["_UPLOAD_S3_PREFIXES"] == (consts["CUSTOMER_DATA_PREFIX"] + "/",)
