"""Testing infrastructure for multi-sandbox NMB integration tests.

Provides :class:`PolicyBroker` (broker with per-sandbox policy
enforcement), :class:`SandboxPolicy` (per-sandbox rule declarations),
:class:`IntegrationHarness` (lifecycle manager), and
:class:`SandboxHandle` (per-sandbox convenience wrapper).
"""

from nemoclaw_escapades.nmb.testing.harness import IntegrationHarness, SandboxHandle
from nemoclaw_escapades.nmb.testing.policy import PolicyBroker, SandboxPolicy

__all__ = [
    "IntegrationHarness",
    "PolicyBroker",
    "SandboxHandle",
    "SandboxPolicy",
]
