"""Skills: slash commands the user can type and tools a model can call."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

# Handled by the registry itself, so no skill may claim them.
RESERVED_NAMES = frozenset({"help"})


class SkillError(Exception):
    """A skill could not do what was asked. The message is safe to show to the user."""


@dataclass(frozen=True)
class SkillContext:
    """What a skill is allowed to know about its surroundings."""

    cwd: Path = field(default_factory=Path.cwd)


@dataclass(frozen=True)
class SkillResult:
    ok: bool
    output: str

    @classmethod
    def success(cls, output: str) -> SkillResult:
        return cls(True, output)

    @classmethod
    def failure(cls, output: str) -> SkillResult:
        return cls(False, output)


class Skill(ABC):
    """One capability, reachable as a slash command and as a model tool.

    Subclasses set the class attributes below and implement run().
    """

    # Slash command name without the "/", lowercase, no spaces. Also the tool name.
    name: ClassVar[str]
    # One line. Shown in /help and sent to the model as the tool description.
    description: ClassVar[str]
    # Shown in /help and in usage errors, e.g. "/code <filename>".
    usage: ClassVar[str]
    # Other slash names that run the same skill.
    aliases: ClassVar[tuple[str, ...]] = ()
    # JSON Schema for the tool arguments.
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}, "required": []}

    def parse_args(self, raw: str) -> dict[str, Any]:
        """Turn the text after the command name into run() arguments.

        The default hands the whole text to the first required parameter, which
        suits one-argument commands. Raise SkillError for bad input.
        """
        raw = raw.strip()
        required = self.parameters.get("required") or []
        if not required:
            return {}
        if not raw:
            raise SkillError(f"Usage: {self.usage}")
        return {required[0]: raw}

    @abstractmethod
    async def run(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        """Do the work. Raise SkillError for failures the user should read."""

    def tool_definition(self) -> dict[str, Any]:
        """This skill in the shape Ollama expects in the `tools` list of a chat request."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._lookup: dict[str, Skill] = {}

    def __iter__(self) -> Iterator[Skill]:
        return iter(self._skills.values())

    def __len__(self) -> int:
        return len(self._skills)

    def register(self, skill: Skill) -> None:
        names = (skill.name, *skill.aliases)
        for name in names:
            if not name or name != name.lower() or any(ch.isspace() for ch in name):
                raise ValueError(f"Invalid skill name {name!r}: use lowercase, no spaces")
            if name in RESERVED_NAMES:
                raise ValueError(f"Skill name {name!r} is reserved")
            if name in self._lookup:
                raise ValueError(f"Skill name {name!r} is already registered")
        self._skills[skill.name] = skill
        for name in names:
            self._lookup[name] = skill

    def get(self, name: str) -> Skill | None:
        return self._lookup.get(name.lower())

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Tool list to send with a chat request so the model can call skills."""
        return [skill.tool_definition() for skill in self]

    @staticmethod
    def split_command(text: str) -> tuple[str, str] | None:
        """Split "/code main.py" into ("code", "main.py"). None if text isn't a command."""
        stripped = text.strip()
        if len(stripped) < 2 or stripped[0] != "/" or stripped[1].isspace():
            return None
        parts = stripped[1:].split(None, 1)
        return parts[0].lower(), parts[1] if len(parts) > 1 else ""

    async def run_command(self, text: str, ctx: SkillContext | None = None) -> SkillResult | None:
        """Run a typed slash command. Returns None if the text isn't one.

        A message that starts with "/" but names no skill (a pasted path, say)
        comes back as a failure, so the caller decides whether to send it on as chat.
        """
        parsed = self.split_command(text)
        if parsed is None:
            return None
        name, rest = parsed
        if name == "help":
            return SkillResult.success(self.help_text())
        skill = self.get(name)
        if skill is None:
            return SkillResult.failure(f"Unknown command /{name}. Type /help to see what is available.")
        try:
            args = skill.parse_args(rest)
        except SkillError as exc:
            return SkillResult.failure(str(exc))
        return await self._execute(skill, args, ctx)

    async def run_tool(
        self, name: str, arguments: Any, ctx: SkillContext | None = None
    ) -> SkillResult:
        """Run a skill the model asked for. Never raises for bad model output."""
        skill = self.get(name)
        if skill is None:
            return SkillResult.failure(f"No skill named {name}")
        if not isinstance(arguments, dict):
            return SkillResult.failure("Tool arguments must be an object")
        missing = [key for key in skill.parameters.get("required") or [] if key not in arguments]
        if missing:
            return SkillResult.failure(f"Missing argument: {', '.join(missing)}")
        return await self._execute(skill, arguments, ctx)

    async def _execute(
        self, skill: Skill, args: dict[str, Any], ctx: SkillContext | None
    ) -> SkillResult:
        try:
            return await skill.run(args, ctx or SkillContext())
        except SkillError as exc:
            return SkillResult.failure(str(exc))
        except Exception as exc:  # noqa: BLE001
            # A bug in one skill must not take down the app. Cancellation isn't an Exception, so it still passes.
            return SkillResult.failure(f"/{skill.name} failed: {exc}")

    def help_text(self) -> str:
        rows = [(skill.usage, skill.description) for skill in self]
        rows.append(("/help", "Show this list"))
        width = max(len(usage) for usage, _ in rows)
        lines = ["Commands:"]
        lines += [f"  {usage.ljust(width)}  {description}" for usage, description in rows]
        return "\n".join(lines)
