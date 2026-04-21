"""Integration tests for the coding sub-agent entrypoint.

Exercises the §14 Phase-1 criterion:

    *Coding agent process starts, [receives task,] runs the M2a
    AgentLoop with the coding tool suite and the SkillLoader-discovered
    skill tool, sends [result].*

The NMB-wired path is a Phase 2 follow-up.  The CLI path (``--task``)
already exercises the same stack — config load, runtime self-check,
AgentLoop assembly, coding tools, final text — so the test runs that
path with a mock inference backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nemoclaw_escapades.agent import __main__ as agent_main
from nemoclaw_escapades.agent.types import AgentSetupBundle


# ── Helpers ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip env vars that would leak between tests."""
    for key in (
        "OPENSHELL_SANDBOX",
        "NEMOCLAW_CONFIG_PATH",
        "NMB_URL",
        "AGENT_SANDBOX_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    # Populate required secrets so ``AppConfig.load`` passes validation.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("INFERENCE_HUB_API_KEY", "test-key")
    monkeypatch.setenv("INFERENCE_HUB_BASE_URL", "http://test")


# ── Arg parsing ─────────────────────────────────────────────────────


class TestArgParsing:
    """``_parse_args`` enforces the mutually-exclusive run modes."""

    def test_task_mode_parses(self) -> None:
        args = agent_main._parse_args(
            ["--task", "fix the bug", "--workspace", "/tmp/ws"]
        )
        assert args.task == "fix the bug"
        assert args.workspace == "/tmp/ws"
        assert args.nmb is False

    def test_nmb_mode_parses(self) -> None:
        args = agent_main._parse_args(["--nmb"])
        assert args.nmb is True
        assert args.task is None

    def test_no_mode_raises_systemexit(self) -> None:
        with pytest.raises(SystemExit):
            agent_main._parse_args([])

    def test_both_modes_raises_systemexit(self) -> None:
        with pytest.raises(SystemExit):
            agent_main._parse_args(["--task", "x", "--nmb"])


# ── End-to-end CLI mode ─────────────────────────────────────────────


class TestCliMode:
    """Run the CLI path end-to-end with a mock backend."""

    @pytest.mark.asyncio
    async def test_task_runs_and_returns_content(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Disable audit to avoid SQLite path setup inside the test
        # process.  The CLI mode still exercises everything above.
        monkeypatch.setenv("AUDIT_ENABLED", "false")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        async def _fake_run_cli(
            task_description: str,
            workspace_root: str | None,
            config: Any,
            backend: Any,
            audit: Any,
            logger: Any,
        ) -> int:
            # We patch *_run_cli_mode* itself so the test stays in
            # process-assembly territory and doesn't exercise the
            # real inference backend (which would open a socket).
            # The assertion below checks the entrypoint reached this
            # point with a sensible bundle shape.
            assert task_description == "print hello"
            assert workspace_root == str(workspace)
            assert config.coding.enabled is True
            print("done")
            return 0

        monkeypatch.setattr(agent_main, "_run_cli_mode", _fake_run_cli)

        rc = await agent_main._async_main(
            ["--task", "print hello", "--workspace", str(workspace)],
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "done" in captured.out

    @pytest.mark.asyncio
    async def test_real_cli_mode_assembles_loop_and_runs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_run_cli_mode`` wires an AgentLoop and returns its content.

        Patches the ``AgentLoop`` class so the test doesn't hit a real
        backend, but otherwise exercises the full assembly path —
        ``_build_tool_registry``, ``_load_coding_prompt``,
        ``LayeredPromptBuilder`` construction.
        """
        captured: dict[str, Any] = {}

        class _FakeAgentLoop:
            def __init__(self, *, backend: Any, tools: Any, **_: Any) -> None:
                captured["tool_names"] = tools.names
                captured["backend"] = backend

            async def run(self, *, messages: list[Any], request_id: str) -> Any:
                captured["system_prompt"] = messages[0]["content"]
                captured["user_prompt"] = messages[-1]["content"]
                captured["request_id"] = request_id
                return type(
                    "R",
                    (),
                    {
                        "content": "task done",
                        "rounds": 1,
                        "tool_calls_made": 0,
                        "hit_safety_limit": False,
                    },
                )()

        monkeypatch.setattr(agent_main, "AgentLoop", _FakeAgentLoop)

        class _FakeBackend:
            async def close(self) -> None:
                pass

        workspace = tmp_path / "ws"
        # Build a minimal config with the workspace pointing at tmp.
        monkeypatch.setenv("CODING_WORKSPACE_ROOT", str(workspace))
        from nemoclaw_escapades.config import AppConfig

        config = AppConfig.load()

        import logging

        logger = logging.getLogger("test")
        rc = await agent_main._run_cli_mode(
            task_description="write README",
            workspace_root=str(workspace),
            config=config,
            backend=_FakeBackend(),
            audit=None,
            logger=logger,
        )
        assert rc == 0
        # The sub-agent got the coding tool suite.
        assert "read_file" in captured["tool_names"]
        assert "write_file" in captured["tool_names"]
        assert "bash" in captured["tool_names"]
        # The prompt carries the task description and workspace.
        assert "write README" in captured["user_prompt"]
        assert str(workspace) in captured["system_prompt"]


# ── AgentSetupBundle round-trip ─────────────────────────────────────


class TestAgentSetupBundleSerde:
    """Serialise / deserialise the NMB payload shape."""

    def test_to_dict_round_trips(self) -> None:
        bundle = AgentSetupBundle(
            task_id="t1",
            agent_id="a1",
            parent_agent_id="orchestrator",
            task_description="fix the bug",
            workspace_root="/sandbox/workspace",
        )
        payload = bundle.to_dict()
        restored = AgentSetupBundle.from_dict(payload)
        assert restored == bundle

    def test_default_source_type_is_agent(self) -> None:
        bundle = AgentSetupBundle(
            task_id="t1",
            agent_id="a1",
            parent_agent_id="orchestrator",
            task_description="fix the bug",
            workspace_root="/sandbox/workspace",
        )
        assert bundle.source_type == "agent"

    def test_from_dict_missing_field_raises(self) -> None:
        with pytest.raises(KeyError):
            AgentSetupBundle.from_dict({"task_id": "t1"})
