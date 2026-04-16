"""Tests for the skill loader and skill tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nemoclaw_escapades.agent.skill_loader import (
    SkillLoader,
    _derive_skill_id,
    _parse_frontmatter,
)
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.skill import register_skill_tool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """Create a temporary skills directory with sample SKILL.md files."""
    skill_a = tmp_path / "code-review"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text(
        "---\n"
        "name: Code Review\n"
        "description: Perform a structured code review\n"
        "---\n\n"
        "# Code Review Skill\n\n"
        "Review the code for correctness and style.\n"
    )

    skill_b = tmp_path / "debugging"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text(
        "# Debugging Skill\n\nSystematically debug the reported issue.\n"
    )

    skill_c = tmp_path / "refactor"
    skill_c.mkdir()
    (skill_c / "SKILL.md").write_text(
        "---\n"
        "name: Refactoring\n"
        "---\n\n"
        "# Refactoring Skill\n\n"
        "Improve code structure without changing behaviour.\n"
    )

    return tmp_path


@pytest.fixture
def loader(skills_dir: Path) -> SkillLoader:
    return SkillLoader(str(skills_dir))


# ---------------------------------------------------------------------------
# SkillLoader — scanning
# ---------------------------------------------------------------------------


class TestSkillLoaderScan:
    """Skill directory scanning and metadata extraction."""

    def test_discovers_all_skills(self, loader: SkillLoader) -> None:
        assert len(loader.skills) == 3

    def test_available_ids_sorted(self, loader: SkillLoader) -> None:
        ids = loader.available_ids
        assert ids == sorted(ids)
        assert "code-review" in ids
        assert "debugging" in ids
        assert "refactor" in ids

    def test_frontmatter_name_extracted(self, loader: SkillLoader) -> None:
        info = loader.skills["code-review"]
        assert info.name == "Code Review"

    def test_frontmatter_description_extracted(self, loader: SkillLoader) -> None:
        info = loader.skills["code-review"]
        assert info.description == "Perform a structured code review"

    def test_no_frontmatter_uses_dir_name(self, loader: SkillLoader) -> None:
        info = loader.skills["debugging"]
        assert info.name == "debugging"
        assert info.description == ""

    def test_partial_frontmatter(self, loader: SkillLoader) -> None:
        info = loader.skills["refactor"]
        assert info.name == "Refactoring"
        assert info.description == ""

    def test_empty_directory(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty_skills"
        empty.mkdir()
        loader = SkillLoader(str(empty))
        assert len(loader.skills) == 0

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        loader = SkillLoader(str(tmp_path / "does_not_exist"))
        assert len(loader.skills) == 0

    def test_path_stored_correctly(self, loader: SkillLoader, skills_dir: Path) -> None:
        info = loader.skills["code-review"]
        assert info.path == str(skills_dir / "code-review" / "SKILL.md")


# ---------------------------------------------------------------------------
# SkillLoader — unique IDs from relative paths
# ---------------------------------------------------------------------------


class TestSkillIdDerivation:
    """Skill IDs derived from relative path segments."""

    def test_single_level_id(self, tmp_path: Path) -> None:
        (tmp_path / "my-skill").mkdir()
        (tmp_path / "my-skill" / "SKILL.md").write_text("# Skill")
        loader = SkillLoader(str(tmp_path))
        assert "my-skill" in loader.skills

    def test_nested_id_joins_segments(self, tmp_path: Path) -> None:
        nested = tmp_path / "coding" / "review"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("# Nested Skill")
        loader = SkillLoader(str(tmp_path))
        assert "coding-review" in loader.skills

    def test_deeply_nested_id(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "SKILL.md").write_text("# Deep")
        loader = SkillLoader(str(tmp_path))
        assert "a-b-c" in loader.skills

    def test_no_collision_same_leaf_name(self, tmp_path: Path) -> None:
        """Two dirs with the same leaf name at different depths get distinct IDs."""
        (tmp_path / "coding" / "review").mkdir(parents=True)
        (tmp_path / "coding" / "review" / "SKILL.md").write_text("# A")
        (tmp_path / "ops" / "review").mkdir(parents=True)
        (tmp_path / "ops" / "review" / "SKILL.md").write_text("# B")
        loader = SkillLoader(str(tmp_path))
        assert "coding-review" in loader.skills
        assert "ops-review" in loader.skills
        assert len(loader.skills) == 2

    def test_skill_in_root_uses_stem(self, tmp_path: Path) -> None:
        (tmp_path / "SKILL.md").write_text("# Root skill")
        loader = SkillLoader(str(tmp_path))
        assert "SKILL" in loader.skills


class TestDeriveSkillIdHelper:
    """Unit tests for the _derive_skill_id helper."""

    def test_single_level(self, tmp_path: Path) -> None:
        skill_file = tmp_path / "my-skill" / "SKILL.md"
        assert _derive_skill_id(tmp_path, skill_file) == "my-skill"

    def test_multi_level(self, tmp_path: Path) -> None:
        skill_file = tmp_path / "a" / "b" / "SKILL.md"
        assert _derive_skill_id(tmp_path, skill_file) == "a-b"

    def test_root_level(self, tmp_path: Path) -> None:
        skill_file = tmp_path / "SKILL.md"
        assert _derive_skill_id(tmp_path, skill_file) == "SKILL"


# ---------------------------------------------------------------------------
# SkillLoader — hot reload
# ---------------------------------------------------------------------------


class TestSkillLoaderRescan:
    """rescan() hot-reload picks up changes without restart."""

    def test_rescan_picks_up_new_skill(self, loader: SkillLoader, skills_dir: Path) -> None:
        assert "new-skill" not in loader.skills

        new_dir = skills_dir / "new-skill"
        new_dir.mkdir()
        (new_dir / "SKILL.md").write_text("# New Skill")
        count = loader.rescan()

        assert "new-skill" in loader.skills
        assert count == 4

    def test_rescan_drops_deleted_skill(self, loader: SkillLoader, skills_dir: Path) -> None:
        assert "debugging" in loader.skills
        (skills_dir / "debugging" / "SKILL.md").unlink()
        (skills_dir / "debugging").rmdir()

        loader.rescan()
        assert "debugging" not in loader.skills
        assert len(loader.skills) == 2

    def test_rescan_updates_frontmatter(self, loader: SkillLoader, skills_dir: Path) -> None:
        assert loader.skills["refactor"].name == "Refactoring"

        (skills_dir / "refactor" / "SKILL.md").write_text(
            "---\nname: Refactoring v2\ndescription: Updated\n---\n# Content"
        )
        loader.rescan()

        assert loader.skills["refactor"].name == "Refactoring v2"
        assert loader.skills["refactor"].description == "Updated"

    def test_rescan_returns_count(self, loader: SkillLoader) -> None:
        count = loader.rescan()
        assert count == 3


# ---------------------------------------------------------------------------
# SkillLoader — loading
# ---------------------------------------------------------------------------


class TestSkillLoaderLoad:
    """Skill content loading by ID."""

    def test_load_returns_full_content(self, loader: SkillLoader) -> None:
        content = loader.load("code-review")
        assert "Code Review Skill" in content
        assert "correctness" in content

    def test_load_unknown_skill_raises(self, loader: SkillLoader) -> None:
        with pytest.raises(KeyError, match="nonexistent"):
            loader.load("nonexistent")

    def test_load_reads_from_disk(self, loader: SkillLoader, skills_dir: Path) -> None:
        """Loading reads the file each time (not from cache)."""
        path = skills_dir / "debugging" / "SKILL.md"
        path.write_text("Updated content")
        content = loader.load("debugging")
        assert content == "Updated content"


# ---------------------------------------------------------------------------
# SkillLoader — summary
# ---------------------------------------------------------------------------


class TestSkillLoaderSummary:
    """Human-readable skill summary for prompt injection."""

    def test_summary_includes_all_skills(self, loader: SkillLoader) -> None:
        summary = loader.available_skills_summary
        assert "code-review" in summary
        assert "debugging" in summary
        assert "refactor" in summary

    def test_summary_includes_description(self, loader: SkillLoader) -> None:
        summary = loader.available_skills_summary
        assert "Perform a structured code review" in summary

    def test_empty_loader_summary(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        loader = SkillLoader(str(empty))
        assert loader.available_skills_summary == "No skills available."


# ---------------------------------------------------------------------------
# _parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    """YAML frontmatter extraction."""

    def test_full_frontmatter(self) -> None:
        raw = "---\nname: Test\ndescription: A test skill\n---\n# Content"
        name, desc = _parse_frontmatter(raw, "fallback")
        assert name == "Test"
        assert desc == "A test skill"

    def test_no_frontmatter(self) -> None:
        raw = "# Just a heading\nSome content."
        name, desc = _parse_frontmatter(raw, "fallback")
        assert name == "fallback"
        assert desc == ""

    def test_unclosed_frontmatter(self) -> None:
        raw = "---\nname: Broken\n# No closing delimiter"
        name, desc = _parse_frontmatter(raw, "fallback")
        assert name == "fallback"

    def test_quoted_values(self) -> None:
        raw = "---\nname: \"Quoted Name\"\ndescription: 'Single quoted'\n---\n"
        name, desc = _parse_frontmatter(raw, "fallback")
        assert name == "Quoted Name"
        assert desc == "Single quoted"


# ---------------------------------------------------------------------------
# Skill tool registration
# ---------------------------------------------------------------------------


class TestSkillTool:
    """The `skill` tool integration with ToolRegistry."""

    def test_registration_with_skills(self, loader: SkillLoader) -> None:
        registry = ToolRegistry()
        register_skill_tool(registry, loader)
        assert "skill" in registry

    def test_no_registration_without_skills(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        empty_loader = SkillLoader(str(empty))
        registry = ToolRegistry()
        register_skill_tool(registry, empty_loader)
        assert "skill" not in registry

    def test_tool_schema_has_enum(self, loader: SkillLoader) -> None:
        registry = ToolRegistry()
        register_skill_tool(registry, loader)
        spec = registry.get("skill")
        assert spec is not None
        enum_values = spec.input_schema["properties"]["skill_name"]["enum"]
        assert "code-review" in enum_values
        assert "debugging" in enum_values

    async def test_tool_loads_skill_content(self, loader: SkillLoader) -> None:
        registry = ToolRegistry()
        register_skill_tool(registry, loader)
        result = await registry.execute("skill", json.dumps({"skill_name": "code-review"}))
        assert "[Skill: code-review]" in result
        assert "Code Review Skill" in result

    async def test_tool_unknown_skill(self, loader: SkillLoader) -> None:
        registry = ToolRegistry()
        register_skill_tool(registry, loader)
        result = await registry.execute("skill", json.dumps({"skill_name": "nonexistent"}))
        assert "Error" in result

    def test_tool_is_read_only(self, loader: SkillLoader) -> None:
        registry = ToolRegistry()
        register_skill_tool(registry, loader)
        spec = registry.get("skill")
        assert spec is not None
        assert spec.is_read_only is True
