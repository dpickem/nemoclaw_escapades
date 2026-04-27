"""Orchestrator-side delegation: spawn a sub-agent, ship a task, collect the reply.

This module owns the orchestrator's half of the M2b ``task.assign`` /
``task.complete`` round-trip (design §6.1 message flow, §8.1 per-agent
concurrency).  The matching sub-agent half lives in
``agent/__main__.py::_run_nmb_mode``.

The lifecycle of one delegation:

1. ``DelegationManager.delegate(...)`` is called by the orchestrator's
   ``delegate_task`` tool (Phase 3a-5) with a fully-built
   :class:`TaskAssignPayload`.
2. The semaphore (sized by ``DelegationConfig.max_concurrent``)
   throttles concurrent in-flight delegations.  Excess calls block
   on the semaphore until a slot frees up.
3. Spawn a sub-agent process via ``subprocess`` (``python -m
   nemoclaw_escapades.agent --nmb``).  The process inherits the
   orchestrator's env, including the OpenShell-provider placeholder
   tokens.
4. Wait for the sub-agent's NMB sandbox to come online (it announces
   itself when it calls ``MessageBus.connect``), then send the
   ``task.assign`` via ``bus.request(timeout=DelegationConfig.task_timeout_s)``.
5. The sub-agent runs the task, replies with ``task.complete`` (or
   ``task.error``).  Validate the reply through Pydantic and return
   the typed payload.
6. Tear down the sub-agent process.

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

from nemoclaw_escapades.config import DEFAULT_NMB_DEFAULT_REQUEST_TIMEOUT, DelegationConfig
from nemoclaw_escapades.nmb.client import MessageBus
from nemoclaw_escapades.nmb.protocol import (
    TASK_ASSIGN,
    TASK_COMPLETE,
    PayloadValidationError,
    TaskAssignPayload,
    TaskCompletePayload,
    TaskErrorPayload,
    dump,
    load,
)
from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("orchestrator.delegation")


class DelegationError(Exception):
    """Raised when a delegation can't proceed or completes with an error.

    Wraps four distinct failure modes:

    - **Spawn-depth exceeded** — the requested delegation would
      nest deeper than ``max_spawn_depth`` permits.  M2b doesn't
      support sub-agent → sub-agent delegation, so this is a hard
      cap, not a backoff.
    - **Sub-agent process failed to start** — ``subprocess.Popen``
      failed (binary missing) or the process exited before
      announcing on NMB (typically a config error inside the
      sub-agent).
    - **Sub-agent replied with `task.error`** — ``recoverable``
      flag preserved on the wrapped payload so the finalisation
      model can decide whether to ``re_delegate``.
    - **NMB transport failure** — ``request`` timed out, target
      offline, etc.

    The wrapped exception is on ``__cause__`` for callers that need
    to branch on the specific failure.
    """

    def __init__(self, message: str, *, error_payload: TaskErrorPayload | None = None) -> None:
        super().__init__(message)
        self.error_payload = error_payload


@dataclass
class DelegationResult:
    """Outcome of a successful ``DelegationManager.delegate`` call.

    Holds the validated complete payload plus the sub-agent's
    sandbox identity (recorded for audit / per-workflow tracking).

    Attributes:
        complete: The validated ``task.complete`` payload from the
            sub-agent.  Baseline drift detection (echo match against
            the assigned ``WorkspaceBaseline``) is the *finalisation*
            step's job, not this module's — we only validate the
            wire shape.
        sub_agent_sandbox_id: NMB sandbox identifier of the sub-agent
            that handled the delegation.  Equal to
            ``TaskAssignPayload.agent_id`` for the same workflow
            (see ``DelegationManager.delegate``'s ``agent_id``
            choice).
    """

    complete: TaskCompletePayload
    sub_agent_sandbox_id: str


# Type for the spawn-callback hook used by tests.  Production code
# uses :func:`DelegationManager._spawn_subprocess`; the integration
# tests inject a stub that doesn't actually exec a child process.
SpawnCallback = Callable[[str, str], Awaitable["SpawnedAgent"]]


@dataclass
class SpawnedAgent:
    """Handle to a running sub-agent process.

    Attributes:
        sandbox_id: NMB identity the sub-agent connects with.  Mirrors
            ``TaskAssignPayload.agent_id`` so sends route correctly.
        terminate: Coroutine that stops the underlying process.  The
            production implementation calls
            :meth:`asyncio.subprocess.Process.terminate` and waits
            for the exit; tests inject a no-op.
    """

    sandbox_id: str
    terminate: Callable[[], Awaitable[None]]


class DelegationManager:
    """Manages the orchestrator's half of one or more delegations.

    Owned by the orchestrator (one instance per process).  Holds
    the shared NMB bus, the per-agent ``Semaphore``, and the
    config-driven caps.

    The :class:`MessageBus` is constructed by the caller (the
    orchestrator's main loop), not by this manager — that keeps
    the bus lifecycle (connect / close) decoupled from delegation
    semantics, and lets the orchestrator's NMB event loop (Phase
    3a-4) share the same bus instance for inbound message
    dispatch.

    Attributes:
        bus: NMB ``MessageBus`` the orchestrator is connected on.
        config: :class:`DelegationConfig`-shaped runtime knobs.
        spawn_callback: Coroutine that spawns a sub-agent process
            and returns a :class:`SpawnedAgent` handle.  Production
            code uses :meth:`_spawn_subprocess`; tests pass a stub.
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

    async def delegate(self, task: TaskAssignPayload) -> DelegationResult:
        """Spawn a sub-agent, ship the task, await the typed complete payload.

        Args:
            task: Fully-built assignment.  The orchestrator's
                ``delegate_task`` tool (Phase 3a-5) is responsible
                for picking ``max_turns`` / ``model`` from the task
                profile and pinning the workspace baseline.  This
                method takes the payload as-is.

        Returns:
            A :class:`DelegationResult` with the validated complete
            payload and the sub-agent's sandbox identity.

        Raises:
            DelegationError: On any failure mode (spawn-depth
                exceeded, transport timeout, or a ``task.error``
                reply).  ``error_payload`` is set when the sub-agent
                replied with ``task.error`` (so the orchestrator's
                finalization model can branch on ``recoverable``).
        """
        self._check_spawn_depth(task)

        # Hold the semaphore for the whole delegation lifecycle —
        # spawn + send + reply.  An eight-tasks-deep refactor still
        # only counts as one delegation against the cap.
        async with self._semaphore:
            agent = await self._spawn(task.agent_id, task.workspace_root)
            try:
                reply = await self._send_assign_and_await_reply(task, agent.sandbox_id)
            finally:
                await agent.terminate()
        return DelegationResult(complete=reply, sub_agent_sandbox_id=agent.sandbox_id)

    def _check_spawn_depth(self, task: TaskAssignPayload) -> None:
        """Refuse delegations that would exceed ``max_spawn_depth``.

        M2b's only legitimate spawn shape is orchestrator → coding
        agent (depth 1).  A coding sub-agent attempting to delegate
        further would land here at depth 2 and get rejected — that's
        an M3 review-agent capability, not M2b.

        The caller is responsible for setting
        ``parent_sandbox_id`` to identify the spawn origin; we use
        it as a proxy for depth (any non-orchestrator parent ⇒
        depth ≥ 2).
        """
        # Phase 3a uses a simple proxy: if parent_sandbox_id is
        # *not* the orchestrator, we're being called from a
        # sub-agent's own delegation tool, which would be depth 2.
        # M3 will switch this to an explicit depth field on the
        # payload once review agents and nested delegation are real.
        if self._config.max_spawn_depth < 1:
            raise DelegationError(
                f"max_spawn_depth={self._config.max_spawn_depth} forbids any delegation",
            )
        # Currently no inspection of the lineage chain — the
        # one-level cap is enforced by construction (sub-agents
        # don't have a delegate_task tool registered until M3).

    async def _send_assign_and_await_reply(
        self,
        task: TaskAssignPayload,
        sub_agent_sandbox_id: str,
    ) -> TaskCompletePayload:
        """Send ``task.assign``, await the typed reply, validate it.

        The ``request`` call blocks until the sub-agent replies (or
        the configured timeout fires).  We catch validation errors
        and transport failures separately so the
        :class:`DelegationError` message tells the caller which
        layer broke.

        Raises:
            DelegationError: On NMB transport failure, validation
                failure, or a ``task.error`` reply.
        """
        # ``request`` only accepts ``float``, so fall through to the
        # NMB default when the operator hasn't pinned a timeout.
        timeout = self._config.task_timeout_s
        if timeout is None:
            timeout = DEFAULT_NMB_DEFAULT_REQUEST_TIMEOUT
        try:
            reply = await self._bus.request(
                to=sub_agent_sandbox_id,
                type=TASK_ASSIGN,
                payload=dump(task),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 — broad NMB transport surface
            raise DelegationError(
                f"task.assign delivery failed: {exc}",
            ) from exc

        if reply.type == TASK_COMPLETE:
            try:
                return load(TaskCompletePayload, TASK_COMPLETE, reply.payload)
            except PayloadValidationError as exc:
                raise DelegationError(
                    f"task.complete payload validation failed: {exc}",
                ) from exc

        # Anything else (task.error or an unexpected type) is a
        # delegation failure.  Wrap the typed error in
        # DelegationError.error_payload so the finalization model
        # can branch on ``recoverable``.
        try:
            error_payload = load(TaskErrorPayload, "task.error", reply.payload)
        except PayloadValidationError as exc:
            raise DelegationError(
                f"unexpected reply type {reply.type!r} (validation also failed: {exc})",
            ) from exc
        raise DelegationError(
            f"sub-agent returned task.error: {error_payload.error}",
            error_payload=error_payload,
        )

    def _default_spawn_callback(self) -> SpawnCallback:
        """Return the production spawn callback.

        Factored out so subclasses / tests can replace it without
        touching ``__init__``.  The default uses ``asyncio
        .create_subprocess_exec`` on ``python -m
        DelegationConfig.sub_agent_module --nmb``.
        """

        async def _spawn(sub_agent_sandbox_id: str, workspace_root: str) -> SpawnedAgent:
            env = os.environ.copy()
            # The sub-agent reads broker_url + sandbox_id from
            # config.nmb.  We pin sandbox_id here so the orchestrator
            # knows what to address.
            env["AGENT_SANDBOX_ID"] = sub_agent_sandbox_id
            env["CODING_WORKSPACE_ROOT"] = workspace_root
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
