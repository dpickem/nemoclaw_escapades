"""Typed Pydantic payloads for the orchestrator ↔ sub-agent NMB protocol.

The NMB transport (``nmb/models.py``) carries arbitrary
``dict[str, Any]`` payloads.  This module promotes the M2b ``task.*``
payloads from free-form JSON sketches to validated Pydantic models so:

- The orchestrator's NMB listener fails malformed payloads at the
  receive boundary with a single ``ValidationError``, rather than
  letting them cascade into the finalization flow as malformed text.
- The finalization model (``docs/design_m2b.md`` §7.1) consumes
  named fields out of ``TaskCompletePayload`` instead of LLM-parsing
  free-form text to extract the diff and notes path.
- Both sides of the wire import the same model definitions, so
  there is no way for orchestrator and sub-agent to drift.

Payload shapes follow the M2b §6.3 spec verbatim (model summary
table in body; full per-field source in Appendix E).

Wire encoding
-------------

The transport (`nmb/models.py::NMBMessage.payload`) is a JSON dict.
:func:`dump` serialises a Pydantic payload to that dict;
:func:`load` validates a received dict against the matching model.
Both sides MUST round-trip through these helpers — never construct
``NMBMessage(payload=task.model_dump())`` directly, because the
default Pydantic dump emits ``None`` for unset Optional fields and
inflates the wire size.

See ``docs/design_m2b.md`` §6.3 (typed protocol payloads) and
Appendix E (full Pydantic source).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, ValidationError

# ── Message type strings on the wire ───────────────────────────────
#
# These are the values of ``NMBMessage.type`` for the payload models
# below.  Both sides import these constants instead of re-typing the
# strings, so a typo is a NameError at import time rather than a
# silent send-and-discard at runtime.

TASK_ASSIGN: str = "task.assign"
TASK_PROGRESS: str = "task.progress"
TASK_COMPLETE: str = "task.complete"
TASK_ERROR: str = "task.error"


# ── Workspace baseline ─────────────────────────────────────────────


class WorkspaceBaseline(BaseModel):
    """Git state the orchestrator pinned for a workflow.

    Carried on ``TaskAssignPayload`` and echoed back unchanged on
    ``TaskCompletePayload``.  Lets the orchestrator detect the
    rare-but-real case where a sub-agent ran ``git_checkout`` or
    ``git_reset`` mid-task and ended up rooted somewhere different
    from what was assigned.

    Attributes:
        repo_url: Canonical clone URL.  Distinguishes parallel agents
            on the same repo from agents on different repos.
        branch: Branch the workspace was checked out on.  Carried for
            audit / PR construction; ``base_sha`` is the actual
            baseline.
        base_sha: 40-char commit SHA the working tree was at when
            workspace seeding finished.  ``TaskCompletePayload.diff``
            is ``git diff <base_sha>..HEAD``.
        is_shallow: True for ``depth=1`` clones; recorded so
            finalisation knows whether ``git pull --rebase`` is safe
            or whether it needs to deepen first.
    """

    model_config = ConfigDict(extra="forbid")

    repo_url: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    base_sha: str = Field(min_length=1)
    is_shallow: bool = True


# ── Workspace context files ────────────────────────────────────────


class ContextFile(BaseModel):
    """Inline file the orchestrator wants seeded into the sub-agent's
    workspace before the run starts.

    Bounded by NMB's per-message-size cap; large repos still go
    through ``git_clone``.

    Attributes:
        path: Workspace-relative POSIX path.
        content: File contents (text or base64-encoded bytes).
        encoding: ``"utf-8"`` for text or ``"base64"`` for binary.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"


# ── Task assign (orchestrator → sub-agent) ─────────────────────────


