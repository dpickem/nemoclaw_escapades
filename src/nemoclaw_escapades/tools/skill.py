"""Skill tool — load task-specific SKILL.md guidance into the conversation.

Thin adapter between the ``SkillLoader`` (domain logic) and the
``ToolRegistry`` (tool system).  The ``skill`` tool lets the agent
request a specific skill by ID; the loader reads the file and returns
its content as a tool result, which the model sees on the next
inference round.

See design_m2a.md §7 for the skill loading design.
"""

from __future__ import annotations

from nemoclaw_escapades.agent.skill_loader import SkillLoader
from nemoclaw_escapades.tools.registry import ToolRegistry, tool

# Logical toolset name for the registry.
_TOOLSET: str = "skills"


def register_skill_tool(registry: ToolRegistry, loader: SkillLoader) -> None:
    """Register the ``skill`` tool with an enum of available skill IDs.

    The tool's parameter schema includes a dynamic ``enum`` list built
    from the loader's discovered skills so the model can only request
    valid skill IDs.

    Args:
        registry: The tool registry to populate.
        loader: A pre-scanned ``SkillLoader`` instance.
    """
    available_ids = loader.available_ids

    if not available_ids:
        return

    @tool(
        "skill",
        "Load a specialized skill to guide your approach to a task. "
        "The skill content will provide detailed instructions.",
        {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "enum": available_ids,
                    "description": "The skill to load.",
                },
            },
            "required": ["skill_name"],
        },
        display_name="Loading skill",
        toolset=_TOOLSET,
    )
    async def skill_tool(skill_name: str) -> str:
        """Load and return the content of the requested skill.

        Args:
            skill_name: ID of the skill to load (must match a
                discovered SKILL.md directory name).

        Returns:
            The skill content prefixed with a header, or an error
            message if the skill cannot be loaded.
        """
        try:
            content = loader.load(skill_name)
            return f"[Skill: {skill_name}]\n{content}"
        except KeyError as exc:
            return f"Error: {exc}"
        except OSError as exc:
            return f"Error loading skill {skill_name!r}: {exc}"

    registry.register(skill_tool)
