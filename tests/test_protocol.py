"""Tests for ``nmb/protocol.py`` — typed payloads + codec helpers.

Three things to verify per model:

1. **Roundtrip** — ``dump(...)`` → ``load(...)`` recovers the same
   instance, with optional fields elided when unset (matching
   ``NMBMessage.to_json``'s ``exclude_none=True`` so the wire is
   byte-identical regardless of which side composed it).
2. **Validation rejects bad payloads** — missing required fields,
   wrong types, out-of-range values all raise
   ``PayloadValidationError`` with the original
   :class:`pydantic.ValidationError` chained as ``__cause__``.
3. **Forbidden extras** — ``model_config = ConfigDict(extra="forbid")``
   on every payload model means an unknown field raises rather than
   silently lands in the audit DB.

These tests are pure protocol-level — they don't open an
``NMBMessage`` or talk to a broker.  Wire-level transport is
covered by ``tests/integration/test_lifecycle.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nemoclaw_escapades.nmb.protocol import (
    ContextFile,
    PayloadValidationError,
    TaskAssignPayload,
    TaskCompletePayload,
    TaskErrorPayload,
    TaskProgressPayload,
    WorkspaceBaseline,
    dump,
    load,
)

# Reusable fixtures: minimal valid payload dicts, one per model.
# Each test that validates a "valid" case starts from one of these
# and only adds the fields under test, so no test rebuilds the full
# required-field set.

_BASELINE: dict[str, object] = {
    "repo_url": "https://gitlab.example.com/acme/demo.git",
    "branch": "main",
    "base_sha": "deadbeef" * 5,  # 40-char dummy SHA
}

_ASSIGN_REQUIRED: dict[str, object] = {
    "prompt": "Add /api/health endpoint",
    "workflow_id": "wf-abc123",
    "parent_sandbox_id": "orchestrator-sandbox",
    "agent_id": "coding-12345678",
    "workspace_root": "/sandbox/workspace/agent-12345678",
}

_COMPLETE_REQUIRED: dict[str, object] = {
    "workflow_id": "wf-abc123",
    "summary": "Added a /api/health endpoint returning 200 OK.",
}

_ERROR_REQUIRED: dict[str, object] = {
    "workflow_id": "wf-abc123",
    "error": "Tool round limit exceeded",
    "error_kind": "max_turns_exceeded",
}

_PROGRESS_REQUIRED: dict[str, object] = {
    "workflow_id": "wf-abc123",
    "status": "writing_code",
}


# ── WorkspaceBaseline ──────────────────────────────────────────────


class TestWorkspaceBaseline:
    """Smallest payload model — exercises the basic roundtrip path."""

    def test_roundtrip(self) -> None:
        baseline = WorkspaceBaseline(**_BASELINE)
        assert load(WorkspaceBaseline, "test", dump(baseline)) == baseline

    def test_default_is_shallow_true(self) -> None:
        baseline = WorkspaceBaseline(**_BASELINE)
        assert baseline.is_shallow is True

    def test_dump_excludes_unset_optionals(self) -> None:
        # ``is_shallow`` defaults to True so it's never ``None`` and
        # always lands in the dump — but no other optional fields
        # exist on this model, so this test is mostly a guard against
        # future-Daniel adding an Optional field and forgetting to
        # check the wire-size invariant.
        wire = dump(WorkspaceBaseline(**_BASELINE))
        assert wire == {**_BASELINE, "is_shallow": True}

    def test_required_fields_validate(self) -> None:
        for missing in ("repo_url", "branch", "base_sha"):
            partial = {k: v for k, v in _BASELINE.items() if k != missing}
            with pytest.raises(PayloadValidationError) as excinfo:
                load(WorkspaceBaseline, "task.assign", partial)
            # The PayloadValidationError tag is the message-type string
            # we passed in, so structured logs can group failures.
            assert excinfo.value.payload_type == "task.assign"

    def test_extra_fields_rejected(self) -> None:
        bad = {**_BASELINE, "branch_pinned_at": "2026-04-27"}
        with pytest.raises(PayloadValidationError):
            load(WorkspaceBaseline, "task.assign", bad)


# ── ContextFile ────────────────────────────────────────────────────


class TestContextFile:
    def test_default_encoding_is_utf8(self) -> None:
        cf = ContextFile(path="README.md", content="hello\n")
        assert cf.encoding == "utf-8"

    def test_base64_encoding_accepted(self) -> None:
        cf = ContextFile(path="logo.png", content="aGVsbG8=", encoding="base64")
        assert load(ContextFile, "task.assign", dump(cf)) == cf

    def test_invalid_encoding_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ContextFile(path="x", content="y", encoding="utf-7")  # type: ignore[arg-type]

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ContextFile(path="", content="anything")


# ── TaskAssignPayload ──────────────────────────────────────────────


class TestTaskAssignPayload:
    def test_minimal_roundtrip(self) -> None:
        payload = TaskAssignPayload(**_ASSIGN_REQUIRED)  # type: ignore[arg-type]
        recovered = load(TaskAssignPayload, "task.assign", dump(payload))
        assert recovered == payload

    def test_full_roundtrip_with_baseline_and_context(self) -> None:
        payload = TaskAssignPayload(
            **_ASSIGN_REQUIRED,  # type: ignore[arg-type]
            max_turns=20,
            model="azure/anthropic/claude-haiku-4",
            tool_surface=["read_file", "write_file"],
            context_files=[ContextFile(path="README.md", content="x")],
            workspace_baseline=WorkspaceBaseline(**_BASELINE),  # type: ignore[arg-type]
            is_iteration=True,
            iteration_number=2,
        )
        recovered = load(TaskAssignPayload, "task.assign", dump(payload))
        assert recovered == payload

    def test_dump_omits_unset_optionals(self) -> None:
        # Wire-size invariant: optional fields that weren't set
        # shouldn't bloat the payload.  ``exclude_none=True`` in
        # ``dump`` is the mechanism that enforces this.
        payload = TaskAssignPayload(**_ASSIGN_REQUIRED)  # type: ignore[arg-type]
        wire = dump(payload)
        for unset in ("max_turns", "model", "tool_surface", "workspace_baseline"):
            assert unset not in wire, f"{unset} should not appear in wire when unset"

    def test_default_is_iteration_false_and_iteration_number_zero(self) -> None:
        payload = TaskAssignPayload(**_ASSIGN_REQUIRED)  # type: ignore[arg-type]
        assert payload.is_iteration is False
        assert payload.iteration_number == 0
        assert payload.context_files == []

    def test_max_turns_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            TaskAssignPayload(**_ASSIGN_REQUIRED, max_turns=0)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            TaskAssignPayload(**_ASSIGN_REQUIRED, max_turns=-5)  # type: ignore[arg-type]

    def test_negative_iteration_number_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TaskAssignPayload(**_ASSIGN_REQUIRED, iteration_number=-1)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "missing",
        ["prompt", "workflow_id", "parent_sandbox_id", "agent_id", "workspace_root"],
    )
    def test_required_fields_validate(self, missing: str) -> None:
        partial = {k: v for k, v in _ASSIGN_REQUIRED.items() if k != missing}
        with pytest.raises(PayloadValidationError):
            load(TaskAssignPayload, "task.assign", partial)

    def test_empty_prompt_rejected(self) -> None:
        # min_length=1 on prompt — a delegation with no work to do
        # is a logic error, not a normal input.
        bad = {**_ASSIGN_REQUIRED, "prompt": ""}
        with pytest.raises(PayloadValidationError):
            load(TaskAssignPayload, "task.assign", bad)

    def test_extra_fields_rejected(self) -> None:
        bad = {**_ASSIGN_REQUIRED, "priority": "high"}
        with pytest.raises(PayloadValidationError):
            load(TaskAssignPayload, "task.assign", bad)

    def test_payload_type_carried_on_validation_error(self) -> None:
        # The orchestrator's NMB listener stamps the message type on
        # every validation error so structured logs can group bad
        # payloads by the message kind that delivered them.
        with pytest.raises(PayloadValidationError) as excinfo:
            load(TaskAssignPayload, "task.assign", {"prompt": "x"})
        assert excinfo.value.payload_type == "task.assign"
        assert isinstance(excinfo.value.__cause__, ValidationError)


# ── TaskCompletePayload ────────────────────────────────────────────


class TestTaskCompletePayload:
    def test_minimal_roundtrip(self) -> None:
        payload = TaskCompletePayload(**_COMPLETE_REQUIRED)  # type: ignore[arg-type]
        assert load(TaskCompletePayload, "task.complete", dump(payload)) == payload

    def test_full_roundtrip(self) -> None:
        payload = TaskCompletePayload(
            **_COMPLETE_REQUIRED,  # type: ignore[arg-type]
            diff="diff --git a/x b/x\n",
            workspace_baseline=WorkspaceBaseline(**_BASELINE),  # type: ignore[arg-type]
            files_changed=["src/api/health.py"],
            notes_path="notes-add-health-endpoint-12345678.md",
            git_commit_sha="cafef00d" * 5,
            tool_calls_made=12,
            rounds_used=4,
            model_used="azure/anthropic/claude-opus-4-6",
            suggested_next_step="Add coverage for the unhealthy state.",
        )
        recovered = load(TaskCompletePayload, "task.complete", dump(payload))
        assert recovered == payload

    def test_default_diff_empty_string(self) -> None:
        # Empty diff is meaningful: "task didn't modify any tracked
        # files" (e.g. summarise this repo).  Distinct from None.
        payload = TaskCompletePayload(**_COMPLETE_REQUIRED)  # type: ignore[arg-type]
        assert payload.diff == ""

    def test_empty_summary_rejected(self) -> None:
        bad = {**_COMPLETE_REQUIRED, "summary": ""}
        with pytest.raises(PayloadValidationError):
            load(TaskCompletePayload, "task.complete", bad)


# ── TaskErrorPayload ───────────────────────────────────────────────


class TestTaskErrorPayload:
    def test_roundtrip(self) -> None:
        payload = TaskErrorPayload(**_ERROR_REQUIRED)  # type: ignore[arg-type]
        recovered = load(TaskErrorPayload, "task.error", dump(payload))
        assert recovered == payload

    def test_recoverable_default_false(self) -> None:
        payload = TaskErrorPayload(**_ERROR_REQUIRED)  # type: ignore[arg-type]
        assert payload.recoverable is False

    def test_with_traceback(self) -> None:
        payload = TaskErrorPayload(
            **_ERROR_REQUIRED,  # type: ignore[arg-type]
            recoverable=True,
            notes_path="notes-foo.md",
            traceback="Traceback (most recent call last):\n  ...",
        )
        recovered = load(TaskErrorPayload, "task.error", dump(payload))
        assert recovered == payload

    def test_invalid_error_kind_rejected(self) -> None:
        # ``error_kind`` is a Literal — the closed enum prevents
        # sub-agents from inventing new failure categories the
        # orchestrator's finalization model doesn't know how to
        # branch on.
        bad = {**_ERROR_REQUIRED, "error_kind": "explosion"}
        with pytest.raises(PayloadValidationError):
            load(TaskErrorPayload, "task.error", bad)

    @pytest.mark.parametrize(
        "missing",
        ["workflow_id", "error", "error_kind"],
    )
    def test_required_fields_validate(self, missing: str) -> None:
        partial = {k: v for k, v in _ERROR_REQUIRED.items() if k != missing}
        with pytest.raises(PayloadValidationError):
            load(TaskErrorPayload, "task.error", partial)


# ── TaskProgressPayload ────────────────────────────────────────────


class TestTaskProgressPayload:
    def test_roundtrip(self) -> None:
        payload = TaskProgressPayload(**_PROGRESS_REQUIRED)  # type: ignore[arg-type]
        assert load(TaskProgressPayload, "task.progress", dump(payload)) == payload

    def test_with_optional_fields(self) -> None:
        payload = TaskProgressPayload(
            **_PROGRESS_REQUIRED,  # type: ignore[arg-type]
            pct=42,
            current_round=3,
            tokens_used=12_345,
            note="Created src/api/health.py",
        )
        recovered = load(TaskProgressPayload, "task.progress", dump(payload))
        assert recovered == payload

    @pytest.mark.parametrize("pct", [-1, 101])
    def test_pct_out_of_range_rejected(self, pct: int) -> None:
        with pytest.raises(ValidationError):
            TaskProgressPayload(**_PROGRESS_REQUIRED, pct=pct)  # type: ignore[arg-type]

    def test_invalid_status_rejected(self) -> None:
        bad = {**_PROGRESS_REQUIRED, "status": "thinking"}
        with pytest.raises(PayloadValidationError):
            load(TaskProgressPayload, "task.progress", bad)


# ── Codec edge cases ───────────────────────────────────────────────


class TestCodec:
    def test_load_rejects_none_payload(self) -> None:
        # NMB allows ``payload`` to be None in some op kinds; the
        # task.* payloads never should be empty, so we surface that
        # as a validation error instead of letting a None propagate.
        with pytest.raises(PayloadValidationError) as excinfo:
            load(TaskAssignPayload, "task.assign", None)
        assert "None" in str(excinfo.value)

    def test_dump_strips_unset_nested_optionals(self) -> None:
        # The wire-size invariant must hold transitively: a nested
        # ``WorkspaceBaseline`` with ``is_shallow=True`` (the default)
        # should still appear in the dump because the field is set
        # to its default (not None), but a parent's None
        # ``workspace_baseline`` should be omitted entirely.
        with_baseline = TaskAssignPayload(
            **_ASSIGN_REQUIRED,  # type: ignore[arg-type]
            workspace_baseline=WorkspaceBaseline(**_BASELINE),  # type: ignore[arg-type]
        )
        without_baseline = TaskAssignPayload(**_ASSIGN_REQUIRED)  # type: ignore[arg-type]
        assert "workspace_baseline" in dump(with_baseline)
        assert "workspace_baseline" not in dump(without_baseline)
