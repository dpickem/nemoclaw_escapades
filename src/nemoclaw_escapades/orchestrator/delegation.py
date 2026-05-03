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
4. Wait for the sub-agent's NMB sandbox to come online — fresh
   subprocesses need a beat to import, load config, and complete the
   broker handshake.  Implemented by retrying the ``task.assign``
   send on ``TARGET_OFFLINE`` until the configured
   ``spawn_ready_timeout_s`` elapses; once any send succeeds, the
   broker has accepted the request and we proceed to await the
   reply with the full ``task_timeout_s`` budget.
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
from nemoclaw_escapades.nmb.client import MessageBus, NMBConnectionError
from nemoclaw_escapades.nmb.models import NMBMessage
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

        async with self._semaphore:
            try:
                agent = await self._spawn(task.agent_id, task.workspace_root)
            except Exception as exc:  # noqa: BLE001 — spawn callback is pluggable
                raise DelegationError(f"failed to spawn sub-agent: {exc}") from exc

            try:
                reply = await self._send_assign_with_readiness_retry(
                    task,
                    agent.sandbox_id,
                )
            except DelegationError:
                raise
            except Exception as exc:  # noqa: BLE001 — defensive around transport implementations
                raise DelegationError(f"delegation failed: {exc}") from exc
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

    async def _send_assign_with_readiness_retry(
        self,
        task: TaskAssignPayload,
        sub_agent_sandbox_id: str,
    ) -> TaskCompletePayload:
        """Send ``task.assign`` with retry-on-``TARGET_OFFLINE``, then await reply.

        Bridges the gap between ``subprocess.exec`` returning and the
        sub-agent's NMB connection becoming live: a freshly spawned
        process needs a beat to import, load config, and finish the
        broker handshake.  Without this loop the orchestrator would
        race the sub-agent and reliably fail with ``TARGET_OFFLINE``
        on every production delegation.

        The retry window is **delivery-only**.  Once the broker has
        accepted the request (i.e. the sub-agent's connection took
        the frame), :meth:`_interpret_reply` validates the eventual
        reply with the full ``task_timeout_s`` budget already applied
        by :meth:`_send_assign_once`.  Any later transport error
        reflects real task semantics, not readiness, and is reported
        as-is.

        Two distinct timeouts are at play:

        - ``spawn_ready_timeout_s`` caps the *delivery* phase — how
          long we keep retrying ``TARGET_OFFLINE``.  Sized for spawn
          cost (subprocess + import + handshake), typically seconds.
        - ``task_timeout_s`` caps the *reply* phase — how long we
          wait for ``task.complete`` once the request has been
          accepted.  Sized for actual task work, typically minutes.

        Raises:
            DelegationError: If the readiness window elapses without
                a successful delivery, or if the eventual reply
                fails validation / is a ``task.error``.
        """
        deadline = asyncio.get_running_loop().time() + self._config.spawn_ready_timeout_s
        last_offline_error: NMBConnectionError | None = None
        attempts = 0
        while True:
            attempts += 1
            try:
                reply = await self._send_assign_once(task, sub_agent_sandbox_id)
                if attempts > 1:
                    logger.info(
                        "Sub-agent %s came online after %d readiness probes",
                        sub_agent_sandbox_id,
                        attempts,
                    )
                return self._interpret_reply(reply)
            except NMBConnectionError as exc:
                if not _is_target_offline(exc):
                    # Real broker error (rate-limited, broker
                    # misconfig, etc.) — not a readiness issue.
                    raise DelegationError(
                        f"task.assign delivery failed: {exc}",
                    ) from exc
                last_offline_error = exc
            except DelegationError:
                # Already classified by ``_interpret_reply`` (e.g. a
                # ``task.error`` reply — the typed error payload is
                # already attached).  Propagate unchanged so callers
                # can branch on ``error_payload.recoverable``.
                raise
            except Exception as exc:  # noqa: BLE001 — broad NMB transport surface
                # Anything else from the bus (``TimeoutError``,
                # generic transport errors, etc.) bypasses the retry
                # loop — it's a real failure, not a readiness signal.
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

            # Sleep, but don't overshoot the deadline.
            sleep_for = min(
                self._config.spawn_ready_poll_interval_s,
                max(0.0, deadline - now),
            )
            await asyncio.sleep(sleep_for)

    async def _send_assign_once(
        self,
        task: TaskAssignPayload,
        sub_agent_sandbox_id: str,
    ) -> NMBMessage:
        """One ``request`` attempt.  Returns the raw reply or raises.

        Separated out so the retry loop in
        :meth:`_send_assign_with_readiness_retry` can distinguish
        ``TARGET_OFFLINE`` (retry) from other ``NMBConnectionError``
        causes (propagate) without entangling the reply-validation
        path.
        """
        timeout = self._config.task_timeout_s
        if timeout is None:
            timeout = DEFAULT_NMB_DEFAULT_REQUEST_TIMEOUT
        return await self._bus.request(
            to=sub_agent_sandbox_id,
            type=TASK_ASSIGN,
            payload=dump(task),
            timeout=timeout,
        )

    def _interpret_reply(self, reply: NMBMessage) -> TaskCompletePayload:
        """Validate a raw NMB reply and return a typed ``TaskCompletePayload``.

        Pulled out of :meth:`_send_assign_with_readiness_retry` so
        the retry loop only handles delivery; reply interpretation
        runs once, after a successful send.

        Raises:
            DelegationError: On a ``task.error`` reply, validation
                failure, or unexpected reply type.
        """
        if reply.type == TASK_COMPLETE:
            try:
                return load(TaskCompletePayload, TASK_COMPLETE, reply.payload)
            except PayloadValidationError as exc:
                raise DelegationError(
                    f"task.complete payload validation failed: {exc}",
                ) from exc

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
            # Pin per-process runtime values so the child connects
            # with the same identity and workspace we assign here.
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
    string.  A more typed signal would be a nice cleanup, but it's
    not worth blocking the readiness fix on.
    """
    return "TARGET_OFFLINE" in str(exc)
