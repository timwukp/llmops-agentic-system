#!/usr/bin/env bash
# Deploy the LLMOps Admin dashboard: Lambda + HTTP API Gateway + DynamoDB + Cognito.
# Ported from bedrock-agentcore-agent-ops-console/deploy/deploy.sh.
# Idempotent-ish: safe to re-run for code updates (skips resources that already exist).
#
# All resource names are llmops-prefixed on purpose — this stack must NOT collide with
# an existing agent-cicd-admin deployment in the same account.
#
# Prereqs: aws cli v2 configured, python >= 3.10, zip.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── configuration ─────────────────────────────────────────────────────────────
REGION="${REGION:-us-east-1}"
DATA_BUCKET="${DATA_BUCKET:-}"                 # optional; Lambda falls back to SSM /llmops/storage/bucket
JUDGE_MODEL="${JUDGE_MODEL:-global.anthropic.claude-opus-5}"
SPANS_SINCE="${SPANS_SINCE:-2026-07-28T12:00:00Z}"  # ISO ts when OTEL_TRACES_SAMPLER=always_on was enabled
OPTIMIZE_HARNESS="${OPTIMIZE_HARNESS:-llmops_orchestrator}"

# ── security tunables (ported) ────────────────────────────────────────────────
# PLUS enables Cognito threat protection: sign-ins using credentials found in public breaches
# are blocked and anomalous attempts are risk-scored. Paid feature plan — set
# COGNITO_TIER=ESSENTIALS to opt out, accepting that credential stuffing is then unmitigated.
COGNITO_TIER="${COGNITO_TIER:-PLUS}"
PASSWORD_MIN_LENGTH="${PASSWORD_MIN_LENGTH:-20}"
# Caps how fast anyone can grind POST /api/login, the only unauthenticated write route.
API_RATE_LIMIT="${API_RATE_LIMIT:-20}"         # steady-state requests/second
API_BURST_LIMIT="${API_BURST_LIMIT:-40}"       # burst bucket

FN=llmops-admin
ROLE=LlmopsAdminLambdaRole
TABLE=LlmopsAdminRuns
POOL_NAME=llmops-admin-pool
SECRET_NAME=llmops-admin/dashboard-login
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "Account $ACCOUNT_ID / $REGION"

# ── DynamoDB (console state: optimization drafts) ─────────────────────────────
aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1 || \
  aws dynamodb create-table --table-name "$TABLE" --region "$REGION" \
    --attribute-definitions AttributeName=id,AttributeType=S \
    --key-schema AttributeName=id,KeyType=HASH --billing-mode PAY_PER_REQUEST

# ── IAM role (see deploy/console/iam-policy.json for the full permission set) ─
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document '{
    "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
fi
# `_comment` keys carry WHY each statement is scoped the way it is, which belongs
# next to the statement rather than in a doc nobody opens when editing IAM. But IAM
# rejects any key it does not know, failing the WHOLE document with "Syntax errors
# in policy" — it does not name the offending key, so this cost a debugging round.
# Strip them at apply time: the file keeps the rationale, IAM gets a clean policy.
sed "s/ACCOUNT_ID/$ACCOUNT_ID/g; s/REGION/$REGION/g" "$(dirname "$0")/iam-policy.json" \
  | jq 'del(.Statement[]._comment)' > /tmp/llmops-admin-policy.json
aws iam put-role-policy --role-name "$ROLE" --policy-name LlmopsAdminPerms --policy-document file:///tmp/llmops-admin-policy.json

# ── Cognito (admin login) ─────────────────────────────────────────────────────
POOL_ID=$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
  --query "UserPools[?Name=='$POOL_NAME'].Id" --output text)
