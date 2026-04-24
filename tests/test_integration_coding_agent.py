"""Subprocess-level integration tests for the coding sub-agent.

Exercises Phase-1 integration rows from ``docs/design_m2b.md`` §16.2
by spawning ``python -m nemoclaw_escapades.agent`` as a child
process against a locally-hosted mock inference endpoint:

- *Coding agent end-to-end* — agent receives task, uses file tools,
  returns a diff-flavoured summary.  Exercises the
  tool_call → tool_result → final inference cycle.
- *Config YAML — deployment override* — custom ``config.yaml`` via
  ``NEMOCLAW_CONFIG_PATH`` makes ``coding.workspace_root`` point
  somewhere non-default without a rebuild.
- *Sandbox boot — broken env* — runtime signal mix that
  ``detect_runtime_environment`` classifies ``INCONSISTENT`` causes
  the sub-agent to fail fast with ``SandboxConfigurationError``
  before it ever touches the inference backend.

The in-process function-level tests live in
``tests/test_coding_agent_main.py``; these subprocess-level tests
complement them by exercising the real startup discipline (runtime
self-check → config load → logging → ``InferenceHubBackend`` →
``AgentLoop``) in a fresh interpreter with a minimal environment.
The inference endpoint is a stdlib ``http.server`` mock on a
kernel-picked free port, so the tests are hermetic — no external
network, no provider dependency.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_CANNED_REPLY = "hello from the mock inference server"


# ── Mock inference server ───────────────────────────────────────────


def _stop_response(content: str, model: str = "mock-model") -> dict[str, object]:
    """Build an OpenAI-format terminating response.

    Emitted by the mock as the final round of a conversation — the
    ``AgentLoop`` sees ``finish_reason="stop"`` and exits.
    """
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "model": model,
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }


def _tool_call_response(
    tool_name: str,
    arguments: dict[str, object],
    call_id: str = "call_1",
    model: str = "mock-model",
) -> dict[str, object]:
    """Build an OpenAI-format tool-call response.

    The ``AgentLoop`` will parse the ``tool_calls`` array, dispatch
    each one via the ``ToolRegistry``, and issue a follow-up inference
    request with the results appended to the message history.
    """
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "model": model,
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }


class _ScriptedMockServer(HTTPServer):
    """HTTP server that replays a pre-canned sequence of responses.

    One response per ``POST``; the last entry is reused indefinitely
    so a runaway loop sees a terminating ``stop`` response rather
    than a 500 / index-out-of-range.  Access is lock-protected
    because ``serve_forever`` hands off requests to a handler thread.
    """

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        responses: list[dict[str, object]],
    ) -> None:
        super().__init__(server_address, handler_class)
        self.responses = list(responses)
        self.request_index = 0
        self._lock = threading.Lock()

    def next_response(self) -> dict[str, object]:
        """Pop the next scripted response (clamped to the last entry)."""
        with self._lock:
            idx = min(self.request_index, len(self.responses) - 1)
            self.request_index += 1
            return self.responses[idx]


class _ScriptedMockHandler(BaseHTTPRequestHandler):
    """POST /chat/completions — echo the next scripted response."""

    def do_POST(self) -> None:  # noqa: N802 - stdlib method signature
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            # Drain the request body.  ``tests/test_backend_inference_hub.py``
            # already covers request-shape assertions — here we only care
            # that the backend reached us.
            _ = self.rfile.read(length)
        assert isinstance(self.server, _ScriptedMockServer)
        body = json.dumps(self.server.next_response()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silence the default stderr access log — clutter without
        # diagnostic value in test output.
        return


def _start_mock_server(
    responses: list[dict[str, object]],
) -> tuple[_ScriptedMockServer, threading.Thread]:
    """Bind the mock to a kernel-picked free port and serve in a thread."""
    server = _ScriptedMockServer(("127.0.0.1", 0), _ScriptedMockHandler, responses=responses)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def mock_inference_url() -> Iterator[str]:
    """Start a single-response mock and yield its base URL.

    Back-compat fixture for the happy-path test.  Tests that need
    multi-round scripted responses build their own via
    :func:`_start_mock_server`.
    """
    server, _ = _start_mock_server([_stop_response(_CANNED_REPLY)])
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


# ── Subprocess env helpers ──────────────────────────────────────────


def _repo_root() -> Path:
    """Project root, inferred from this file's path."""
    return Path(__file__).resolve().parent.parent


