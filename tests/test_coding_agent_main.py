"""Integration tests for the coding sub-agent entrypoint.

Exercises the §14 Phase-1 criterion:

    *Coding agent process starts, [receives task,] runs the M2a
    AgentLoop with the coding tool suite and the SkillLoader-discovered
    skill tool, sends [result].*

The full Phase-2 receive-loop body is deferred, but the Phase-1
CLI path (``--task``) and the NMB-mode *wiring* (connect, read
config, close on shutdown) are both covered here with mock
inference and a fake ``MessageBus``.
"""

from __future__ import annotations

import asyncio
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
    # ``inference.base_url`` flows through the default YAML overlay —
    # no env-var hook needed here (or anywhere in the runtime path).
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("INFERENCE_HUB_API_KEY", "test-key")


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
        workspace = tmp_path / "ws"
        workspace.mkdir()

        # ``_async_main`` runs ``detect_runtime_environment`` and
        # refuses to start outside a healthy sandbox.  Mock the
        # detector to return a ``SANDBOX`` report so the entrypoint
        # proceeds to config load + dispatch.
        from nemoclaw_escapades.runtime import RuntimeEnvironment, RuntimeReport

        def _fake_detect() -> RuntimeReport:
            return RuntimeReport(
                classification=RuntimeEnvironment.SANDBOX,
                signals_present=(
                    "OPENSHELL_SANDBOX",
                    "sandbox_dir_writable",
                    "app_src_present",
                    "https_proxy_env",
                ),
                signals_missing=(),
            )

        monkeypatch.setattr(agent_main, "detect_runtime_environment", _fake_detect)

        async def _fake_run_cli(
            task_description: str,
            workspace_root: str | None,
            config: Any,
            backend: Any,
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
            def __init__(self, *, backend: Any, tools: Any, audit: Any = None, **_: Any) -> None:
                captured["tool_names"] = tools.names
                captured["backend"] = backend
                captured["audit"] = audit

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
        # ``_run_cli_mode`` below receives the workspace explicitly via
        # ``workspace_root=``, so the config just needs to load — no
        # knob-tweaking required.
        from nemoclaw_escapades.config import AppConfig

        config = AppConfig.load()

        import logging

        logger = logging.getLogger("test")
        rc = await agent_main._run_cli_mode(
            task_description="write README",
            workspace_root=str(workspace),
            config=config,
            backend=_FakeBackend(),
            logger=logger,
        )
        assert rc == 0
        # The sub-agent got the coding tool suite.
        assert "read_file" in captured["tool_names"]
        assert "write_file" in captured["tool_names"]
        assert "bash" in captured["tool_names"]
        # Regression: ``skill`` is registered when skills are enabled
        # so the system prompt's ``skill("scratchpad")`` instruction
        # resolves to a real tool.  The repo ships skills in the
        # default ``skills/`` directory that ``SkillLoader`` picks
        # up unchanged by the test.
        assert "skill" in captured["tool_names"]
        # The prompt carries the task description and workspace.
        assert "write README" in captured["user_prompt"]
        assert str(workspace) in captured["system_prompt"]
        # Regression: the sub-agent does NOT open its own AuditDB —
        # Phase 2's AuditBuffer will flush over NMB to the orchestrator's
        # single audit DB.  Assert the fake loop received ``audit=None``.
        assert captured["audit"] is None
        # Regression: CLI mode must scope each run to a per-agent
        # subdirectory so two concurrent invocations can't clobber
        # each other's scratchpad / notes files.
        import re

        assert re.search(
            rf"Workspace root: {re.escape(str(workspace))}/agent-[0-9a-f]{{8}}\b",
            captured["system_prompt"],
        ), (
            "expected per-agent subdirectory of shape "
            "``<workspace>/agent-<agent_id>`` in the system prompt, got: "
            f"{captured['system_prompt']!r}"
        )

    @pytest.mark.asyncio
    async def test_skill_tool_omitted_when_skills_disabled(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``config.skills.enabled=False`` leaves ``skill`` unregistered.

        Opt-out escape hatch for callers / tests that want a pure
        coding surface.  Without this test, a future refactor that
        unconditionally wires the ``skill`` tool (say, for
        "consistency") would break the opt-out silently.
        """
        captured: dict[str, Any] = {}

        class _FakeAgentLoop:
            def __init__(self, *, tools: Any, **_: Any) -> None:
                captured["tool_names"] = tools.names

            async def run(self, *, messages: list[Any], request_id: str) -> Any:
                return type(
                    "R",
                    (),
                    {
                        "content": "ok",
                        "rounds": 1,
                        "tool_calls_made": 0,
                        "hit_safety_limit": False,
                    },
                )()

        monkeypatch.setattr(agent_main, "AgentLoop", _FakeAgentLoop)

        class _FakeBackend:
            async def close(self) -> None:
                pass

        from nemoclaw_escapades.config import AppConfig

        workspace = tmp_path / "ws"
        # ``skills.enabled=false`` now flows through YAML only (env-var
        # overrides for non-secret knobs were retired in the config
        # SSOT refactor — see GAPS §16).
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text("skills:\n  enabled: false\n")
        config = AppConfig.load(path=yaml_path)
        assert config.skills.enabled is False

        import logging

        await agent_main._run_cli_mode(
            task_description="noop",
            workspace_root=str(workspace),
            config=config,
            backend=_FakeBackend(),
            logger=logging.getLogger("test"),
        )
        # Coding tools still there — skills disabled is additive-only.
        assert "read_file" in captured["tool_names"]
        assert "bash" in captured["tool_names"]
        # But skill tool is absent — the opt-out worked.
        assert "skill" not in captured["tool_names"]

    @pytest.mark.asyncio
    async def test_cli_mode_per_agent_subdirectory_is_created(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two sub-agent invocations land in distinct workspace subdirs.

        Enforces the §4.2 isolation invariant at the filesystem
        level: even when operators run two CLI invocations with the
        same ``--workspace`` base path (or both default to
        ``config.coding.workspace_root``), the concrete tool-root
        directories must diverge so scratchpad notes / edits don't
        clobber each other.
        """
        created_dirs: list[Path] = []

        class _FakeAgentLoop:
            def __init__(self, *, tools: Any, **_: Any) -> None:
                # ``tools`` is the real ``ToolRegistry`` — the file
                # tools' workspace root lives behind it.  We don't
                # need to introspect; the assertion is that the
                # workspace mkdir inside ``_build_tool_registry``
                # actually hit a unique path.  Just record that
                # we got called.
                created_dirs.append(Path(tools.names.__self__.__class__.__name__))

            async def run(self, *, messages: list[Any], request_id: str) -> Any:
                return type(
                    "R",
                    (),
                    {
                        "content": "ok",
                        "rounds": 1,
                        "tool_calls_made": 0,
                        "hit_safety_limit": False,
                    },
                )()

        monkeypatch.setattr(agent_main, "AgentLoop", _FakeAgentLoop)

        class _FakeBackend:
            async def close(self) -> None:
                pass

        from nemoclaw_escapades.config import AppConfig

        base = tmp_path / "ws"
        config = AppConfig.load()

        import logging

        logger = logging.getLogger("test")
        # First invocation.
        await agent_main._run_cli_mode(
            task_description="task one",
            workspace_root=str(base),
            config=config,
            backend=_FakeBackend(),
            logger=logger,
        )
        # Second invocation — same base, expect a different subdir.
        await agent_main._run_cli_mode(
            task_description="task two",
            workspace_root=str(base),
            config=config,
            backend=_FakeBackend(),
            logger=logger,
        )
        # Exactly two ``agent-<hex>`` subdirectories landed on disk.
        subdirs = sorted(p for p in base.iterdir() if p.is_dir())
        assert len(subdirs) == 2
        for p in subdirs:
            assert p.name.startswith("agent-")
            assert len(p.name) == len("agent-") + 8  # _make_agent_id is 8 hex
        # Distinct agent ids.
        assert subdirs[0].name != subdirs[1].name

    @pytest.mark.asyncio
    async def test_run_task_never_passes_audit_even_when_config_enables_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: ``_run_task`` constructs ``AgentLoop`` with ``audit=None``.

        Even with ``config.audit.enabled=True`` at the process level,
        the sub-agent must not open its own DB.  The single-authoritative-
        audit-DB invariant is enforced by the orchestrator; sub-agents
        buffer and flush (Phase 2).  Guards against a future edit that
        reintroduces ``AuditDB`` here.
        """
        captured: dict[str, Any] = {}

        class _FakeAgentLoop:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def run(self, *, messages: list[Any], request_id: str) -> Any:
                return type(
                    "R",
                    (),
                    {
                        "content": "ok",
                        "rounds": 1,
                        "tool_calls_made": 0,
                        "hit_safety_limit": False,
                    },
                )()

        monkeypatch.setattr(agent_main, "AgentLoop", _FakeAgentLoop)

        class _FakeBackend:
            async def close(self) -> None:
                pass

        from nemoclaw_escapades.config import AppConfig

        # ``audit.enabled`` defaults to ``True``.  The regression this
        # test guards is that the sub-agent still passes ``audit=None``
        # to AgentLoop — it relies on the orchestrator to own the
        # single authoritative DB.
        config = AppConfig.load()
        import logging

        workspace = tmp_path / "ws"
        workspace.mkdir()
        rc = await agent_main._run_cli_mode(
            task_description="noop",
            workspace_root=str(workspace),
            config=config,
            backend=_FakeBackend(),
            logger=logging.getLogger("test"),
        )
        assert rc == 0
        assert captured["audit"] is None


# ── NMB mode smoke test ─────────────────────────────────────────────


class TestNmbMode:
    """``--nmb`` mode wiring: connect, idle until shutdown, close.

    Phase 1 ships the entrypoint skeleton (the receive-loop body is
    a Phase 2 TODO).  These tests cover the wiring itself — reading
    ``config.nmb`` values (not env), constructing ``MessageBus``
    with the right args, calling ``connect_with_retry``, and
    tearing down on shutdown.  Exercises the plumbing without
    spinning up a real broker.
    """

    @pytest.mark.asyncio
    async def test_nmb_mode_reads_broker_url_from_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The broker URL and sandbox ID come from ``config.nmb`` (not env)."""
        captured: dict[str, Any] = {}

        class _FakeBus:
            def __init__(self, *, broker_url: str, sandbox_id: str) -> None:
                captured["broker_url"] = broker_url
                captured["sandbox_id"] = sandbox_id

            async def connect_with_retry(self) -> None:
                captured["connected"] = True

            async def close(self) -> None:
                captured["closed"] = True

        # Shadow the lazy ``from nemoclaw_escapades.nmb.client import
        # MessageBus`` done inside ``_run_nmb_mode`` with our fake.
        import nemoclaw_escapades.nmb.client as nmb_client_mod

        monkeypatch.setattr(nmb_client_mod, "MessageBus", _FakeBus)

        from nemoclaw_escapades.config import AppConfig

        config = AppConfig()
        config.nmb.broker_url = "ws://test-broker:1234"
        config.nmb.sandbox_id = "pinned-sub-agent-id"

        class _FakeBackend:
            async def close(self) -> None:
                pass

        import logging

        shutdown_event = asyncio.Event()
        # Schedule shutdown immediately so the idle loop exits.
        shutdown_event.set()
        rc = await agent_main._run_nmb_mode(
            config=config,
            backend=_FakeBackend(),
            logger=logging.getLogger("test"),
            shutdown_event=shutdown_event,
        )
        assert rc == 0
        assert captured["broker_url"] == "ws://test-broker:1234"
        # Non-empty sandbox_id → used as-is (no ``coding-…`` prefix).
        assert captured["sandbox_id"] == "pinned-sub-agent-id"
        assert captured["connected"] is True
        assert captured["closed"] is True

    @pytest.mark.asyncio
    async def test_nmb_mode_generates_sandbox_id_when_config_blank(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty ``config.nmb.sandbox_id`` → agent generates ``coding-<hex>``."""
        captured: dict[str, Any] = {}

        class _FakeBus:
            def __init__(self, *, broker_url: str, sandbox_id: str) -> None:
                captured["sandbox_id"] = sandbox_id

            async def connect_with_retry(self) -> None:
                pass

            async def close(self) -> None:
                pass

        import nemoclaw_escapades.nmb.client as nmb_client_mod

        monkeypatch.setattr(nmb_client_mod, "MessageBus", _FakeBus)

        from nemoclaw_escapades.config import AppConfig

        config = AppConfig()
        config.nmb.broker_url = "ws://test:1"
        config.nmb.sandbox_id = ""  # explicit — agent should synthesise.

        class _FakeBackend:
            async def close(self) -> None:
                pass

        import logging

        shutdown_event = asyncio.Event()
        shutdown_event.set()
        await agent_main._run_nmb_mode(
            config=config,
            backend=_FakeBackend(),
            logger=logging.getLogger("test"),
            shutdown_event=shutdown_event,
        )
        assert captured["sandbox_id"].startswith("coding-")
        # ``_make_agent_id`` truncates to 8 hex chars.
        assert len(captured["sandbox_id"]) == len("coding-") + 8


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
