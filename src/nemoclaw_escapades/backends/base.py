"""Abstract base class for inference backends.

Every model provider (NVIDIA Inference Hub, OpenAI, Anthropic, a local
vLLM server, etc.) is wrapped in a ``BackendBase`` subclass so the
orchestrator never knows which provider is in use.  Adding a new
provider means creating one new file with one new subclass — no changes
to the orchestrator or connector code.

The contract is intentionally minimal:

- ``complete()`` — send an OpenAI-format message list, get back a
  structured ``InferenceResponse``.
- ``close()`` — release resources (HTTP pools, sockets, etc.).

Retry logic, timeout enforcement, and error categorisation are the
responsibility of each concrete implementation, *not* the caller.
If a call fails after all retries, the backend raises ``InferenceError``
with a categorised ``ErrorCategory`` so the orchestrator can surface
the right user-facing message.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from nemoclaw_escapades.models.types import InferenceRequest, InferenceResponse


class BackendBase(ABC):
    """One stable interface for model calls.

    The orchestrator depends on this contract, never on a specific provider.
    Adding a new provider means adding a new subclass, not modifying existing code.
    """

    @abstractmethod
    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        """Send a completion request and return the model's response.

        Implementations must handle retries, timeouts, and error categorization
        internally and raise InferenceError for unrecoverable failures.
        """

    async def close(self) -> None:
        """Release any held resources (HTTP clients, connections, etc.)."""
