#!/usr/bin/env python3
"""Verify all API credentials from the host (no sandbox needed).

Tests core infrastructure (Slack bot, inference hub) and every
configured service tool (Jira, GitLab, Gerrit, Confluence, Slack user)
by making one lightweight API call per service.  Unconfigured services
are skipped.

Loads ``.env`` automatically so the script works standalone::

  PYTHONPATH=src python scripts/test_auth.py

Or via the Makefile (which also exports .env)::

  make test-auth
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

_PASS = "\033[32m✓\033[0m"
_FAIL = "\033[31m✗\033[0m"
_SKIP = "\033[33m–\033[0m"
_WARN = "\033[33m⚠\033[0m"
_SEPARATOR = "═" * 59

_HTTP_TIMEOUT = 15.0


def _load_dotenv(path: str = ".env") -> None:
    """Populate ``os.environ`` from a .env file (existing vars take precedence)."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


# ---------------------------------------------------------------------------
# Core infrastructure checks (pure httpx — no tool client dependency)
# ---------------------------------------------------------------------------


async def _check_slack_bot() -> bool | None:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print(f"  {_FAIL} SLACK_BOT_TOKEN not set")
        return False

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        if data.get("ok"):
            print(f"  {_PASS} auth.test succeeded (user: {data.get('user', '?')})")
            return True
        print(f"  {_FAIL} auth.test returned ok=false ({data.get('error', 'unknown')})")
        return False


async def _check_slack_app() -> bool | None:
    token = os.environ.get("SLACK_APP_TOKEN", "")
    if not token:
        print(f"  {_FAIL} SLACK_APP_TOKEN not set")
        return False

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            "https://slack.com/api/apps.connections.open",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        if data.get("ok"):
            print(f"  {_PASS} apps.connections.open succeeded")
            return True
        error = data.get("error", "unknown")
        print(f"  {_WARN} apps.connections.open returned ok=false ({error})")
        print(f"       This can fail transiently; retry if you see 'internal_error'")
        return True  # treat as non-fatal


async def _check_inference() -> bool | None:
    base_url = os.environ.get("INFERENCE_HUB_BASE_URL", "")
    api_key = os.environ.get("INFERENCE_HUB_API_KEY", "")
    model = os.environ.get("INFERENCE_MODEL", "azure/anthropic/claude-opus-4-6")

    if not base_url:
        print(f"  {_FAIL} INFERENCE_HUB_BASE_URL not set")
        return False
    if not api_key:
        print(f"  {_FAIL} INFERENCE_HUB_API_KEY not set")
        return False

    headers = {"Authorization": f"Bearer {api_key}"}
    ok = True

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(f"{base_url}/models", headers=headers)
        if resp.status_code == 200:
            print(f"  {_PASS} /models returned 200 (key valid)")
        elif resp.status_code == 401:
            print(f"  {_FAIL} /models returned 401 (key rejected)")
            return False
        else:
            print(f"  {_WARN} /models returned HTTP {resp.status_code}")

        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={**headers, "Content-Type": "application/json"},
            content=json.dumps(payload),
        )
        if resp.status_code == 200:
            print(f"  {_PASS} chat/completions returned 200 (model: {model})")
        elif resp.status_code == 404:
            print(f"  {_FAIL} chat/completions returned 404 (model not found: {model})")
            ok = False
        elif resp.status_code == 401:
            print(f"  {_FAIL} chat/completions returned 401 (key rejected)")
            ok = False
        else:
            print(f"  {_WARN} chat/completions returned HTTP {resp.status_code}")

    return ok


# ---------------------------------------------------------------------------
# Service tool checks (use the actual client classes)
# ---------------------------------------------------------------------------


async def _check_jira() -> bool | None:
    url = os.environ.get("JIRA_URL", "https://jirasw.nvidia.com")
    auth = os.environ.get("JIRA_AUTH", "")
    if not auth:
        print(f"  {_SKIP} JIRA_AUTH not set — skipped")
        return None

    from nemoclaw_escapades.tools.jira import JiraClient

    client = JiraClient(base_url=url, auth_header=auth)
    try:
        result = await client.me()
        if "error" in result:
            print(f"  {_FAIL} {result['error']}")
            return False
        name = result.get("displayName", result.get("name", "unknown"))
        print(f"  {_PASS} Authenticated as {name}")
        return True
    except Exception as exc:
        print(f"  {_FAIL} {exc}")
        return False
    finally:
        await client.close()


