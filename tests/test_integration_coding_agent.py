"""Subprocess-level end-to-end test for the coding sub-agent.

Exercises the design_m2b.md §14 Phase-1 exit criterion:

    *Coding agent process starts, handles task, returns result.*

Unlike ``tests/test_coding_agent_main.py`` — which calls
``_run_cli_mode`` directly with a faked ``AgentLoop`` — this suite
spawns ``python -m nemoclaw_escapades.agent --task ...`` as a
separate process and lets it go through the full startup sequence:

    runtime self-check
    → ``AppConfig.load``
    → logging setup
    → ``InferenceHubBackend`` HTTP client
    → ``create_coding_tool_registry`` (file / search / bash / git)
    → ``AgentLoop.run`` (one inference round)
    → stdout

The inference endpoint is a locally-hosted OpenAI-format mock (stdlib
``http.server`` on a random port) so the test neither depends on
external network nor spins up a real provider.  The mock returns a
canned ``finish_reason="stop"`` response on the first request, which
terminates the loop immediately — total runtime is well under a
second and the CI is hermetic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


_CANNED_REPLY = "hello from the mock inference server"


class _MockInferenceHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-format mock.

    ``POST /chat/completions`` returns a single ``finish_reason="stop"``
    assistant message with no tool calls.  That's the shortest possible
    terminating response from the ``AgentLoop``'s perspective.
    """

    def do_POST(self) -> None:  # noqa: N802 - stdlib method signature
        length = int(self.headers.get("Content-Length", "0"))
        # Drain but ignore the request body — the test only cares that
        # the backend reached us, not the exact payload shape.  The
        # in-process tests in ``test_backend_inference_hub.py`` already
        # cover request-building.
        if length:
            _ = self.rfile.read(length)
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": _CANNED_REPLY,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "model": "mock-model",
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silence the default stderr access log — it clutters pytest
        # output without adding diagnostic value.
        return


@pytest.fixture
def mock_inference_url() -> Iterator[str]:
    """Start an OpenAI-format mock on 127.0.0.1 and yield its base URL.

    The server binds to port 0 (kernel-picked free port) so parallel
    test runs don't collide.  Shut down and closed cleanly on teardown.
    """
    server = HTTPServer(("127.0.0.1", 0), _MockInferenceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _repo_root() -> Path:
    """Project root, inferred from this file's path."""
    return Path(__file__).resolve().parent.parent


def _clean_subprocess_env(
    mock_inference_url: str,
    workspace: Path,
    home: Path,
) -> dict[str, str]:
    """Build a minimal env dict for the sub-agent subprocess.

    Replaces the parent's environment entirely so no stray vars from
    the developer's shell or ``.env`` leak into the test.  Only the
    keys the sub-agent actually needs are included.
    """
    env: dict[str, str] = {
        # Needed for the child to exec ``python`` and any subprocess
        # tools (``git``, ``bash``) the coding suite registers.
        "PATH": os.environ.get("PATH", ""),
        # Avoid the child reading ``~/.nemoclaw/...`` or picking up a
        # config path from the developer's real home.
        "HOME": str(home),
        # Package discovery: editable / PYTHONPATH install via src/.
        "PYTHONPATH": str(_repo_root() / "src"),
        # Required secrets for ``_check_required_secrets`` in local dev.
        "SLACK_BOT_TOKEN": "test-bot",
        "SLACK_APP_TOKEN": "test-app",
        "INFERENCE_HUB_API_KEY": "test-key",
        "INFERENCE_HUB_BASE_URL": mock_inference_url,
        # Tight timeout / no retries: if the mock fails for any reason
        # we want the subprocess to fail fast rather than hang pytest.
        "INFERENCE_TIMEOUT_S": "5",
        "INFERENCE_MAX_RETRIES": "1",
        # Workspace root for the coding tools.  Sub-agent further scopes
        # this to ``<root>/agent-<hex>`` so the test dir just needs to
        # exist.
        "CODING_WORKSPACE_ROOT": str(workspace),
        # Skills dir — point at the shipped folder so ``SkillLoader``
        # is a no-op if nothing matches.  The CLI-mode path doesn't
        # construct a SkillLoader anyway, but the config still wants
        # a valid path.
        "SKILLS_DIR": str(_repo_root() / "skills"),
    }
    return env


def test_agent_subprocess_runs_task_end_to_end(
    tmp_path: Path,
    mock_inference_url: str,
) -> None:
    """``python -m nemoclaw_escapades.agent --task ...`` prints the reply.

    Full end-to-end: spawn the module, wait for it to produce the
    assistant's reply on stdout, assert a clean exit.

    Covers the Phase-1 exit criterion from design_m2b.md §14:

        *Coding agent process starts, assembles the ``AgentLoop``
        with the coding tool suite + skill loader, runs a task.*

    and the first half of:

        *Coding agent process receives ``task.assign`` over NMB and
        sends ``task.complete`` (deferred: receive-loop body is a
        Phase 2 TODO).*

    The NMB side lands with the Phase 2 delegation flow; the CLI path
    here is the other half of the exit criterion.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    env = _clean_subprocess_env(mock_inference_url, workspace, home)

    result = subprocess.run(
        [sys.executable, "-m", "nemoclaw_escapades.agent", "--task", "say hi"],
        env=env,
        cwd=tmp_path,  # avoid picking up the repo's .env / config/ dir
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    # Fail loudly with both streams on abnormal exit — otherwise
    # debugging a CI failure requires re-running locally.
    assert result.returncode == 0, (
        f"agent exited with code {result.returncode}\n"
        f"---- stdout ----\n{result.stdout}\n"
        f"---- stderr ----\n{result.stderr}\n"
    )
    # The mock's canned reply makes it to stdout.  Structured log
    # records go to stderr so they don't interleave with the task
    # result.
    assert _CANNED_REPLY in result.stdout, (
        f"expected canned reply in stdout; got:\n{result.stdout!r}\n"
        f"stderr:\n{result.stderr}"
    )


def test_agent_subprocess_per_agent_workspace_is_created(
    tmp_path: Path,
    mock_inference_url: str,
) -> None:
    """End-to-end confirms the sub-agent scopes its workspace per run.

    The in-process test in ``tests/test_coding_agent_main.py`` asserts
    this at function level; the subprocess version verifies the same
    invariant survives through the full startup path.  Two child
    processes with the same ``CODING_WORKSPACE_ROOT`` must produce
    two distinct ``agent-<8 hex>`` subdirectories.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = _clean_subprocess_env(mock_inference_url, workspace, home)

    for _ in range(2):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "nemoclaw_escapades.agent",
                "--task",
                "say hi",
            ],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (
            f"agent exited with code {result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}\n"
        )

    subdirs = sorted(p for p in workspace.iterdir() if p.is_dir())
    assert len(subdirs) == 2, f"expected 2 per-agent dirs; got {subdirs}"
    for p in subdirs:
        assert p.name.startswith("agent-"), p.name
        assert len(p.name) == len("agent-") + 8, p.name
    assert subdirs[0].name != subdirs[1].name
