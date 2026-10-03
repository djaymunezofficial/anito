"""Open files and folders in Visual Studio Code."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from anito.skills.base import Skill, SkillContext, SkillError, SkillResult

# Tried in order; the first one found on PATH wins.
EXECUTABLES = ("code", "code-insiders")
LAUNCH_TIMEOUT = 10.0
# On Windows `code` is a .cmd script, and cmd.exe gives these characters special meaning.
UNSAFE_BATCH_CHARS = frozenset('&|<>^%"')

NOT_FOUND = (
    "VS Code's 'code' command was not found on PATH. Install VS Code, and in its "
    "Command Palette run: Shell Command: Install 'code' command in PATH."
)


def find_vscode() -> str | None:
    for name in EXECUTABLES:
        path = shutil.which(name)
        if path:
            return path
    return None


def resolve_target(raw: str, cwd: Path) -> Path:
    """Turn user text into an absolute path. Raises SkillError if it can't be opened."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if not text:
        raise SkillError("Enter a file name, e.g. /code main.py")
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = cwd / path
        path = path.resolve()
        # A missing file is fine when its folder exists: VS Code opens it as a new file.
        if path.exists() or path.parent.is_dir():
            return path
    except (OSError, RuntimeError, ValueError) as exc:
        raise SkillError(f"Invalid path: {text}") from exc
    raise SkillError(f"Folder not found: {path.parent}")


def _check_launcher_safe(exe: str, target: Path) -> None:
    if Path(exe).suffix.lower() in {".cmd", ".bat"} and UNSAFE_BATCH_CHARS & set(str(target)):
        raise SkillError(
            "That path has characters the VS Code launcher can't take safely. "
            "Rename it, or open it from inside VS Code."
        )


async def _launch(exe: str, target: Path) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            str(target),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise SkillError(f"Could not start VS Code: {exc}") from exc
    try:
        code = await asyncio.wait_for(proc.wait(), LAUNCH_TIMEOUT)
    except asyncio.TimeoutError:
        # Still running means the launcher stayed attached to VS Code, so it did start.
        return
    if code != 0:
        raise SkillError(f"VS Code exited with an error (code {code})")


class VSCodeSkill(Skill):
    name = "code"
    description = "Open a file or folder in Visual Studio Code."
    usage = "/code <filename>"
    aliases = ("vscode",)
    parameters = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Path of the file or folder to open. Relative paths start from the current directory.",
            }
        },
        "required": ["filename"],
    }

    async def run(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        filename = args.get("filename")
        if not isinstance(filename, str):
            raise SkillError("filename must be text")
        target = resolve_target(filename, ctx.cwd)
        exe = find_vscode()
        if exe is None:
            raise SkillError(NOT_FOUND)
        _check_launcher_safe(exe, target)
        is_new = not target.exists()
        await _launch(exe, target)
        note = " (new file, not saved yet)" if is_new else ""
        return SkillResult.success(f"Opened {target} in VS Code{note}.")