async def _check_gitlab() -> bool | None:
    url = os.environ.get("GITLAB_URL", "https://gitlab-master.nvidia.com")
    token = os.environ.get("GITLAB_TOKEN", "")
    if not token:
        print(f"  {_SKIP} GITLAB_TOKEN not set — skipped")
        return None

    from nemoclaw_escapades.tools.gitlab import GitLabClient

    client = GitLabClient(base_url=url, token=token)
    try:
        result = await client.get_current_user()
        if "error" in result:
            print(f"  {_FAIL} {result['error']}")
            return False
        name = result.get("name", result.get("username", "unknown"))
        print(f"  {_PASS} Authenticated as {name}")
        return True
    except Exception as exc:
        print(f"  {_FAIL} {exc}")
        return False
    finally:
        await client.close()


async def _check_gerrit() -> bool | None:
    url = os.environ.get("GERRIT_URL", "https://git-av.nvidia.com/r/a")
    username = os.environ.get("GERRIT_USERNAME", "")
    password = os.environ.get("GERRIT_HTTP_PASSWORD", "")
    if not (username and password):
        print(f"  {_SKIP} GERRIT_USERNAME/PASSWORD not set — skipped")
        return None

    from nemoclaw_escapades.tools.gerrit import GerritClient

    client = GerritClient(base_url=url, username=username, http_password=password)
    try:
        result = await client.get_account()
        if "error" in result:
            print(f"  {_FAIL} {result['error']}")
            return False
        name = result.get("name", result.get("username", "unknown"))
        print(f"  {_PASS} Authenticated as {name}")
        return True
    except Exception as exc:
        print(f"  {_FAIL} {exc}")
        return False
    finally:
        await client.close()


async def _check_confluence() -> bool | None:
    url = os.environ.get("CONFLUENCE_URL", "")
    username = os.environ.get("CONFLUENCE_USERNAME", "")
    api_token = os.environ.get("CONFLUENCE_API_TOKEN", "")
    if not (url and username and api_token):
        missing = [v for v, val in [
            ("CONFLUENCE_URL", url), ("CONFLUENCE_USERNAME", username),
            ("CONFLUENCE_API_TOKEN", api_token),
        ] if not val]
        print(f"  {_SKIP} {', '.join(missing)} not set — skipped")
        return None

    from nemoclaw_escapades.tools.confluence import ConfluenceClient

    client = ConfluenceClient(base_url=url, username=username, api_token=api_token)
    try:
        result = await client.search("type=page", limit=1)
        if "error" in result:
            print(f"  {_FAIL} {result['error']}")
            return False
        print(f"  {_PASS} Search returned successfully")
        return True
    except Exception as exc:
        print(f"  {_FAIL} {exc}")
        return False
    finally:
        await client.close()


async def _check_slack_user() -> bool | None:
    token = os.environ.get("SLACK_USER_TOKEN", "")
    if not token:
        print(f"  {_SKIP} SLACK_USER_TOKEN not set — skipped")
        return None

    from nemoclaw_escapades.tools.slack_search import SlackSearchClient

    client = SlackSearchClient(user_token=token)
    try:
        result = await client.list_channels(limit=1)
        if "error" in result:
            print(f"  {_FAIL} {result['error']}")
            return False
        print(f"  {_PASS} API responded successfully")
        return True
    except Exception as exc:
        print(f"  {_FAIL} {exc}")
        return False
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_CHECKS: list[tuple[str, object]] = [
    ("Slack Bot Token", _check_slack_bot),
    ("Slack App Token", _check_slack_app),
    ("Inference Hub", _check_inference),
    ("Jira", _check_jira),
    ("GitLab", _check_gitlab),
    ("Gerrit", _check_gerrit),
    ("Confluence", _check_confluence),
    ("Slack (user token)", _check_slack_user),
]


async def _run_all() -> int:
    failures = 0
    for label, check_fn in _CHECKS:
        print(label)
        result = await check_fn()  # type: ignore[operator]
        if result is False:
            failures += 1
        print()
    return failures


def main() -> None:
    print(_SEPARATOR)
    print(" Credential Check (host-side, no sandbox)")
    print(_SEPARATOR)
    print()

    failures = asyncio.run(_run_all())

    print(_SEPARATOR)
    if failures == 0:
        print(f"{_PASS} All checks passed")
    else:
        print(f"{_FAIL} {failures} check(s) failed")
    print(_SEPARATOR)
    sys.exit(failures)


if __name__ == "__main__":
    main()
