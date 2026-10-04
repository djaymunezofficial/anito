"""Start and stop a local Ollama server.

Ollama's HTTP API has no way to shut itself down or start itself, so this
drives the process from outside. Which way depends on how Ollama is installed:
a systemd service (Linux), or a plain `ollama serve` that ANITO runs itself.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from anito.utils.ollama_api import OllamaClient, OllamaError

SERVICE_NAME = "ollama"
COMMAND_TIMEOUT = 60.0
STOP_GRACE = 10.0

_LOCAL_HOSTS = frozenset({"localhost", "::1", "0.0.0.0"})
# What sudo and polkit say when they need a password but have no terminal to ask on.
_AUTH_HINTS = (
    "a password is required",
    "a terminal is required",
    "no tty present",
    "interactive authentication required",
)


class Action(str, Enum):
    START = "start"
    STOP = "stop"


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str = ""
    # True when the command needs a password: retry it with run_in_terminal().
    needs_terminal: bool = False


def is_local_url(url: str) -> bool:
    """True if the URL points at this machine, the only case ANITO can control."""
    host = (urlparse(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.startswith("127.")


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _detach_kwargs() -> dict[str, Any]:
    # The server should outlive ANITO's terminal, so it gets its own session.
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class OllamaController:
    label: str

    async def run(self, action: Action) -> Outcome:
        """Try the action without touching the terminal."""
        raise NotImplementedError

    def run_in_terminal(self, action: Action) -> Outcome:
        """Run the action with the real terminal, so a password prompt works.

        Blocks. Call it only while the app is suspended.
        """
        return Outcome(False, "This control never needs the terminal")


class SystemdController(OllamaController):
    label = f"systemd service '{SERVICE_NAME}'"

    def __init__(self, use_sudo: bool | None = None) -> None:
        if use_sudo is None:
            use_sudo = os.geteuid() != 0 and shutil.which("sudo") is not None
        self._sudo = use_sudo

    def _command(self, action: Action, *, interactive: bool) -> list[str]:
        command = ["systemctl", action.value, SERVICE_NAME]
        if not self._sudo:
            return command
        # -n makes sudo fail at once instead of waiting for a password nobody can type.
        return ["sudo", *([] if interactive else ["-n"]), *command]

    async def run(self, action: Action) -> Outcome:
        command = self._command(action, interactive=False)
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return Outcome(False, f"Could not run {command[0]}: {exc}")
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return Outcome(False, f"Timed out trying to {action.value} Ollama")
        if proc.returncode == 0:
            return Outcome(True)
        text = stderr.decode(errors="replace").strip()
        if any(hint in text.lower() for hint in _AUTH_HINTS):
            return Outcome(False, needs_terminal=True)
        return Outcome(False, _last_line(text) or f"{command[0]} exited with code {proc.returncode}")

    def run_in_terminal(self, action: Action) -> Outcome:
        command = self._command(action, interactive=True)
        print(f"\nANITO needs permission to {action.value} Ollama.\n$ {' '.join(command)}\n")
        try:
            code = subprocess.run(command, check=False).returncode
        except KeyboardInterrupt:
            return Outcome(False, "Cancelled")
        except OSError as exc:
            return Outcome(False, f"Could not run {command[0]}: {exc}")
        if code == 0:
            return Outcome(True)
        try:
            input("\nPress Enter to return to ANITO...")
        except (EOFError, KeyboardInterrupt):
            pass
        return Outcome(False, f"Command failed (exit code {code})")


class ProcessController(OllamaController):
    """Runs `ollama serve` itself, so it can only stop a server it started."""

    label = "'ollama serve' run by ANITO"

    def __init__(self, executable: str) -> None:
        self._exe = executable
        self._proc: asyncio.subprocess.Process | None = None

    async def run(self, action: Action) -> Outcome:
        if action is Action.START:
            return await self._start()
        return await self._stop()

    async def _start(self) -> Outcome:
        if self._proc is not None and self._proc.returncode is None:
            return Outcome(True)
        try:
            proc = await asyncio.create_subprocess_exec(
                self._exe,
                "serve",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **_detach_kwargs(),
            )
        except OSError as exc:
            return Outcome(False, f"Could not start Ollama: {exc}")
        try:
            code = await asyncio.wait_for(proc.wait(), 1.0)
        except asyncio.TimeoutError:
            self._proc = proc
            return Outcome(True)
        return Outcome(
            False,
            f"Ollama exited right away (code {code}). Is another copy already running?",
        )

    async def _stop(self) -> Outcome:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return Outcome(
                False,
                "ANITO didn't start this Ollama, so it can't stop it. Stop it where you started it.",
            )
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), STOP_GRACE)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        self._proc = None
        return Outcome(True)


async def _has_unit() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "cat",
            f"{SERVICE_NAME}.service",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return await asyncio.wait_for(proc.wait(), 5.0) == 0
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False


async def detect() -> OllamaController | None:
    """Pick a way to control Ollama here, or None if there isn't one."""
    if sys.platform.startswith("linux") and shutil.which("systemctl") and await _has_unit():
        return SystemdController()
    executable = shutil.which("ollama")
    return ProcessController(executable) if executable else None


async def wait_for_state(
    client: OllamaClient,
    *,
    running: bool,
    timeout: float = 20.0,
    interval: float = 0.5,
) -> bool:
    """Poll until the server answers (running=True) or stops answering (running=False)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            await client.version()
            up = True
        except OllamaError:
            up = False
        if up == running:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)