if [ -z "$POOL_ID" ] || [ "$POOL_ID" = "None" ]; then
  # RequireSymbols stays false on purpose: the password generated below is alphanumeric, so
  # requiring symbols would make admin-set-user-password reject this script's own password.
  # Length is the strength knob instead. AllowAdminCreateUserOnly=true disables self-signup —
  # the dashboard authorizes on "any valid token from this pool", so an extra account would
  # inherit every write endpoint.
  POOL_ID=$(aws cognito-idp create-user-pool --pool-name "$POOL_NAME" --region "$REGION" \
    --policies "{\"PasswordPolicy\":{\"MinimumLength\":$PASSWORD_MIN_LENGTH,\"RequireUppercase\":true,\"RequireLowercase\":true,\"RequireNumbers\":true,\"RequireSymbols\":false}}" \
    --admin-create-user-config '{"AllowAdminCreateUserOnly":true}' \
    --user-pool-tier "$COGNITO_TIER" \
    $([ "$COGNITO_TIER" = "PLUS" ] && echo "--user-pool-add-ons AdvancedSecurityMode=ENFORCED") \
    --deletion-protection ACTIVE --query 'UserPool.Id' --output text)
  CLIENT_ID=$(aws cognito-idp create-user-pool-client --user-pool-id "$POOL_ID" --region "$REGION" \
    --client-name llmops-admin-dashboard --no-generate-secret \
    --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --access-token-validity 8 --id-token-validity 8 --refresh-token-validity 30 \
    --token-validity-units '{"AccessToken":"hours","IdToken":"hours","RefreshToken":"days"}' \
    --prevent-user-existence-errors ENABLED --query 'UserPoolClient.ClientId' --output text)
  # guarantee all required classes: random core + forced digit suffix
  PASSWORD="Adm-$(openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c 16)$(openssl rand -base64 9 | tr -dc '0-9' | head -c 4)"
  # extremely unlikely, but if the digit pool came up short, pad deterministically-random digits
  while [ ${#PASSWORD} -lt 24 ]; do PASSWORD="${PASSWORD}$(( RANDOM % 10 ))"; done
  aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username admin --message-action SUPPRESS --region "$REGION"
  aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" --username admin --password "$PASSWORD" --permanent --region "$REGION"
  aws secretsmanager create-secret --region "$REGION" --name "$SECRET_NAME" \
    --secret-string "{\"username\":\"admin\",\"password\":\"$PASSWORD\",\"poolId\":\"$POOL_ID\",\"clientId\":\"$CLIENT_ID\"}" >/dev/null
  echo "Cognito admin user created; password stored in Secrets Manager: $SECRET_NAME"
else
  CLIENT_ID=$(aws cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --region "$REGION" \
    --query 'UserPoolClients[0].ClientId' --output text)
  # Report drift on an existing pool but do NOT "fix" it here. update-user-pool has PUT
  # semantics, not PATCH: any field omitted from the call reverts to its default. A convenient
  # single-field call to raise the tier would silently reset AllowAdminCreateUserOnly and
  # re-open self-signup. To apply changes: read the current config, merge, send whole, read back.
  read -r CUR_TIER CUR_MINLEN CUR_ADMINONLY < <(aws cognito-idp describe-user-pool \
    --user-pool-id "$POOL_ID" --region "$REGION" --output text \
    --query 'UserPool.[UserPoolTier,Policies.PasswordPolicy.MinimumLength,AdminCreateUserConfig.AllowAdminCreateUserOnly]' \
    2>/dev/null || echo "? ? ?")
  [ "$CUR_TIER" = "PLUS" ] || echo "  note: pool tier is $CUR_TIER (not PLUS) — threat protection is off"
  { [ "$CUR_MINLEN" -ge "$PASSWORD_MIN_LENGTH" ]; } 2>/dev/null \
    || echo "  note: password minimum is $CUR_MINLEN (want >= $PASSWORD_MIN_LENGTH)"
  [ "$CUR_ADMINONLY" = "True" ] \
    || echo "  WARNING: self-signup is ENABLED on this pool — any stranger can register and, because the dashboard accepts any valid token from it, gain every write endpoint"
fi

# ── package: vendor boto3 (Lambda's builtin may predate AgentCore harness APIs) ─
BUILD=$(mktemp -d)
# AgentCore batch-eval/recommendation APIs need boto3 >= 1.43.51, which requires
# Python >= 3.10 — macOS system python3 (3.9) silently vendors 1.42.x and every
# batch-eval op then fails at runtime with "object has no attribute". Pick a
# modern interpreter explicitly and hard-fail if the vendored version is short.
PY_FOR_BUILD=""
for cand in python3.12 python3.11 python3.10 "$SCRIPT_DIR/../../.venv/bin/python"; do
  if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then PY_FOR_BUILD="$cand"; break; fi
done
[ -z "$PY_FOR_BUILD" ] && { echo "FATAL: no python >= 3.10 found for vendoring boto3"; exit 1; }
"$PY_FOR_BUILD" -m pip install -q "boto3>=1.43.51" -t "$BUILD"
VENDORED=$("$PY_FOR_BUILD" -c "import sys; sys.path.insert(0,'$BUILD'); import boto3; print(boto3.__version__)")
case "$VENDORED" in
  1.4[3-9].*|1.[5-9]*|[2-9].*) echo "vendored boto3 $VENDORED OK";;
  *) echo "FATAL: vendored boto3 $VENDORED < 1.43.51"; exit 1;;
