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

1. ``DelegationManager.delegate(task, context=...)`` is called by
   the orchestrator's ``delegate_task`` tool.
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

from nemoclaw_escapades.config import DelegationConfig
from nemoclaw_escapades.nmb.client import MessageBus, NMBConnectionError
from nemoclaw_escapades.nmb.protocol import (
    TASK_ASSIGN,
    TaskAssignPayload,
    TaskErrorPayload,
    dump,
)
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.orchestrator.workflow import WorkflowContext

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
        # Per-workflow spawn handles.  Two responsibilities:
        # - Lets :meth:`close` terminate sub-agent processes that
        #   were still in flight when the orchestrator was asked to
        #   shut down.
        # - Each entry holds exactly one slot of :data:`_semaphore`
        #   for the workflow's lifetime — :meth:`delegate` acquires
        #   the slot when the entry first appears, :meth:`terminate`
        #   releases it when the entry leaves.  This ties
        #   :data:`DelegationConfig.max_concurrent` to live workflows
        #   rather than to concurrent spawn calls; without it the
        #   "hard cap" docstring would be a fiction the moment
        #   ``send`` returned (the slot would free even though the
        #   sub-agent process was still alive).
        self._spawned: dict[str, SpawnedAgent] = {}

    async def delegate(
        self,
        task: TaskAssignPayload,
        *,
        context: WorkflowContext | None = None,
    ) -> DelegationResult:
        """Spawn a sub-agent and send ``task.assign``; return immediately.

        The sub-agent's reply (``task.complete`` / ``task.error``)
        is routed to the orchestrator's :class:`WorkflowDispatcher`
        rather than awaited here.  ``context`` is accepted but only
        used by callers (the ``delegate_task`` tool) that want to
        carry it through to the dispatcher's workflow registry.

        Args:
            task: Fully-built assignment.  The caller is responsible
                for picking ``max_turns`` / ``model`` from the task
                profile and pinning the workspace baseline.
            context: Optional :class:`WorkflowContext`.  Carried only
                so re-delegation calls from the finalisation flow
                can pass through the originating channel/thread; the
                manager itself does not register it (the caller
                does, before calling ``delegate``).

        Returns:
            A :class:`DelegationResult` carrying the workflow id and
            the spawned sub-agent's sandbox id.

        Raises:
            DelegationError: On spawn-depth violations, spawn
                failures, or transport rejections.
        """
        del context  # accepted for API symmetry; caller registers with dispatcher
        self._check_spawn_depth(task)

        # Semaphore lifecycle invariant: ``len(self._spawned)`` slots
        # are held by :data:`_semaphore` at all times.  We acquire when
        # the workflow_id is *new* to ``_spawned`` (a fresh delegation)
        # and release when it leaves (terminate, close, or send-failure
        # cleanup below).  Re-delegation reuses the workflow_id, so
        # iteration N+1 keeps holding the slot iteration 1 acquired —
        # no double-acquire, no leak.
        #
        # Without this discipline the cap is fiction: ``async with
        # self._semaphore`` previously released the slot the moment
        # ``send`` returned, even though the sub-agent process was
        # still alive and the dispatcher hadn't even seen the eventual
        # ``task.complete`` yet.  ``DelegationConfig.max_concurrent``
        # is documented as "Hard cap on concurrent in-flight
        # delegations"; the slot must live as long as the workflow
        # does.
        acquired_for_new_workflow = task.workflow_id not in self._spawned
        if acquired_for_new_workflow:
            await self._semaphore.acquire()

        try:
            try:
                agent = await self._spawn(task.agent_id, task.workspace_root)
            except Exception as exc:  # noqa: BLE001 — spawn callback is pluggable
                raise DelegationError(f"failed to spawn sub-agent: {exc}") from exc

            # Swap-and-terminate: ``re_delegate`` reuses the
            # ``workflow_id`` for iteration N+1, so ``_spawned`` may
            # already hold iteration N's handle.  An unconditional
            # overwrite would drop that reference and leak it
            # permanently from the registry — :meth:`terminate` and
            # :meth:`close` would then only ever reach the latest
            # iteration.  Pop first, install the new agent, *then*
            # terminate the old one so a concurrent
            # :meth:`terminate` call lands on the new agent rather
            # than the about-to-die old one.
            previous = self._spawned.get(task.workflow_id)
            self._spawned[task.workflow_id] = agent
            if previous is not None:
                # Single-shot sub-agents typically self-exit at
                # ``task.complete`` so this is usually a no-op
                # against a process whose ``returncode`` is already
                # set; the call is here for the pathological "old
                # iteration is hung / running long" cases.  Errors
                # are swallowed so a broken old handle can't block
                # the new iteration's send below.
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
                # Send failed for good — terminate this iteration's
                # sub-agent and propagate.  Compare-and-swap on the
                # pop so we don't accidentally evict a handle a
                # later (unlikely but possible) re-delegation
                # already installed under the same workflow_id.
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
            # Slot accounting on any failure path: if the workflow no
            # longer has an entry in ``_spawned``, free its slot.
            #
            # - New workflow + spawn or send failure → entry never
            #   installed (or popped above) → release the slot we
            #   just acquired.
            # - Re-delegation + send failure → entry popped above →
            #   release the slot the *prior* iteration was holding
            #   (the workflow is dead, no one else will free it).
            # - Re-delegation + spawn failure → previous entry
            #   untouched → workflow still alive → don't release.
            #
            # CancelledError from anywhere inside lands here too, so
            # task cancellation between acquire and install can't
            # leak a slot.
            if task.workflow_id not in self._spawned:
                self._semaphore.release()
            raise

        return DelegationResult(
            workflow_id=task.workflow_id,
            sub_agent_sandbox_id=agent.sandbox_id,
        )

    async def terminate(self, workflow_id: str) -> None:
        """Tear down a sub-agent process by workflow id.

        Invoked from :meth:`WorkflowDispatcher.deregister_workflow`
        whenever a workflow ends — terminal finalisation,
        ``task.error`` arrival, finalisation exception, Push & PR /
        Discard button click, or the spawn-failure cleanup inside
        :meth:`delegate`.  Without this caller chain, ``_spawned``
        would accumulate handles for the orchestrator's entire
        lifetime; the dispatcher's wire-through in
        :meth:`WorkflowDispatcher.__init__`'s ``delegation_manager``
        argument is what makes this method actually run on the
        per-workflow path (not just at process shutdown via
        :meth:`close`).

        Idempotent for unknown workflow ids — late or duplicate
        calls land on the ``pop`` ``None`` default and short-circuit
        without touching the semaphore.
        """
        agent = self._spawned.pop(workflow_id, None)
        if agent is None:
            return
        # The slot acquired by :meth:`delegate` for this workflow is
        # released here, mirroring the
        # ``len(self._spawned)``-slots-held invariant: removing a
        # workflow from the registry must give its slot back so the
        # next ``delegate`` call can proceed.
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

    def _check_spawn_depth(self, task: TaskAssignPayload) -> None:
        """Refuse delegations that would exceed ``max_spawn_depth``.

        M2b's only legitimate spawn shape is orchestrator → coding
        agent (depth 1).  A coding sub-agent attempting to delegate
        further would land here at depth 2 and get rejected.
        """
        del task  # depth is currently a global cap, not per-task
        if self._config.max_spawn_depth < 1:
            raise DelegationError(
                f"max_spawn_depth={self._config.max_spawn_depth} forbids any delegation",
            )
        # Currently no inspection of the lineage chain — the
        # one-level cap is enforced by construction (sub-agents
        # don't have a delegate_task tool registered until M3).

    async def _send_assign_with_readiness_retry(
        self,
        task: TaskAssignPayload,
        sub_agent_sandbox_id: str,
    ) -> None:
        """Retry ``bus.send`` on ``TARGET_OFFLINE`` until the sub-agent connects.

        Bridges the gap between ``subprocess.exec`` returning and the
        sub-agent's NMB connection becoming live: a freshly spawned
        process needs a beat to import, load config, and finish the
        broker handshake.  Without this loop the orchestrator would
        race the sub-agent and reliably fail with ``TARGET_OFFLINE``
        on every production delegation.

        Raises:
            DelegationError: If the readiness window elapses without
                a successful send, or if the broker rejects with a
                non-``TARGET_OFFLINE`` error.
        """
        deadline = asyncio.get_running_loop().time() + self._config.spawn_ready_timeout_s
        last_offline_error: NMBConnectionError | None = None
        attempts = 0
        while True:
            attempts += 1
            try:
                await self._bus.send(
                    to=sub_agent_sandbox_id,
                    type=TASK_ASSIGN,
                    payload=dump(task),
                )
                if attempts > 1:
                    logger.info(
                        "Sub-agent %s came online after %d readiness probes",
                        sub_agent_sandbox_id,
                        attempts,
                    )
                return
            except NMBConnectionError as exc:
                if not _is_target_offline(exc):
                    raise DelegationError(
                        f"task.assign delivery failed: {exc}",
                    ) from exc
                last_offline_error = exc
            except Exception as exc:  # noqa: BLE001 — broad NMB transport surface
                raise DelegationError(
                    f"task.assign delivery failed: {exc}",
                ) from exc

            now = asyncio.get_running_loop().time()
            if now >= deadline:
                raise DelegationError(
                    "sub-agent never came online within "
                    f"spawn_ready_timeout_s={self._config.spawn_ready_timeout_s}s "
                    f"(last broker error: {last_offline_error})",
                ) from last_offline_error

            sleep_for = min(
                self._config.spawn_ready_poll_interval_s,
                max(0.0, deadline - now),
            )
            await asyncio.sleep(sleep_for)

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


def _is_target_offline(exc: NMBConnectionError) -> bool:
    """True iff *exc* is the broker's "target not connected" rejection.

    The broker formats these as ``Broker error TARGET_OFFLINE: ...``
    (see ``nmb/broker.py::_handle_request``); we substring-match
    rather than expose the ``ErrorCode`` enum because the public
    surface of :class:`NMBConnectionError` only carries the message
    string.
    """
    return "TARGET_OFFLINE" in str(exc)
