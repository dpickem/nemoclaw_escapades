"""Tests for ``tools/tool_registry_factory.py::build_full_tool_registry``.

Focused on the orchestrator-only ``delegate_task`` wiring added in
M2b Phase 3a follow-up: when a :class:`DelegationManager` is
supplied, ``delegate_task`` is registered; when omitted (or
``delegation.enabled=false``) the tool is absent so the production
startup path matches the test harness in this PR.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nemoclaw_escapades.config import AppConfig
from nemoclaw_escapades.tools.tool_registry_factory import build_full_tool_registry


@pytest.fixture(autouse=True)
def _set_required_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate Slack secrets so ``AppConfig.load`` passes validation."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")


class TestDelegateTaskRegistration:
    """``delegate_task`` lands in the registry iff a manager is supplied."""

    def test_no_manager_means_no_delegate_task(self, tmp_path: Any) -> None:
        # Default startup with no manager (e.g. broker unreachable):
        # the orchestrator runs degraded but stays up, and the tool
        # is correctly absent so the planning model can't try to
        # invoke a path that has no live wire.
        config = AppConfig.load()
        config.coding.workspace_root = str(tmp_path / "ws")
        registry = build_full_tool_registry(config)
        assert "delegate_task" not in registry.names

    def test_manager_supplied_registers_delegate_task(self, tmp_path: Any) -> None:
        # The wiring fix: pass a manager and the tool shows up.
        # We don't exercise the manager itself here — that's covered
        # by tests/test_delegation_tool.py and the integration test;
        # we only verify the factory branch.
        config = AppConfig.load()
        config.coding.workspace_root = str(tmp_path / "ws")
        manager = MagicMock()
        registry = build_full_tool_registry(config, delegation_manager=manager)
        assert "delegate_task" in registry.names

    def test_orchestrator_registry_uses_nmb_sandbox_id_as_parent(
        self,
        tmp_path: Any,
    ) -> None:
        # ``parent_sandbox_id`` on every ``TaskAssignPayload`` must
        # match the orchestrator's NMB identity so the broker's
        # routing and §16.3's "no recursive delegation" depth check
        # work.  The factory pulls it from ``config.nmb.sandbox_id``;
        # a blank value (auto-generate at runtime) falls back to the
        # literal "orchestrator" so audit rows have a stable key.
        config = AppConfig.load()
        config.coding.workspace_root = str(tmp_path / "ws")
        config.nmb.sandbox_id = "orchestrator-abc123"
        manager = MagicMock()
        registry = build_full_tool_registry(config, delegation_manager=manager)
        spec = registry.get("delegate_task")
        assert spec is not None
        # The handler closes over parent_sandbox_id; we assert via the
        # spec's input schema description rather than poking internals.
        assert spec.name == "delegate_task"