esac
cp "$(dirname "$0")/lambda_function.py" "$BUILD/lambda_function.py"
cp "$(dirname "$0")/frontend.html" "$BUILD/frontend.html"   # read once at cold start by the handler
# The gate arithmetic, vendored flat because the handler does `import cost_model`.
# Without it every estimate refuses with 503 and the Cost tab reports the rate card
# as unavailable -- which is the fail-closed behaviour working, but it means the tab
# renders and does nothing. Measured on the first live deploy: the tab was up and
# `rate_card.present` was false, so this copy is the difference between a visible
# panel and a working one.
cp "$(dirname "$0")/../../pipeline/contracts/cost_model.py" "$BUILD/cost_model.py"
"$PY_FOR_BUILD" -c "import sys; sys.path.insert(0,'$BUILD'); import cost_model; \
  assert hasattr(cost_model,'approval_decision'), 'cost_model missing approval_decision'; \
  print('bundled cost_model OK')"
(cd "$BUILD" && zip -rq /tmp/llmops-admin-dashboard.zip .)

ENV_VARS="Variables={CONSOLE_TABLE=$TABLE,RUNS_TABLE=llmops-pipeline-runs,EVENTS_TABLE=llmops-stage-events,STATE_MACHINE=llmops-pipeline,START_FN=llmops-start-pipeline,DATA_BUCKET=$DATA_BUCKET,COGNITO_POOL_ID=$POOL_ID,COGNITO_CLIENT_ID=$CLIENT_ID,JUDGE_MODEL=$JUDGE_MODEL,SPANS_SINCE=$SPANS_SINCE,OPTIMIZE_HARNESS=$OPTIMIZE_HARNESS}"

if aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FN" --zip-file fileb:///tmp/llmops-admin-dashboard.zip --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FN" --environment "$ENV_VARS" --region "$REGION" >/dev/null
else
  aws lambda create-function --function-name "$FN" --runtime python3.12 --timeout 300 --memory-size 512 \
    --role "arn:aws:iam::$ACCOUNT_ID:role/$ROLE" --handler lambda_function.handler \
    --zip-file fileb:///tmp/llmops-admin-dashboard.zip --environment "$ENV_VARS" --region "$REGION" >/dev/null
fi
aws lambda wait function-active --function-name "$FN" --region "$REGION"

# ── HTTP API Gateway (avoids org guardrails that block public Lambda Function URLs) ─
API_ID=$(aws apigatewayv2 get-apis --region "$REGION" --query "Items[?Name=='$FN-api'].ApiId" --output text)
if [ -z "$API_ID" ] || [ "$API_ID" = "None" ]; then
  API_ID=$(aws apigatewayv2 create-api --name "$FN-api" --protocol-type HTTP --region "$REGION" \
    --target "arn:aws:lambda:$REGION:$ACCOUNT_ID:function:$FN" --query ApiId --output text)
  aws lambda add-permission --function-name "$FN" --statement-id apigw --region "$REGION" \
    --action lambda:InvokeFunction --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT_ID:$API_ID/*"
fi

# Throttle the auto-created $default stage. Applied on every run (not just on create) so
# existing deployments pick it up when they re-run this script for a code update.
aws apigatewayv2 update-stage --api-id "$API_ID" --stage-name '$default' --region "$REGION" \
  --default-route-settings "ThrottlingRateLimit=$API_RATE_LIMIT,ThrottlingBurstLimit=$API_BURST_LIMIT,DetailedMetricsEnabled=true" \
  >/dev/null

echo ""
echo "Dashboard: https://$API_ID.execute-api.$REGION.amazonaws.com/"
echo "Login: username 'admin'; password in Secrets Manager '$SECRET_NAME'"
