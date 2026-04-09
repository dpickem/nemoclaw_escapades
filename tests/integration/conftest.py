"""Shared fixtures for NMB multi-sandbox integration tests."""

from __future__ import annotations

import pytest

from nemoclaw_escapades.nmb.testing import IntegrationHarness, SandboxPolicy


# ---------------------------------------------------------------------------
# Base harness (tests call start() with their own policies)
# ---------------------------------------------------------------------------


@pytest.fixture
async def harness() -> IntegrationHarness:
    """Bare harness — call ``start(policies)`` inside the test."""
    h = IntegrationHarness()
    yield h  # type: ignore[misc]
    await h.stop()


# ---------------------------------------------------------------------------
# Pre-configured topologies
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_sandbox_harness(
    harness: IntegrationHarness,
) -> IntegrationHarness:
    """Orchestrator + coding-1 with bidirectional send policies.

    ::

        orchestrator ←→ coding-1   (send/request/stream)
        both can subscribe to progress.* and system channels
    """
    await harness.start(
        [
            SandboxPolicy(
                sandbox_id="orchestrator",
                allowed_egress_targets={"coding-1"},
                allowed_ingress_sources={"coding-1"},
                allowed_channels={"progress.*", "system"},
            ),
            SandboxPolicy(
                sandbox_id="coding-1",
                allowed_egress_targets={"orchestrator"},
                allowed_ingress_sources={"orchestrator"},
                allowed_channels={"progress.coding-1", "system"},
            ),
        ]
    )
    return harness


@pytest.fixture
async def three_sandbox_harness(
    harness: IntegrationHarness,
) -> IntegrationHarness:
    """Orchestrator + coding-1 + review-1.

    ::

        orchestrator → coding-1   (egress + ingress)
        orchestrator → review-1   (egress + ingress)
        coding-1 → orchestrator   (egress + ingress)
        review-1 → orchestrator   (egress + ingress)
        coding-1 ↛ review-1      (blocked both directions)
    """
    await harness.start(
        [
            SandboxPolicy(
                sandbox_id="orchestrator",
                allowed_egress_targets={"coding-1", "review-1"},
                allowed_ingress_sources={"coding-1", "review-1"},
                allowed_channels={"progress.*", "system"},
            ),
            SandboxPolicy(
                sandbox_id="coding-1",
                allowed_egress_targets={"orchestrator"},
                allowed_ingress_sources={"orchestrator"},
                allowed_channels={"progress.coding-1", "system"},
            ),
            SandboxPolicy(
                sandbox_id="review-1",
                allowed_egress_targets={"orchestrator"},
                allowed_ingress_sources={"orchestrator"},
                allowed_channels={"progress.review-1", "system"},
            ),
        ]
    )
    return harness