def _write_subprocess_yaml(
    yaml_path: Path,
    *,
    mock_inference_url: str,
    workspace: Path,
    skills_dir: Path,
) -> None:
    """Write a YAML config file for the subprocess's ``NEMOCLAW_CONFIG_PATH``.

    After the M2b P1 config-SSOT refactor (GAPS §16), non-secret
    knobs flow through YAML only.  The helper writes everything the
    sub-agent needs into a single file that the subprocess loads via
    ``NEMOCLAW_CONFIG_PATH``.
    """
    yaml_path.write_text(
        "inference:\n"
        f"  base_url: {mock_inference_url}\n"
        "  timeout_s: 5\n"
        "  max_retries: 1\n"
        "coding:\n"
        f"  workspace_root: {workspace}\n"
        "skills:\n"
        f"  skills_dir: {skills_dir}\n"
    )


def _clean_subprocess_env(
    yaml_path: Path,
    home: Path,
) -> dict[str, str]:
    """Build a minimal env dict for the sub-agent subprocess.

    Replaces the parent's environment entirely so stray vars from the
    developer's shell / ``.env`` don't leak into the test.  Slack
    tokens are deliberately *absent* — the sub-agent runs without
    Slack (``create_coding_agent_config`` / ``require_slack=False``),
    and omitting them here doubles as a regression check that the
    sub-agent never starts depending on them.

    Sandbox signals: the sub-agent's runtime self-check refuses to
    start with ``INCONSISTENT`` classification (zero signals on a
    bare dev laptop).  On the host we can reliably set only the
    three env-based signals (``OPENSHELL_SANDBOX``, ``HTTPS_PROXY``,
    ``SSL_CERT_FILE``) — the path checks (``/sandbox``, ``/app/src``)
    need root and the DNS check needs ``inference.local`` to resolve.
    We lower the signal threshold to ``3`` via
    ``NEMOCLAW_SANDBOX_SIGNAL_THRESHOLD`` so the classifier still
    runs against real signals; prod behaviour is unaffected (the env
    var is only set here).  ``SSL_CERT_FILE`` points at the Python
    runtime's default CA bundle so httpx's client init (which parses
    the file at constructor time) doesn't blow up.

    Non-secret config flows through the YAML at *yaml_path* via
    ``NEMOCLAW_CONFIG_PATH``; env vars here hold secrets and
    sandbox-detection signals only.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(_repo_root() / "src"),
        "NEMOCLAW_CONFIG_PATH": str(yaml_path),
        # Sandbox signals — see docstring.
        "NEMOCLAW_SANDBOX_SIGNAL_THRESHOLD": "3",
        "OPENSHELL_SANDBOX": "1",
        "HTTPS_PROXY": "http://openshell-proxy.invalid:3128",
        "SSL_CERT_FILE": ssl.get_default_verify_paths().cafile,
    }


def _subprocess_env_with_defaults(
    tmp_path: Path,
    mock_inference_url: str,
    workspace: Path,
    home: Path,
) -> dict[str, str]:
    """Write a default YAML under *tmp_path* and return the subprocess env.

    Convenience wrapper used by most tests in this file — they all
    want the same shape (mock inference endpoint + per-test workspace
    + repo skills dir) so they share this helper.
    """
    yaml_path = tmp_path / "subprocess.yaml"
    _write_subprocess_yaml(
        yaml_path,
        mock_inference_url=mock_inference_url,
        workspace=workspace,
        skills_dir=_repo_root() / "skills",
    )
    return _clean_subprocess_env(yaml_path, home)


def _run_agent(
    env: dict[str, str],
    cwd: Path,
    task: str = "say hi",
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    """Spawn ``python -m nemoclaw_escapades.agent --task ...`` with *env*.

    Captures stdout + stderr and swallows the subprocess's own
    non-zero exit instead of raising, so the caller can assert on
    ``returncode`` directly.
    """
    return subprocess.run(
        [sys.executable, "-m", "nemoclaw_escapades.agent", "--task", task],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ── Coding agent happy path ─────────────────────────────────────────


def test_agent_subprocess_runs_task_end_to_end(
    tmp_path: Path,
    mock_inference_url: str,
) -> None:
    """``python -m nemoclaw_escapades.agent --task ...`` prints the reply.

    Phase-1 exit criterion from design_m2b.md §14: *"Coding agent
    process starts, assembles the AgentLoop with the coding tool
    suite + skill loader, runs a task."*  Covers the full startup
    discipline (runtime self-check → config → logging → backend →
    loop → stdout) in a real child process.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    env = _subprocess_env_with_defaults(tmp_path, mock_inference_url, workspace, home)
    result = _run_agent(env, cwd=tmp_path)

    assert result.returncode == 0, (
        f"agent exited with code {result.returncode}\n"
        f"---- stdout ----\n{result.stdout}\n"
        f"---- stderr ----\n{result.stderr}\n"
    )
    assert _CANNED_REPLY in result.stdout, (
        f"expected canned reply in stdout; got:\n{result.stdout!r}\nstderr:\n{result.stderr}"
    )