class TaskAssignPayload(BaseModel):
    """Orchestrator → sub-agent.  Carries everything needed to run one task.

    Sent on the NMB ``task.assign`` message type.  The sub-agent's
    NMB receive loop validates the incoming payload through this model
    before constructing a per-task ``AgentLoopConfig`` and running the
    loop (``docs/design_m2b.md`` §6.2 Sub-Agent Entrypoint).

    Attributes:
        prompt: Natural-language task description.  Becomes the
            initial user message for the AgentLoop.
        workflow_id: UUID generated at delegation time.  Threads
            through every NMB message and audit record for the
            workflow.
        parent_sandbox_id: Orchestrator's sandbox identity.  Used by
            the sub-agent's ``AuditBuffer`` to attribute records back
            to the parent (Phase 3b).
        agent_id: Unique identifier for this sub-agent invocation.
            Used as the notes-file owner tag and as the per-agent
            workspace subdirectory name (``§4.2.1``).
        workspace_root: Absolute path the sub-agent operates in.
            File / bash / git tools treat this as their root.
        max_turns: Per-task cap on AgentLoop tool rounds.  Overrides
            ``cfg.agent_loop.max_tool_rounds`` for this delegation;
            ``None`` means "fall back to the global cap" (§6.4).
        model: Per-task model override.  Operationally a no-op in
            M2b's same-sandbox topology because the L7 proxy rewrites
            the model field, but recorded in the audit DB so M3's
            per-sandbox provider binding (Option D) can pick it up
            unchanged (§6.5).
        tool_surface: Optional explicit allowlist of tool names the
            sub-agent may use.  ``None`` means "use the role's
            default registry"; non-empty narrows it.
        context_files: Inline files seeded into the workspace before
            the run starts.  Bounded by NMB message-size cap; large
            repos still use ``git_clone``.
        workspace_baseline: Git state the diff in
            ``TaskCompletePayload.diff`` is computed against.
            ``None`` means "no git repo, the task isn't expected to
            produce a diff" (§6.6).
        is_iteration: True when this assignment is a ``re_delegate``
            follow-up after a finalization-driven iteration.
        iteration_number: 0 for the first assignment in a workflow,
            1 for the first iteration, etc.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    parent_sandbox_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    workspace_root: str = Field(min_length=1)
    max_turns: PositiveInt | None = None
    model: str | None = None
    tool_surface: list[str] | None = None
    context_files: list[ContextFile] = Field(default_factory=list)
    workspace_baseline: WorkspaceBaseline | None = None
    is_iteration: bool = False
    iteration_number: NonNegativeInt = 0


# ── Task progress (sub-agent → orchestrator, optional) ─────────────


class TaskProgressPayload(BaseModel):
    """Sub-agent → orchestrator.  Optional, periodic, best-effort.

    Sent on ``task.progress``.  Phase 3a may emit these from the
    sub-agent's loop wrapper at round boundaries; the orchestrator's
    Slack rendering picks them up to update a thinking indicator
    (Phase 6 polish row in §14).

    Attributes:
        workflow_id: Workflow this progress belongs to.
        status: Coarse progress phase the sub-agent is in.
        pct: Optional rough completion percentage (0-100).
        current_round: Tool round the sub-agent is on.
        tokens_used: Cumulative tokens consumed in this run.
        note: Human-readable line, e.g. "Created src/api/health.py".
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    status: Literal[
        "starting",
        "reading_workspace",
        "writing_code",
        "running_tests",
        "finalizing",
    ]
    pct: int | None = Field(default=None, ge=0, le=100)
    current_round: NonNegativeInt | None = None
    tokens_used: NonNegativeInt | None = None
    note: str | None = None


# ── Task complete (sub-agent → orchestrator, terminal success) ─────


