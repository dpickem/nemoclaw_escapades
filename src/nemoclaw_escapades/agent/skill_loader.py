"""Skill loader — scan and load SKILL.md files for task-specific guidance.

Implements the basic skill loading described in design_m2a.md §7.  Skills
are Markdown files (``SKILL.md``) with optional YAML frontmatter that
provide task-specific instructions the agent can load on demand.

The loader scans a directory tree for ``SKILL.md`` files at startup,
parses their frontmatter (``name``, ``description``), and makes them
available by ID.  The ``skill`` tool (``tools/skill.py``) calls
``load()`` to inject skill content into the conversation as a tool
result.

**Skill IDs** are derived from the relative path between the skills root
and the SKILL.md file's parent directory, with ``/`` replaced by ``-``.
This guarantees uniqueness (filesystem paths are unique) and remains
stable across rescans::

    skills/code-review/SKILL.md       → "code-review"
    skills/coding/review/SKILL.md     → "coding-review"
    skills/nested/deep/task/SKILL.md  → "nested-deep-task"

**Hot reload** — call ``rescan()`` to re-discover skills at runtime
without restarting.  New files are picked up, deleted files are dropped,
and changed frontmatter is re-parsed.

**Scope boundary** — M2a implements only reading and loading.  Auto-skill
creation (``skillify``), progressive disclosure, and template substitution
are deferred to M6.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("agent.skill_loader")

# Frontmatter delimiter used by SKILL.md files.
_FRONTMATTER_DELIMITER: str = "---"

# Maximum skill file size to prevent accidentally loading huge files.
_MAX_SKILL_SIZE_BYTES: int = 64_000

# Separator used to join path segments into a flat skill ID.
_ID_SEPARATOR: str = "-"


@dataclass(frozen=True)
class SkillInfo:
    """Metadata about a discovered skill.

    Attributes:
        skill_id: Unique identifier derived from the relative path
            between the skills root and the SKILL.md parent directory.
        name: Human-readable name from frontmatter, or the skill ID.
        description: Short description from frontmatter, or empty string.
        path: Absolute path to the SKILL.md file.
    """

    skill_id: str
    name: str
    description: str
    path: str


class SkillLoader:
    """Scan a directory tree for SKILL.md files and load them by ID.

    The loader discovers skills at construction time and caches their
    metadata.  Skill content is read on demand (not cached) so changes
    to skill files are picked up without restarting.  Call ``rescan()``
    to pick up new or deleted skill files at runtime.

    Attributes:
        skills_dir: Root directory to scan for skills.
        skills: Mapping of skill ID to ``SkillInfo``.
    """

    def __init__(self, skills_dir: str) -> None:
        """Initialise the loader and scan for available skills.

        Args:
            skills_dir: Absolute path to the directory containing skill
                subdirectories, each with a ``SKILL.md`` file.
        """
        self.skills_dir = skills_dir
        self.skills: dict[str, SkillInfo] = {}
        self._scan()

    def rescan(self) -> int:
        """Re-scan the skills directory to pick up changes at runtime.

        Clears all cached metadata and re-discovers SKILL.md files.
        New files are picked up, deleted files are dropped, and changed
        frontmatter is re-parsed.

        Returns:
            The number of skills discovered in the fresh scan.
        """
        self.skills.clear()
        self._scan()
        return len(self.skills)

    def _scan(self) -> None:
        """Discover SKILL.md files and parse their frontmatter.

        Skill IDs are derived from the relative path between the skills
        root and the SKILL.md file's parent directory.  Path segments
        are joined with ``-`` to produce a flat, unique identifier.
        """
        root = Path(self.skills_dir)
        if not root.is_dir():
            logger.warning("Skills directory not found", extra={"path": self.skills_dir})
            return

        for skill_file in sorted(root.rglob("SKILL.md")):
            skill_id = _derive_skill_id(root, skill_file)

            if skill_id in self.skills:
                logger.warning(
                    "Duplicate skill ID — skipping",
                    extra={
                        "skill_id": skill_id,
                        "existing": self.skills[skill_id].path,
                        "duplicate": str(skill_file),
                    },
                )
                continue

            try:
                raw = skill_file.read_text()
            except OSError:
                logger.warning("Failed to read skill file", extra={"path": str(skill_file)})
                continue

            name, description = _parse_frontmatter(raw, fallback_name=skill_id)

            info = SkillInfo(
                skill_id=skill_id,
                name=name,
                description=description,
                path=str(skill_file),
            )
            self.skills[skill_id] = info
            logger.info(
                "Discovered skill",
                extra={"skill_id": skill_id, "name": name, "path": str(skill_file)},
            )

        logger.info("Skill scan complete", extra={"count": len(self.skills)})

    def load(self, skill_id: str) -> str:
        """Load a skill's full content by ID.

        Reads the file on demand (not from cache) so edits to skill
        files are picked up without restarting.

        Args:
            skill_id: The skill identifier.

        Returns:
            The full skill content including frontmatter.

        Raises:
            KeyError: If no skill with the given ID exists.
            OSError: If the file cannot be read.
        """
        info = self.skills.get(skill_id)
        if info is None:
            raise KeyError(f"Unknown skill: {skill_id!r}. Available: {self.available_ids}")

        path = Path(info.path)
        size = path.stat().st_size
        if size > _MAX_SKILL_SIZE_BYTES:
            logger.warning(
                "Skill file exceeds size limit",
                extra={"skill_id": skill_id, "size": size, "limit": _MAX_SKILL_SIZE_BYTES},
            )

        return path.read_text()

    @property
    def available_ids(self) -> list[str]:
        """Sorted list of all discovered skill IDs."""
        return sorted(self.skills)

    @property
    def available_skills_summary(self) -> str:
        """Human-readable summary of available skills for prompt injection.

        Returns:
            A bullet list of skill IDs with descriptions.
        """
        if not self.skills:
            return "No skills available."

        lines: list[str] = []
        for sid in sorted(self.skills):
            info = self.skills[sid]
            desc = f" — {info.description}" if info.description else ""
            lines.append(f"- {sid}{desc}")

        return "\n".join(lines)


def _derive_skill_id(root: Path, skill_file: Path) -> str:
    """Derive a unique skill ID from the relative path.

    Uses the relative path between *root* and the SKILL.md file's
    parent directory, joining segments with ``-``.  If the SKILL.md
    sits directly in *root* (no parent subdirectory), the filename
    stem is used as a fallback.

    Examples::

        root = /skills
        /skills/code-review/SKILL.md        → "code-review"
        /skills/coding/review/SKILL.md      → "coding-review"
        /skills/SKILL.md                    → "SKILL"

    Args:
        root: The skills root directory.
        skill_file: Absolute path to the SKILL.md file.

    Returns:
        A flat, unique skill identifier string.
    """
    try:
        rel = skill_file.parent.relative_to(root)
    except ValueError:
        return skill_file.stem

    parts = rel.parts
    if not parts or parts == (".",):
        return skill_file.stem

    return _ID_SEPARATOR.join(parts)


def _parse_frontmatter(raw: str, fallback_name: str) -> tuple[str, str]:
    """Extract name and description from optional YAML frontmatter.

    Supports the standard ``---``-delimited format::

        ---
        name: Code Review
        description: Perform a structured code review
        ---

    Args:
        raw: The full file content.
        fallback_name: Name to use if frontmatter doesn't specify one.

    Returns:
        A (name, description) tuple.
    """
    name = fallback_name
    description = ""

    stripped = raw.strip()
    if not stripped.startswith(_FRONTMATTER_DELIMITER):
        return name, description

    lines = stripped.split("\n")
    end_idx = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER_DELIMITER:
            end_idx = i
            break

    if end_idx < 0:
        return name, description

    yaml_block = "\n".join(lines[1:end_idx])
    try:
        data: Any = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return name, description

    if isinstance(data, dict):
        name = str(data.get("name", fallback_name))
        description = str(data.get("description", ""))

    return name, description
