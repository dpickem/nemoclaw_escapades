#!/usr/bin/env bash
# Verify that all credentials in .env are accepted by their respective APIs.
# Exit code 0 = all OK, non-zero = at least one failure.
#
# Usage:  make test-auth          (sources .env automatically)
#         ./scripts/test_auth.sh  (assumes vars are already exported)

set -euo pipefail

PASS="\033[32m✓\033[0m"
FAIL="\033[31m✗\033[0m"
WARN="\033[33m⚠\033[0m"
failures=0

# ── Helpers ───────────────────────────────────────────────────────────

check_var() {
    local name="$1"
    if [[ -z "${!name:-}" ]]; then
        printf "  ${FAIL} %s is not set\n" "$name"
        ((failures++))
        return 1
    fi
    return 0
}

# ── Slack Bot Token (xoxb-...) ────────────────────────────────────────

echo "Slack Bot Token (SLACK_BOT_TOKEN)"
if check_var SLACK_BOT_TOKEN; then
    status=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
        https://slack.com/api/auth.test)
    if [[ "$status" == "200" ]]; then
        ok=$(curl -s -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
            https://slack.com/api/auth.test | grep -o '"ok":true')
        if [[ -n "$ok" ]]; then
            printf "  ${PASS} auth.test succeeded\n"
        else
            printf "  ${FAIL} auth.test returned ok=false (token rejected)\n"
            ((failures++))
        fi
    else
        printf "  ${FAIL} auth.test returned HTTP %s\n" "$status"
        ((failures++))
    fi
fi

# ── Slack App Token (xapp-...) ────────────────────────────────────────

echo "Slack App Token (SLACK_APP_TOKEN)"
if check_var SLACK_APP_TOKEN; then
    resp=$(curl -s -X POST \
        -H "Authorization: Bearer ${SLACK_APP_TOKEN}" \
        https://slack.com/api/apps.connections.open)
    ok=$(echo "$resp" | grep -o '"ok":true' || true)
    if [[ -n "$ok" ]]; then
        printf "  ${PASS} apps.connections.open succeeded\n"
    else
        error=$(echo "$resp" | grep -o '"error":"[^"]*"' | head -1 || true)
        printf "  ${WARN} apps.connections.open returned ok=false (%s)\n" "${error:-unknown}"
        printf "       This can fail transiently; retry if you see 'internal_error'\n"
    fi
fi

# ── Inference Hub API Key ─────────────────────────────────────────────

INFERENCE_BASE="${INFERENCE_HUB_BASE_URL:-}"
MODEL="${INFERENCE_MODEL:-azure/anthropic/claude-opus-4-6}"

echo "Inference Hub API Key (INFERENCE_HUB_API_KEY)"
if [[ -z "$INFERENCE_BASE" ]]; then
    printf "  ${FAIL} INFERENCE_HUB_BASE_URL is not set — cannot test inference\n"
    ((failures++))
elif check_var INFERENCE_HUB_API_KEY; then
    status=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer ${INFERENCE_HUB_API_KEY}" \
        "${INFERENCE_BASE}/models")
    if [[ "$status" == "200" ]]; then
        printf "  ${PASS} /models returned 200 (key is valid)\n"
    elif [[ "$status" == "401" ]]; then
        printf "  ${FAIL} /models returned 401 (key rejected)\n"
        ((failures++))
    else
        printf "  ${WARN} /models returned HTTP %s (unexpected)\n" "$status"
    fi

    echo "Inference Hub Model (${MODEL})"
    status=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer ${INFERENCE_HUB_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1}" \
        "${INFERENCE_BASE}/chat/completions")
    if [[ "$status" == "200" ]]; then
        printf "  ${PASS} chat/completions returned 200 (model accessible)\n"
    elif [[ "$status" == "401" ]]; then
        printf "  ${FAIL} chat/completions returned 401 (key rejected)\n"
        ((failures++))
    elif [[ "$status" == "404" ]]; then
        printf "  ${FAIL} chat/completions returned 404 (model not found: %s)\n" "$MODEL"
        ((failures++))
    else
        printf "  ${WARN} chat/completions returned HTTP %s\n" "$status"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────

echo ""
if [[ "$failures" -eq 0 ]]; then
    printf "${PASS} All auth checks passed\n"
else
    printf "${FAIL} %d check(s) failed\n" "$failures"
fi
exit "$failures"