class TaskCompletePayload(BaseModel):
    """Sub-agent → orchestrator.  Terminal success message.

    Sent on ``task.complete``.  The orchestrator's NMB listener
    validates this through Pydantic before handing off to the
    finalization flow (Phase 3b § 7.1).  Baseline drift is detected
    by comparing ``workspace_baseline`` against the value originally
    sent on ``TaskAssignPayload`` (§6.6.3).

    Attributes:
        workflow_id: Workflow this completion belongs to.
        summary: One-paragraph user-facing description of what was
            done.  The finalization step renders this verbatim into
            Slack.
        diff: Unified diff between
            ``workspace_baseline.base_sha`` (set on the matching
            ``TaskAssignPayload``) and the workspace's HEAD after
            the sub-agent's edits.  Empty when the task didn't
            modify any tracked files or when the assigned baseline
            was ``None``.
        workspace_baseline: Echoed verbatim from the matching
            ``TaskAssignPayload``.  Lets the orchestrator detect
            mid-task ``git_checkout`` drift.  ``None`` only when
            the assigned baseline was also ``None``.
        files_changed: Workspace-relative paths.  Convenience field —
            ``diff`` is the source of truth.
        notes_path: Workspace-relative path to the agent's
            scratchpad (``notes-<task-slug>-<agent-id>.md``), if it
            created one.  M2a's scratchpad skill makes this
            optional, not mandatory.
        git_commit_sha: New SHA on top of
            ``workspace_baseline.base_sha`` if the sub-agent ran
            ``git_commit``.  Lets the orchestrator's
            ``push_and_create_pr`` push the range
            ``<base_sha>..<git_commit_sha>``.
        tool_calls_made: Total tool invocations in this run.
        rounds_used: Number of inference calls made.  Always
            ``rounds_used <= effective_max_turns``.
        model_used: Model name the sub-agent's ``AgentLoop`` set on
            the chat-completions request body — i.e. the *requested*
            model.  In M2b the L7 proxy may have rewritten this
            before reaching the upstream (§6.5.1); recorded here so
            the audit row matches what the protocol-level
            scaffolding said happened.
        suggested_next_step: Optional hint that more work is needed.
            The finalization model may use this to proactively call
            ``re_delegate``.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    diff: str = ""
    workspace_baseline: WorkspaceBaseline | None = None
    files_changed: list[str] = Field(default_factory=list)
    notes_path: str | None = None
    git_commit_sha: str | None = None
    tool_calls_made: NonNegativeInt = 0
    rounds_used: NonNegativeInt = 0
    model_used: str | None = None
    suggested_next_step: str | None = None


# ── Task error (sub-agent → orchestrator, terminal failure) ────────


class TaskErrorPayload(BaseModel):
    """Sub-agent → orchestrator.  Terminal failure message.

    Sent on ``task.error``.  Distinguished from ``task.complete``
    because finalisation tools branch on success vs failure (a failed
    task with a partial diff still needs the orchestrator to surface
    the error to the user, but doesn't go down the
    ``push_and_create_pr`` path).

    Attributes:
        workflow_id: Workflow this error belongs to.
        error: Human-readable failure description.
        error_kind: Structured failure category.  ``recoverable``
            errors (``max_turns_exceeded``) are candidates for
            ``re_delegate``; the rest surface to the user.
        recoverable: Whether the orchestrator's finalization model
            may attempt ``re_delegate`` with adjusted parameters.
        notes_path: Workspace-relative path to whatever partial work
            the sub-agent produced before the error.
        traceback: Optional Python traceback string for forensic
            diagnosis.  Stored in audit; never rendered to the user.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    error: str = Field(min_length=1)
    error_kind: Literal[
        "max_turns_exceeded",
        "tool_failure",
        "policy_denied",
        "inference_error",
        "other",
    ]
    recoverable: bool = False
    notes_path: str | None = None
    traceback: str | None = None


# ── Codec helpers ──────────────────────────────────────────────────


class PayloadValidationError(Exception):
    """Raised when an inbound NMB payload fails Pydantic validation.

    The wrapped :class:`pydantic.ValidationError` is preserved on
    ``__cause__`` for callers that need the per-field error list;
    the ``str`` form is a single line suitable for a structured-log
    field.

    Attributes:
        payload_type: NMB message type string (e.g. ``"task.assign"``)
            that failed validation.  Lets the orchestrator's NMB
            listener log a single structured field instead of
            scraping the exception message.
    """

    def __init__(self, payload_type: str, message: str) -> None:
        super().__init__(f"{payload_type}: {message}")
        self.payload_type = payload_type


def dump(payload: BaseModel) -> dict[str, Any]:
    """Serialise a typed payload to a wire dict.

    Calls ``model_dump(mode="json")`` so enum-like fields land as
    their string representations and ``None`` values are dropped —
    matching ``NMBMessage.to_json``'s ``exclude_none=True`` behaviour
    so the wire bytes are byte-identical regardless of which side
    composed the message.

    Args:
        payload: A typed payload instance.

    Returns:
        A JSON-serialisable dict suitable for assignment to
        ``NMBMessage.payload``.
    """
    return payload.model_dump(mode="json", exclude_none=True)


def load[T: BaseModel](model: type[T], payload_type: str, raw: dict[str, Any] | None) -> T:
    """Validate a received NMB payload against a typed model.

    Args:
        model: The Pydantic model class to validate against.
        payload_type: The NMB ``type`` string the message arrived on.
            Used purely for the error message — the dispatcher already
            picked the right ``model`` based on this string.
        raw: The raw ``NMBMessage.payload`` dict (or ``None`` for an
            empty payload, which always fails validation since every
            payload model in this module has at least one required
            field).

    Returns:
        A validated instance of ``model``.

    Raises:
        PayloadValidationError: If the payload doesn't conform to
            ``model``'s schema.  The original
            :class:`pydantic.ValidationError` is on ``__cause__``.
    """
    if raw is None:
        raise PayloadValidationError(payload_type, "payload is None")
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise PayloadValidationError(payload_type, str(exc)) from exc


__all__ = [
    "TASK_ASSIGN",
    "TASK_COMPLETE",
    "TASK_ERROR",
    "TASK_PROGRESS",
    "ContextFile",
    "PayloadValidationError",
    "TaskAssignPayload",
    "TaskCompletePayload",
    "TaskErrorPayload",
    "TaskProgressPayload",
    "WorkspaceBaseline",
    "dump",
    "load",
]
