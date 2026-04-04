#!/usr/bin/env bash
# test_credential_injection.sh — Verify that OpenShell injects provider
# credentials into a sandbox as environment variables matching .env values.
#
# Usage:  ./scripts/test_credential_injection.sh
#
# What it does:
#   1. Reads SLACK_BOT_TOKEN, SLACK_APP_TOKEN, INFERENCE_HUB_API_KEY from .env
#   2. Creates a generic provider with those credentials
#   3. Verifies the provider was stored correctly via `openshell provider get`
#   4. Spins up an ephemeral sandbox with the provider attached
#   5. Runs `env` inside the sandbox to check what the credentials look like
#   6. Cleans up the provider (sandbox auto-deletes via --no-keep)
#
# This tells us whether credentials arrive as real values, placeholders,
# or not at all — so we know what the orchestrator sandbox will see.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"

PROVIDER_NAME="test-cred-injection"
SANDBOX_NAME="test-cred-sandbox"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
RESET='\033[0m'

pass() { echo -e "${GREEN}✓ $1${RESET}"; }
fail() { echo -e "${RED}✗ $1${RESET}"; }
info() { echo -e "${YELLOW}→ $1${RESET}"; }
header() { echo -e "\n${BOLD}── $1 ──${RESET}"; }

cleanup() {
    info "Cleaning up..."
    openshell sandbox delete "$SANDBOX_NAME" 2>/dev/null || true
    openshell provider delete "$PROVIDER_NAME" 2>/dev/null || true
}
trap cleanup EXIT

# ── Helper: read a key=value from .env ─────────────────────────────────────

env_val() {
    grep "^${1}=" "$ENV_FILE" | head -1 | cut -d= -f2-
}

# ── Prerequisites ──────────────────────────────────────────────────────────

if ! command -v openshell >/dev/null 2>&1; then
    fail "openshell CLI not found"
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    fail ".env file not found at $ENV_FILE"
    exit 1
fi

# ── Load credentials from .env ─────────────────────────────────────────────

EXPECT_SLACK_BOT_TOKEN=$(env_val SLACK_BOT_TOKEN)
EXPECT_SLACK_APP_TOKEN=$(env_val SLACK_APP_TOKEN)
EXPECT_INFERENCE_HUB_API_KEY=$(env_val INFERENCE_HUB_API_KEY)

for key in SLACK_BOT_TOKEN SLACK_APP_TOKEN INFERENCE_HUB_API_KEY; do
    val=$(env_val "$key")
    if [ -z "$val" ]; then
        fail "$key is empty or missing in .env"
        exit 1
    fi
    info "$key = ${val:0:12}..."
done

# ── Step 1: Create provider ────────────────────────────────────────────────

header "Step 1: Create a generic provider with .env credentials"
info "Provider: $PROVIDER_NAME"

openshell provider delete "$PROVIDER_NAME" 2>/dev/null || true

openshell provider create \
    --name "$PROVIDER_NAME" \
    --type generic \
    --credential "SLACK_BOT_TOKEN=${EXPECT_SLACK_BOT_TOKEN}" \
    --credential "SLACK_APP_TOKEN=${EXPECT_SLACK_APP_TOKEN}" \
    --credential "INFERENCE_HUB_API_KEY=${EXPECT_INFERENCE_HUB_API_KEY}"

pass "Provider created"

# ── Step 2: Verify provider stores the credentials ────────────────────────

header "Step 2: Verify provider credentials via 'openshell provider get'"

PROVIDER_OUTPUT=$(openshell provider get "$PROVIDER_NAME" 2>&1)
echo "$PROVIDER_OUTPUT"

ALL_KEYS_FOUND=true
for key in SLACK_BOT_TOKEN SLACK_APP_TOKEN INFERENCE_HUB_API_KEY; do
    if echo "$PROVIDER_OUTPUT" | grep -q "$key"; then
        pass "Provider shows credential key: $key"
    else
        fail "Provider does not list credential key: $key"
        ALL_KEYS_FOUND=false
    fi
done

if [ "$ALL_KEYS_FOUND" = false ]; then
    echo "  (credentials may be redacted in output — continuing anyway)"
fi

# ── Step 3: Spin up ephemeral sandbox with the provider ────────────────────

header "Step 3: Create ephemeral sandbox and read env vars inside"
info "Sandbox: $SANDBOX_NAME (--no-keep, auto-deletes after command)"
info "Image: python:3.11-slim"
info "Command: env | sort"

# Use the default OpenShell sandbox image (has sandbox user, iproute2, etc.).
# No --from flag needed — OpenShell uses its stock image.
# --no-keep auto-deletes the sandbox after the command exits.
# We tee to a temp file so the user sees progress in real time.
TMPOUT=$(mktemp)
openshell sandbox create \
    --name "$SANDBOX_NAME" \
    --provider "$PROVIDER_NAME" \
    --no-keep \
    -- sh -c "echo '--- ENV DUMP ---'; env | sort; echo '--- END ---'" 2>&1 \
    | tee "$TMPOUT"

SANDBOX_OUTPUT=$(cat "$TMPOUT")
rm -f "$TMPOUT"

# ── Step 4: Verify each credential inside the sandbox ──────────────────────

header "Step 4: Compare sandbox env vars against .env values"

sandbox_val() {
    echo "$SANDBOX_OUTPUT" | grep "^${1}=" | head -1 | cut -d= -f2-
}

PASS_COUNT=0
FAIL_COUNT=0
TOTAL=3

check_cred() {
    local key="$1"
    local expected="$2"
    local actual
    actual=$(sandbox_val "$key")

    if [ -z "$actual" ]; then
        fail "$key is NOT set inside the sandbox"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    elif [ "$actual" = "$expected" ]; then
        pass "$key matches .env (real value injected)"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        fail "$key is set but DIFFERS from .env (likely a placeholder)"
        echo "    .env:    ${expected:0:20}..."
        echo "    sandbox: ${actual:0:20}..."
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

check_cred "SLACK_BOT_TOKEN"       "$EXPECT_SLACK_BOT_TOKEN"
check_cred "SLACK_APP_TOKEN"       "$EXPECT_SLACK_APP_TOKEN"
check_cred "INFERENCE_HUB_API_KEY" "$EXPECT_INFERENCE_HUB_API_KEY"

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
header "Summary"
echo "  $PASS_COUNT/$TOTAL credentials match .env values"

if [ "$FAIL_COUNT" -eq 0 ]; then
    pass "All credentials injected correctly — sandbox deployment should work"
else
    fail "$FAIL_COUNT credential(s) failed — investigate before deploying"
fi