def test_agent_subprocess_per_agent_workspace_is_created(
    tmp_path: Path,
    mock_inference_url: str,
) -> None:
    """End-to-end confirms the sub-agent scopes its workspace per run.

    The in-process test in ``tests/test_coding_agent_main.py`` asserts
    this at function level; the subprocess version verifies the same
    invariant survives through the full startup path.  Two child
    processes with the same ``coding.workspace_root`` must produce
    two distinct ``agent-<8 hex>`` subdirectories.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = _subprocess_env_with_defaults(tmp_path, mock_inference_url, workspace, home)

    for _ in range(2):
        result = _run_agent(env, cwd=tmp_path)
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


# ── Coding agent with file-tool execution ──────────────────────────


def test_agent_subprocess_executes_file_tool_call(tmp_path: Path) -> None:
    """Agent receives task, uses file tools, returns a result.

    Covers the §16.2 *"Coding agent end-to-end"* row: mock returns a
    ``write_file`` tool call on the first inference round, the
    ``AgentLoop`` executes it against the real file tool (which
    creates the file under the sub-agent's workspace subdir), appends
    the tool result to the message history, and makes a second
    inference call.  The mock's second response is a terminating
    summary which goes to stdout.

    End-to-end assertions:

    - subprocess exits 0,
    - the file actually landed on disk inside the per-agent workspace,
    - the assistant's final summary reaches stdout.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    file_name = "hello.txt"
    file_content = "hi there"
    final_reply = "Wrote hello.txt; content is 'hi there'."

    server, _ = _start_mock_server(
        [
            _tool_call_response(
                "write_file",
                {"path": file_name, "content": file_content},
            ),
            _stop_response(final_reply),
        ]
    )
    try:
        host, port = server.server_address
        mock_url = f"http://{host}:{port}"
        env = _subprocess_env_with_defaults(tmp_path, mock_url, workspace, home)
        result = _run_agent(env, cwd=tmp_path)
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, (
        f"agent exited with code {result.returncode}\n"
        f"---- stdout ----\n{result.stdout}\n"
        f"---- stderr ----\n{result.stderr}\n"
    )
    # The mock saw exactly two inference rounds: tool_call + stop.
    # ``request_index`` is incremented past the end so we expect >=2.
    assert server.request_index >= 2, (
        f"expected the mock to see at least 2 rounds, saw {server.request_index}"
    )
    # The file actually got written to the per-agent workspace subdir
    # (``<workspace>/agent-<hex>/hello.txt``) — proves the tool_call
    # was dispatched through the real ``ToolRegistry`` and the file
    # tool executed against the scoped workspace root.
    agent_dirs = [p for p in workspace.iterdir() if p.is_dir() and p.name.startswith("agent-")]
    assert len(agent_dirs) == 1, f"expected one per-agent workspace; got {agent_dirs}"
    written = agent_dirs[0] / file_name
    assert written.is_file(), f"{written} not written.  stderr:\n{result.stderr}"
    assert written.read_text() == file_content
    # Final reply reaches stdout.
    assert final_reply in result.stdout, (
        f"expected final reply in stdout; got:\n{result.stdout!r}\nstderr:\n{result.stderr}"
    )


# ── Config YAML deployment override ────────────────────────────────


def test_agent_subprocess_honours_yaml_deployment_override(
    tmp_path: Path,
    mock_inference_url: str,
) -> None:
    """Custom ``config.yaml`` via ``NEMOCLAW_CONFIG_PATH`` is respected.

    Covers the §16.2 *"Config YAML — deployment override"* row:
    point ``NEMOCLAW_CONFIG_PATH`` at a fresh YAML that directs
    ``coding.workspace_root`` somewhere non-default and confirm the
    sub-agent lands in that path without an image rebuild or code
    change.  With env-var overrides for non-secret knobs removed
    (GAPS §16), the YAML is the single source of truth.
    """
    yaml_ws = tmp_path / "yaml_ws"
    home = tmp_path / "home"
    home.mkdir()

    yaml_path = tmp_path / "custom.yaml"
    yaml_path.write_text(
        "inference:\n"
        f"  base_url: {mock_inference_url}\n"
        "  timeout_s: 5\n"
        "  max_retries: 1\n"
        "coding:\n"
        f"  workspace_root: {yaml_ws}\n"
        "skills:\n"
        f"  skills_dir: {_repo_root() / 'skills'}\n"
    )

    env = _clean_subprocess_env(yaml_path, home)

    result = _run_agent(env, cwd=tmp_path)

    assert result.returncode == 0, (
        f"agent exited with code {result.returncode}\n"
        f"---- stdout ----\n{result.stdout}\n"
        f"---- stderr ----\n{result.stderr}\n"
    )
    # The per-agent workspace subdir landed under the YAML-supplied
    # root, not under the ``CODING_WORKSPACE_ROOT`` env (which we
    # removed) and not under the dataclass default.
    assert yaml_ws.is_dir(), (
        f"expected YAML-supplied workspace {yaml_ws} to be created.\nstderr:\n{result.stderr}"
    )
    agent_dirs = [p for p in yaml_ws.iterdir() if p.is_dir()]
    assert any(p.name.startswith("agent-") for p in agent_dirs), (
        f"no agent-<hex> subdir landed under YAML workspace; got {agent_dirs}"
    )


# ── Sandbox boot — broken env ───────────────────────────────────────


def test_agent_subprocess_inconsistent_runtime_fails_fast(
    tmp_path: Path,
) -> None:
    """Broken sandbox-env signal mix → fail-fast with structured error.

    Covers the §16.2 *"Sandbox boot — broken env"* row: an env mix
    that the multi-signal detector classifies ``INCONSISTENT``
    (``OPENSHELL_SANDBOX`` set without the matching path / DNS
    signals) must stop the sub-agent at the runtime self-check,
    before ``AppConfig.load`` and before any I/O.

    The subprocess should:

    - exit non-zero,
    - surface ``SandboxConfigurationError`` (with its structured
      diagnostic) on stderr,
    - *not* attempt any inference call (no outbound HTTP — we
      deliberately don't even start a mock).
    """
    # Minimal env — we never reach config load, so no secrets or
    # workspace needed.  Two signals (OPENSHELL_SANDBOX + HTTPS_PROXY)
    # is below the 4-of-6 threshold for SANDBOX, so the classifier
    # returns INCONSISTENT.
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(_repo_root() / "src"),
        "OPENSHELL_SANDBOX": "1",
        "HTTPS_PROXY": "http://nope.invalid:3128",
    }

    result = _run_agent(env, cwd=tmp_path, task="should-not-run")

    assert result.returncode != 0, (
        "expected non-zero exit on INCONSISTENT runtime; got 0.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The raised exception reaches stderr via ``asyncio.run``'s
    # default traceback handler.
    assert "SandboxConfigurationError" in result.stderr, (
        f"expected SandboxConfigurationError on stderr; got:\n{result.stderr}"
    )
    # The structured diagnostic ("refusing to start" is part of
    # ``SandboxConfigurationError``'s message) is present so operators
    # get actionable feedback in the logs.
    assert "refusing to start" in result.stderr, (
        f"expected structured diagnostic; stderr:\n{result.stderr}"
    )
