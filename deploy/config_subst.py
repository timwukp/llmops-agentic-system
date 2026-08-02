"""Resolve `<PLACEHOLDER>` tokens in a harness config at deploy time.

Why this exists. The s3 skill-source shape is a single URI --
``{"s3": {"uri": "s3://bucket/prefix"}}`` -- and this account's bucket is
``llmops-agentic-<ACCOUNT_ID>-<REGION>``, so the bucket NAME embeds the account id.
``agents/*/harness.json`` are files in a public repo where no account id may appear
(enforced by ``hooks/pre-commit`` and ``.github/workflows/redaction-check.yml``). So a
literal URI cannot be committed, and before this module a placeholder URI had nothing to
resolve it: ``deploy/05_harnesses.py`` did ``strip_comments`` + ``ensure_env`` and no
substitution whatsoever. ``deploy/01_iam.py`` has resolved exactly these tokens in its
policy documents since Phase 1; this is that mechanism, applied to harness configs.

**The unresolved-token check is the load-bearing half of this file.** A skill source that
is wrong or unreachable is accepted by ``UpdateHarness``, mints a version, reports READY,
and then fails at SESSION START -- on every invocation, with the config still reading
healthy. So a typo like ``<DATABUCKET>`` would not fail here, or at validation, or at
apply: it would ship a harness that looks deployed and cannot start a session. Deploy time
is the last moment where catching it costs nothing, which is why ``resolve()`` refuses to
return a config with a token left in it rather than passing one through to AWS.
"""
import re

#: Any `<UPPER_SNAKE>` token. Deliberately broader than the mapping's own keys: the
#: failure being guarded is a token nobody has a value for, so the pattern must match
#: tokens this module has never heard of.
PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_]*>")


def default_bucket(account_id, region):
    """The data bucket's name, derived the same way `deploy/01_iam.py:171` derives it."""
    return f"llmops-agentic-{account_id}-{region}"


def mapping_for(account_id, region, bucket=None):
    """Build the substitution mapping. `bucket` overrides the derived default.

    Callers that can reach SSM should pass the published `/llmops/storage/bucket` value:
    that parameter is what `03_storage.py` actually created, and a `--bucket` given to
    `01_iam.py` would make the derived name disagree with it. The derivation stays as the
    offline fallback so `--dry-run --account-id ...` needs no AWS call at all.
    """
    return {
        "<ACCOUNT_ID>": str(account_id),
        "<REGION>": region,
        "<DATA_BUCKET>": bucket or default_bucket(account_id, region),
    }


def substitute(obj, mapping):
    """Recursively replace mapping keys in every string of a JSON structure."""
    if isinstance(obj, str):
        for k, v in mapping.items():
            obj = obj.replace(k, v)
        return obj
    if isinstance(obj, list):
        return [substitute(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: substitute(v, mapping) for k, v in obj.items()}
    return obj


def unresolved(obj, _found=None):
    """Every `<PLACEHOLDER>` token still present, sorted and de-duplicated.

    Walks the whole structure rather than stopping at the first hit, so one deploy tells
    you every token you have to supply instead of one per re-run.
    """
    found = set() if _found is None else _found
    if isinstance(obj, str):
        found.update(PLACEHOLDER.findall(obj))
    elif isinstance(obj, list):
        for x in obj:
            unresolved(x, found)
    elif isinstance(obj, dict):
        for v in obj.values():
            unresolved(v, found)
    return sorted(found)


def resolve(cfg, mapping, where=""):
    """Substitute, then REFUSE a config with any token left unresolved.

    Raising here is the point. The alternative -- ship it and find out -- means an
    `UpdateHarness` that succeeds, a version that is minted, a status that reads READY,
    and a session that dies at start on every single invocation.
    """
    out = substitute(cfg, mapping)
    left = unresolved(out)
    if left:
        raise SystemExit(
            f"{where or 'config'}: unresolved placeholder(s) {left}. Known tokens are "
            f"{sorted(mapping)}. AgentCore would accept this config, mint a version and "
            "report READY -- and then every session would fail at START, because a bad "
            "skill source is not validated until a session tries to fetch it.")
    return out
