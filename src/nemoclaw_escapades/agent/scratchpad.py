"""Agent scratchpad — core domain object for working memory.

This module contains the ``Scratchpad`` class, which owns all
file-backed storage logic: creating the backing file, reading,
writing, truncation, section-aware appending, and formatting for
context injection.  It has **no knowledge of the tool system**;
the thin tool-adapter layer lives in ``tools.scratchpad``.

The scratchpad is a Markdown file on the sandbox filesystem that the
agent uses to record observations, track plans, note open questions,
and document decisions.  Two key properties:

1. **Injected into context** — ``AgentLoop`` appends the scratchpad
   contents to the system prompt on every inference round, so the
   model always has access to its latest notes.
2. **Returned to the orchestrator** — on task completion, the
   scratchpad is included in the ``task.complete`` payload via
   ``AgentLoopResult.scratchpad_contents``, giving the orchestrator
   visibility into the sub-agent's reasoning.

The scratchpad is ephemeral (per-task).  Persistent cross-session
memory is deferred to M5+.
"""

from __future__ import annotations

from pathlib import Path

from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("agent.scratchpad")

# 32 KiB keeps scratchpad content comfortably within a single context
# window while still leaving room for a multi-step plan.
_DEFAULT_MAX_SIZE: int = 32_768


class Scratchpad:
    """File-backed working memory for a single agent task.

    Attributes:
        path: Absolute path to the scratchpad Markdown file.
        max_size: Maximum file size in bytes.  Writes that would
            exceed this limit are truncated with a warning.
    """

    def __init__(self, path: str, max_size: int = _DEFAULT_MAX_SIZE) -> None:
        """Initialise the scratchpad.

        Creates the file if it doesn't exist.

        Args:
            path: Absolute filesystem path for the scratchpad file.
            max_size: Maximum allowed file size in bytes.  Prevents
                context window bloat from an over-enthusiastic agent.
        """
        self.path = path
        self.max_size = max_size

        # Ensure the file exists so read() never fails on a fresh task.
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("")

    def read(self) -> str:
        """Read current scratchpad contents.

        Returns:
            The full scratchpad text, or an empty string if the file
            is missing or unreadable.
        """
        try:
            return Path(self.path).read_text()
        except OSError:
            return ""

    def write(self, content: str) -> str:
        """Overwrite the scratchpad with new content.

        Truncates to ``max_size`` if the content exceeds the limit.

        Args:
            content: Full replacement text (Markdown).

        Returns:
            Confirmation message, with a warning if truncated.
        """
        truncated = False
        encoded = content.encode()
        if len(encoded) > self.max_size:
            # Slice bytes, not characters — multi-byte UTF-8 (emoji, CJK)
            # would overshoot max_size if we sliced the str by char count.
            # errors="ignore" drops any partial multi-byte char at the cut.
            content = encoded[: self.max_size].decode(errors="ignore")
            truncated = True

        try:
            Path(self.path).write_text(content)
        except OSError as exc:
            return f"Error writing scratchpad: {exc}"

        msg = f"Scratchpad updated ({len(content)} bytes)"
        if truncated:
            msg += f" — truncated to {self.max_size} byte limit"
            logger.warning("Scratchpad truncated", extra={"max_size": self.max_size})

        return msg

    def append(self, section: str, content: str) -> str:
        """Append content under a named section header.

        If the section already exists, content is appended after the
        last line of that section.  If not, a new ``## section``
        header is created at the end.

        Args:
            section: Section name (rendered as ``## section``).
            content: Text to append under the section.

        Returns:
            Confirmation message.
        """
        current = self.read()
        header = f"## {section}"

        if header in current:
            lines = current.split("\n")
            insert_at = len(lines)
            header_found = False
            for i, line in enumerate(lines):
                if line.strip() == header:
                    header_found = True
                    continue
                if header_found and line.startswith("## "):
                    insert_at = i
                    break

            lines.insert(insert_at, content)
            current = "\n".join(lines)
        else:
            current += f"\n{header}\n{content}\n"

        return self.write(current)

    def snapshot(self) -> str:
        """Return contents for inclusion in task.complete payload.

        Equivalent to ``read()`` but named distinctly for clarity at
        the call site.

        Returns:
            The full scratchpad text.
        """
        return self.read()

    def context_block(self) -> str:
        """Format the scratchpad for injection into the system prompt.

        Returns:
            The scratchpad wrapped in ``<scratchpad>`` tags, or an
            empty string if the scratchpad is empty.
        """
        content = self.read()
        if not content.strip():
            return ""

        return f"<scratchpad>\n{content}\n</scratchpad>"
