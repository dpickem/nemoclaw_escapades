"""Orchestrator-side delegation: spawn a sub-agent and ship a task.assign.

Phase 3b architecture (design §7.1, §8.2): :class:`DelegationManager`
is fire-and-forget.  ``delegate(task)`` spawns the sub-agent, sends
``task.assign`` over NMB, and returns immediately with a
:class:`DelegationResult` describing the in-flight workflow.  The
sub-agent's ``task.complete`` / ``task.error`` / ``audit.flush``
arrivals land on the orchestrator's :class:`WorkflowDispatcher`,
which kicks off finalisation as an independent ``asyncio.Task`` so
concurrent workflows finalise concurrently and the user-facing
chat thread never blocks on sub-agent latency.

The lifecycle of one delegation:

1. ``DelegationManager.delegate(task)`` is called by the
   orchestrator's ``delegate_task`` tool.
2. Spawn-depth and concurrency caps (semaphore) gate the request.
3. Spawn a sub-agent process via ``subprocess`` (``python -m
   nemoclaw_escapades.agent --nmb``).
4. Wait for the sub-agent's NMB sandbox to come online by retrying
   ``bus.send`` on ``TARGET_OFFLINE`` until ``spawn_ready_timeout_s``
   elapses; once one send succeeds the broker has accepted the
   request and we return.
5. The sub-agent's ``task.complete`` / ``task.error`` / ``audit.flush``
   arrive on the orchestrator's bus listen queue; the dispatcher
   routes them to the per-workflow handler.
6. Sub-agent process teardown happens when the dispatcher's
   finalisation task finishes (or ``DelegationManager.close()`` is
   called on shutdown).

Single-shot per process matches the M3 multi-sandbox shape:
``openshell sandbox create`` will replace the ``subprocess`` spawn
in M3, but the NMB protocol stays unchanged (design §1).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tenacity import AsyncRetrying, retry_if_exception, stop_after_delay, wait_fixed

from nemoclaw_escapades.config import DelegationConfig
from nemoclaw_escapades.nmb.client import MessageBus, NMBConnectionError
from nemoclaw_escapades.nmb.protocol import (
    TASK_ASSIGN,
    TaskAssignPayload,
    TaskErrorPayload,
    dump,
)
from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("orchestrator.delegation")


class DelegationError(Exception):
    """Raised when a delegation can't be spawned or sent.

    Three failure modes:

    - **Spawn-depth exceeded** — the requested delegation would
      nest deeper than ``max_spawn_depth`` permits.
    - **Sub-agent process failed to start** — ``subprocess.exec``
      failed (binary missing) or the process exited before
      announcing on NMB.
    - **NMB transport failure** — ``send`` rejected by the broker
      (rate-limited, broker misconfig) or the readiness window
      elapsed.

    ``task.error`` arrivals are *not* surfaced through this exception
    in the new architecture — they're routed to the
    :class:`WorkflowDispatcher`, which renders them to the user.
    The legacy ``error_payload`` field is kept on the exception for
    pre-Phase-3b callers that want to peek at a synchronous error
    payload.
    """

    def __init__(self, message: str, *, error_payload: TaskErrorPayload | None = None) -> None:
        super().__init__(message)
        self.error_payload = error_payload


@dataclass
class DelegationResult:
    """Outcome of a successful ``DelegationManager.delegate`` call.

    Attributes:
        workflow_id: The ``TaskAssignPayload.workflow_id`` for the
            in-flight workflow.  The orchestrator's
            ``delegate_task`` tool returns a user-facing
            acknowledgement keyed off this.
        sub_agent_sandbox_id: NMB sandbox identifier of the spawned
            sub-agent.  Equal to ``TaskAssignPayload.agent_id``.
    """

    workflow_id: str
    sub_agent_sandbox_id: str


# Type for the spawn-callback hook used by tests.  Production code
# uses :meth:`DelegationManager._default_spawn_callback`; tests pass
# a stub that doesn't actually exec a child process.
SpawnCallback = Callable[[str, str], Awaitable["SpawnedAgent"]]


@dataclass
class SpawnedAgent:
    """Handle to a running sub-agent process.

    Attributes:
        sandbox_id: NMB identity the sub-agent connects with.
        terminate: Coroutine that stops the underlying process.
            Used during ``DelegationManager.close()`` and on
            workflow teardown.
    """

    sandbox_id: str
    terminate: Callable[[], Awaitable[None]]


class DelegationManager:
    """Fire-and-forget orchestrator-side delegation.

    Owned by the orchestrator (one instance per process).  Holds
    the shared NMB bus, the per-agent ``Semaphore``, and the
    config-driven caps.  The :class:`MessageBus` is constructed by
    the caller (the orchestrator's main loop) so the bus lifecycle
    stays decoupled from delegation semantics.

    Reply routing is the dispatcher's job — the manager itself does
    not call :meth:`MessageBus.request` and does not wait for
    ``task.complete``.

    Attributes:
        config: :class:`DelegationConfig`-shaped runtime knobs.
        spawn_callback: Coroutine that spawns a sub-agent process
            and returns a :class:`SpawnedAgent` handle.
    """

    def __init__(
        self,
        bus: MessageBus,
        config: DelegationConfig,
        *,
        spawn_callback: SpawnCallback | None = None,
    ) -> None:
        self._bus = bus
        self._config = config
        self._spawn = spawn_callback or self._default_spawn_callback()
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        # Live workflow handles.  Each entry owns one semaphore slot
        # until terminate()/close() removes it.
        self._spawned: dict[str, SpawnedAgent] = {}

    async def delegate(
        self,
        task: TaskAssignPayload,
    ) -> DelegationResult:
        """Spawn a sub-agent and send ``task.assign``; return immediately.

        The sub-agent's reply (``task.complete`` / ``task.error``)
        is handled later by :class:`WorkflowDispatcher`.

        Args:
            task: Fully-built assignment.

        Returns:
            Workflow id and spawned sub-agent sandbox id.

        Raises:
            DelegationError: On spawn-depth, spawn, or transport failures.
        """
        self._check_spawn_depth()

        # The concurrency cap applies to live workflows, not just the
        # spawn+send window.  Re-delegation reuses the workflow's slot.
        acquired_for_new_workflow = task.workflow_id not in self._spawned
        if acquired_for_new_workflow:
            await self._semaphore.acquire()

        try:
            try:
                agent = await self._spawn(task.agent_id, task.workspace_root)
            except Exception as exc:  # noqa: BLE001 — spawn callback is pluggable
                raise DelegationError(f"failed to spawn sub-agent: {exc}") from exc

            # Re-delegation replaces the prior iteration's handle.
            # Install the new agent before stopping the old one so a
            # racing terminate() targets the active iteration.
            previous = self._spawned.get(task.workflow_id)
            self._spawned[task.workflow_id] = agent
            if previous is not None:
                # Best-effort cleanup for a prior iteration that did
                # not already self-exit after task.complete.
                try:
                    await previous.terminate()
                except Exception:  # noqa: BLE001 — defensive around process lifecycle
                    logger.warning(
                        "Failed to terminate previous iteration's sub-agent "
                        "during re-delegation; new agent installed regardless",
                        extra={
                            "workflow_id": task.workflow_id,
                            "old_sandbox_id": previous.sandbox_id,
                            "new_sandbox_id": agent.sandbox_id,
                        },
                        exc_info=True,
                    )
            try:
                await self._send_assign_with_readiness_retry(task, agent.sandbox_id)
            except DelegationError:
                # Compare-and-pop so a racing re-delegation cannot be
                # evicted by this failed send path.
                if self._spawned.get(task.workflow_id) is agent:
                    self._spawned.pop(task.workflow_id, None)
                await agent.terminate()
                raise
            except Exception as exc:  # noqa: BLE001 — defensive around transport implementations
                if self._spawned.get(task.workflow_id) is agent:
                    self._spawned.pop(task.workflow_id, None)
                await agent.terminate()
                raise DelegationError(f"delegation failed: {exc}") from exc
        except BaseException:
            # If failure/cancellation removed the workflow handle,
            # release the slot it held.
            if task.workflow_id not in self._spawned:
                self._semaphore.release()
            raise

        return DelegationResult(
            workflow_id=task.workflow_id,
            sub_agent_sandbox_id=agent.sandbox_id,
        )

    async def terminate(self, workflow_id: str) -> None:
        """Tear down a sub-agent process by workflow id.

        Called by dispatcher workflow cleanup and process shutdown.
        Idempotent for unknown workflow ids.
        """
        agent = self._spawned.pop(workflow_id, None)
        if agent is None:
            return

        # Removing the workflow gives back the slot delegate() acquired.
        self._semaphore.release()
        try:
            await agent.terminate()
        except Exception:  # noqa: BLE001 — defensive around process lifecycle
            logger.warning(
                "Sub-agent terminate raised",
                extra={"workflow_id": workflow_id, "sandbox_id": agent.sandbox_id},
                exc_info=True,
            )

    async def close(self) -> None:
        """Terminate every still-running sub-agent.

        Called from ``main.py``'s teardown so the asyncio loop can
        shut down cleanly.  Idempotent.
        """
        for workflow_id in list(self._spawned.keys()):
            await self.terminate(workflow_id)

    def _check_spawn_depth(self) -> None:
        """Refuse delegations that would exceed ``max_spawn_depth``.

        M2b's only legitimate spawn shape is orchestrator → coding
        agent (depth 1).  A coding sub-agent attempting to delegate
        further would land here at depth 2 and get rejected.
        """
        if self._config.max_spawn_depth < 1:
            raise DelegationError(
                f"max_spawn_depth={self._config.max_spawn_depth} forbids any delegation",
            )
        # One-level cap is enforced by construction until M3.

    async def _send_assign_with_readiness_retry(
        self,
        task: TaskAssignPayload,
        sub_agent_sandbox_id: str,
    ) -> None:
        """Retry ``bus.send`` on ``TARGET_OFFLINE`` until the sub-agent connects.

        Raises:
            DelegationError: If the readiness window elapses without
                a successful send, or if the broker rejects with a
                non-``TARGET_OFFLINE`` error.
        """
        attempts = 0
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception(_is_target_offline),
                wait=wait_fixed(self._config.spawn_ready_poll_interval_s),
                stop=stop_after_delay(self._config.spawn_ready_timeout_s),
                reraise=True,
            ):
                attempts = attempt.retry_state.attempt_number
                with attempt:
                    await self._bus.send(
                        to=sub_agent_sandbox_id,
                        type=TASK_ASSIGN,
                        payload=dump(task),
                    )
        except NMBConnectionError as exc:
            if _is_target_offline(exc):
                raise DelegationError(
                    "sub-agent never came online within "
                    f"spawn_ready_timeout_s={self._config.spawn_ready_timeout_s}s "
                    f"(last broker error: {exc})",
                ) from exc
            raise DelegationError(f"task.assign delivery failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — broad NMB transport surface
            raise DelegationError(f"task.assign delivery failed: {exc}") from exc

        if attempts > 1:
            logger.info(
                "Sub-agent %s came online after %d readiness probes",
                sub_agent_sandbox_id,
                attempts,
            )

    def _default_spawn_callback(self) -> SpawnCallback:
        """Return the production spawn callback.

        Factored out so subclasses / tests can replace it without
        touching ``__init__``.  The default uses
        :func:`asyncio.create_subprocess_exec` on
        ``python -m DelegationConfig.sub_agent_module --nmb``.
        """

        async def _spawn(sub_agent_sandbox_id: str, workspace_root: str) -> SpawnedAgent:
            env = os.environ.copy()
            env["NEMOCLAW_SANDBOX_ID"] = sub_agent_sandbox_id
            env["NEMOCLAW_WORKSPACE_ROOT"] = workspace_root
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                self._config.sub_agent_module,
                "--nmb",
                env=env,
            )

            async def _terminate() -> None:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except TimeoutError:
                        proc.kill()
                        await proc.wait()

            return SpawnedAgent(sandbox_id=sub_agent_sandbox_id, terminate=_terminate)

        return _spawn


def _is_target_offline(exc: BaseException) -> bool:
    """True iff *exc* is the broker's "target not connected" rejection.

    The broker formats these as ``Broker error TARGET_OFFLINE: ...``
    (see ``nmb/broker.py::_handle_request``); we substring-match
    rather than expose the ``ErrorCode`` enum because the public
    surface of :class:`NMBConnectionError` only carries the message
    string.
    """
    return isinstance(exc, NMBConnectionError) and "TARGET_OFFLINE" in str(exc)
