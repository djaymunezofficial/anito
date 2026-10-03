#!/usr/bin/env python3
"""ANITO installer: an interactive, step-by-step setup for Windows, Linux and macOS.

Run it from the project folder, in a terminal:

    python install.py        (Windows: py install.py)

It uses only the Python standard library, so it works before anything is installed.
"""

from __future__ import annotations

import http.client
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

MIN_PYTHON = (3, 10)
APP_NAME = "anito"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
ROOT = Path(__file__).resolve().parent
LOG_PATH = Path(tempfile.gettempdir()) / "anito-install.log"
BAYBAYIN = "ᜀᜈᜒᜆᜓ"
IS_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------- terminal

def _enable_ansi() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if not IS_WINDOWS:
        return os.environ.get("TERM") != "dumb"
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def _can_print(text: str) -> bool:
    try:
        text.encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


COLOR = _enable_ansi()
FANCY = _can_print("\u2714\u2716\u2500\u280b")
OK_MARK = "\u2714" if FANCY else "+"
FAIL_MARK = "\u2716" if FANCY else "x"
RULE = "\u2500" if FANCY else "-"
SPINNER = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f" if FANCY else "|/-\\"
SHOW_SPINNER = sys.stdout.isatty()


def _style(text: str, code: str) -> str:
    return "\033[%sm%s\033[0m" % (code, text) if COLOR else text


def bold(text: str) -> str:
    return _style(text, "1")


def dim(text: str) -> str:
    return _style(text, "2")


def green(text: str) -> str:
    return _style(text, "32")


def yellow(text: str) -> str:
    return _style(text, "33")


def red(text: str) -> str:
    return _style(text, "31")


def cyan(text: str) -> str:
    return _style(text, "36")


def heading(text: str) -> None:
    print()
    print(bold(cyan(text)))


def ok(text: str) -> None:
    print("  %s %s" % (green(OK_MARK), text))


def bad(text: str) -> None:
    print("  %s %s" % (red(FAIL_MARK), text))


def warn(text: str) -> None:
    print("  %s %s" % (yellow("!"), text))


def info(text: str) -> None:
    print("    %s" % dim(text))


def banner() -> None:
    title = "ANITO"
    if _can_print(BAYBAYIN):
        title += "  " + BAYBAYIN
    print()
    print(bold(cyan("  " + title)))
    print("  Terminal control deck for local Ollama models")
    print(dim("  " + RULE * 44))
    print("  This installer guides you step by step.")
    print("  Press Ctrl+C at any time to cancel.")


# --------------------------------------------------------------------------- prompts

class Cancelled(Exception):
    """The user quit the installer."""


def _input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Cancelled() from None


def ask_yes_no(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = _input("%s [%s] " % (question, hint)).lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def ask_choice(question: str, options: list[tuple[str, str]], default: int = 1) -> int:
    """Show a numbered menu of (label, description) and return the 1-based choice."""
    print(bold(question))
    for number, (label, description) in enumerate(options, 1):
        print("  %s  %s" % (cyan(str(number)), label))
        if description:
            print("     %s" % dim(description))
    while True:
        answer = _input("Choose 1-%d [%d], or q to quit: " % (len(options), default))
        if answer.lower() in ("q", "quit"):
            raise Cancelled()
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        print("  Enter a number from 1 to %d." % len(options))


# --------------------------------------------------------------------------- running commands

class StepFailed(Exception):
    def __init__(self, label: str, tail: list[str], hint: str | None = None) -> None:
        super().__init__(label)
        self.label = label
        self.tail = tail
        self.hint = hint


def _capture(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess | None:
    """Run a short command and capture its output. None if it can't run at all."""
    try:
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _clear_line(width: int) -> None:
    if SHOW_SPINNER:
        sys.stdout.write("\r" + " " * width + "\r")
        sys.stdout.flush()


def run_step(label: str, cmd: list[str], *, hint: str | None = None) -> None:
    """Run cmd behind a spinner. Output goes to the log file; raises StepFailed on error."""
    width = len(label) + 6
    with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as log:
        log.write("\n$ %s\n" % " ".join(cmd))
        log.flush()
        start = LOG_PATH.stat().st_size
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        except OSError as exc:
            raise StepFailed(label, [str(exc)], hint) from exc
        frames = itertools.cycle(SPINNER)
        try:
            while proc.poll() is None:
                if SHOW_SPINNER:
                    sys.stdout.write("\r  %s %s" % (next(frames), label))
                    sys.stdout.flush()
                time.sleep(0.1)
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            _clear_line(width)
            raise Cancelled() from None
    _clear_line(width)
    if proc.returncode != 0:
        bad(label)
        with open(LOG_PATH, "rb") as fh:
            fh.seek(start)
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
        raise StepFailed(label, lines[-15:], hint)
    ok(label)


# --------------------------------------------------------------------------- system checks

def python_version(cmd: list[str]) -> tuple[int, int] | None:
    result = _capture(cmd + ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
    if result is None or result.returncode != 0:
        return None
    try:
        major, minor = result.stdout.strip().split(".")[:2]
        return int(major), int(minor)
    except ValueError:
        return None


def find_python() -> tuple[str, str] | None:
    """Return (path, "3.12") of a Python that is new enough, or None."""
    current = sys.version_info[:2]
    if sys.executable and current >= MIN_PYTHON:
        return sys.executable, "%d.%d" % current

    candidates: list[list[str]] = []
    if IS_WINDOWS:
        candidates.append(["py", "-3"])
    candidates += [["python3.%d" % minor] for minor in (13, 12, 11, 10)]
    candidates += [["python3"], ["python"]]
    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        version = python_version(cmd)
        if version is None or version < MIN_PYTHON:
            continue
        result = _capture(cmd + ["-c", "import sys; print(sys.executable)"])
        if result is not None and result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip(), "%d.%d" % version
    return None


def python_install_help() -> list[str]:
    if IS_WINDOWS:
        return [
            "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),",
            "or run: winget install Python.Python.3.12",
            "Then open a new terminal and run this installer again.",
        ]
    if sys.platform == "darwin":
        return [
            "Install it with Homebrew: brew install python",
            "or from https://www.python.org/downloads/",
            "Then run this installer again.",
        ]
    return [
        "Install it with your package manager. On Debian or Ubuntu:",
        "  sudo apt install python3 python3-venv python3-pip",
        "Then run this installer again.",
    ]


def pipx_command(py: str) -> list[str] | None:
    if shutil.which("pipx"):
        return ["pipx"]
    result = _capture([py, "-m", "pipx", "--version"])
    if result is not None and result.returncode == 0:
        return [py, "-m", "pipx"]
    return None


def pipx_has_anito(pipx: list[str]) -> bool:
    result = _capture(pipx + ["list"])
    if result is None or result.returncode != 0:
        return False
    return re.search(r"^\s*package %s\b" % APP_NAME, result.stdout, re.MULTILINE) is not None


def pipx_bin_dir(pipx: list[str]) -> Path:
    result = _capture(pipx + ["environment", "--value", "PIPX_BIN_DIR"])
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return Path.home() / ".local" / "bin"


def script_name() -> str:
    return "anito.exe" if IS_WINDOWS else "anito"


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def venv_script(venv: Path) -> Path:
    return venv / ("Scripts/%s" if IS_WINDOWS else "bin/%s") % script_name() if False else (
        venv / ("Scripts" if IS_WINDOWS else "bin") / script_name()
    )


def ollama_info(url: str) -> tuple[str, int | None] | None:
    """(version, model count) if Ollama answers at url, otherwise None."""
    expected = (OSError, ValueError, http.client.HTTPException, AttributeError)
    try:
        with urllib.request.urlopen(url + "/api/version", timeout=2) as resp:
            version = str(json.load(resp).get("version", "unknown"))
    except expected:
        return None
    models = None
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=3) as resp:
            models = len(json.load(resp).get("models") or [])
    except expected:
        pass
    return version, models


def normalize_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value or DEFAULT_OLLAMA_URL


def check_ollama() -> str | None:
    """Report Ollama's state. Returns a non-default URL the user wants saved, if any."""
    installed = shutil.which("ollama") is not None
    status = ollama_info(DEFAULT_OLLAMA_URL)
    if status is not None:
        version, models = status
        detail = "" if models is None else ", %d model%s installed" % (models, "" if models == 1 else "s")
        ok("Ollama %s is running%s" % (version, detail))
        return None

    if installed:
        warn("Ollama is installed but not running. Start it with 'ollama serve' or open the Ollama app.")
    else:
        warn("Ollama was not found. ANITO needs it: https://ollama.com/download")
    info("You can install ANITO now and start Ollama later.")
    print()
    answer = _input("  Ollama on another machine or port? Enter its address, or press Enter to keep %s: " % DEFAULT_OLLAMA_URL)
    if not answer:
        return None
    url = normalize_url(answer)
    if ollama_info(url) is not None:
        ok("Reached Ollama at %s" % url)
    else:
        warn("Could not reach %s right now. It will be saved anyway." % url)
    return None if url == DEFAULT_OLLAMA_URL else url


# --------------------------------------------------------------------------- settings file

def config_dir() -> Path:
    # Mirrors anito.config.get_config_dir().
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        root = Path(xdg) if xdg else Path.home() / ".config"
    return root / APP_NAME


def save_ollama_url(url: str) -> None:
    """Store the Ollama address, keeping any settings that already exist."""
    path = config_dir() / "config.json"
    data: dict = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    data["ollama_url"] = url
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def apply_ollama_url(url: str | None) -> None:
    if not url:
        return
    try:
        save_ollama_url(url)
        ok("Saved the Ollama address (%s)" % url)
    except (OSError, ValueError) as exc:
        warn("Could not save the Ollama address (%s). Set it later in ANITO's Settings tab." % exc)


# --------------------------------------------------------------------------- install modes

PIPX_HINT = (
    "Install pipx with your package manager (macOS: brew install pipx; Debian/Ubuntu: "
    "sudo apt install pipx), then run this installer again. The development install "
    "doesn't need pipx."
)
VENV_HINT = "On Debian or Ubuntu the venv module is a separate package: sudo apt install python3-venv"


def install_everyday(py: str, pipx: list[str] | None, ollama_url: str | None) -> tuple[Path | None, bool]:
    """Install with pipx. Returns (path to the anito command, whether it is on PATH)."""
    heading("4. Installing")
    if pipx is None:
        run_step("Installing pipx", [py, "-m", "pip", "install", "--user", "--upgrade", "pipx"], hint=PIPX_HINT)
        pipx = [py, "-m", "pipx"]
        run_step("Adding pipx's folder to your PATH", pipx + ["ensurepath"])
    run_step("Installing ANITO", pipx + ["install", "--force", "--python", py, str(ROOT)])
    apply_ollama_url(ollama_url)

    launcher = pipx_bin_dir(pipx) / script_name()
    on_path = shutil.which("anito") is not None
    if not on_path:
        _capture(pipx + ["ensurepath"])
    return (launcher if launcher.exists() else None), on_path


def install_development(py: str, ollama_url: str | None) -> Path | None:
    """Editable install into a project .venv. Returns the path to the anito command."""
    heading("4. Installing")
    venv = ROOT / ".venv"
    if venv_python(venv).exists():
        ok("Using the existing .venv")
    else:
        run_step("Creating a virtual environment (.venv)", [py, "-m", "venv", str(venv)], hint=VENV_HINT)
    run_step("Installing ANITO and its dependencies", [str(venv_python(venv)), "-m", "pip", "install", "-e", str(ROOT)])
    apply_ollama_url(ollama_url)
    launcher = venv_script(venv)
    return launcher if launcher.exists() else None


def remove_tree(path: Path, label: str) -> None:
    try:
        shutil.rmtree(path)
        ok(label)
    except OSError as exc:
        bad("%s failed: %s" % (label, exc))


def run_uninstall(pipx: list[str] | None) -> int:
    heading("2. What should be removed?")
    venv = ROOT / ".venv"
    has_pipx = pipx is not None and pipx_has_anito(pipx)
    has_venv = (venv / "pyvenv.cfg").is_file()
    settings = config_dir()
    has_settings = settings.is_dir()

    if not (has_pipx or has_venv or has_settings):
        ok("Nothing to remove. ANITO isn't installed here.")
        return 0

    remove_pipx = has_pipx and ask_yes_no("Remove the ANITO command (installed with pipx)?")
    remove_venv = has_venv and ask_yes_no("Delete the project's .venv folder?")
    remove_settings = has_settings and ask_yes_no(
        "Also delete your settings in %s?" % settings, default=False
    )
    if not (remove_pipx or remove_venv or remove_settings):
        print("Nothing selected, so nothing was changed.")
        return 0

    print()
    if not ask_yes_no("Remove the selected items now?"):
        raise Cancelled()

    heading("3. Removing")
    if remove_pipx and pipx is not None:
        run_step("Removing ANITO with pipx", pipx + ["uninstall", APP_NAME])
    if remove_venv:
        remove_tree(venv, "Deleted the .venv folder")
    if remove_settings and settings.name == APP_NAME:
        remove_tree(settings, "Deleted your settings")
    heading("Done")
    ok("ANITO has been removed.")
    return 0


# --------------------------------------------------------------------------- main flow

def run() -> int:
    banner()
    if not sys.stdin.isatty():
        bad("This installer is interactive. Run it in a terminal: python install.py")
        return 1
    if not (ROOT / "Pyproject.toml").is_file():
        bad("Pyproject.toml was not found next to install.py. Run the installer from the ANITO folder.")
        return 1

    heading("1. Checking your system")
    found = find_python()
    if found is None:
        bad("Python %d.%d or newer was not found." % MIN_PYTHON)
        for line in python_install_help():
            info(line)
        return 1
    py, py_version = found
    ok("Python %s  (%s)" % (py_version, py))

    pipx = pipx_command(py)
    if pipx is not None:
        ok("pipx is installed")
    else:
        warn("pipx is not installed. It's only needed for the everyday install, and I can set it up.")
    ollama_url = check_ollama()

    heading("2. Choose how to install")
    while True:
        mode = ask_choice(
            "What would you like to do?",
            [
                ("Install ANITO (recommended)", "The 'anito' command works from any terminal. Uses pipx."),
                ("Install for development", "Editable install in a project .venv. Code changes apply right away."),
                ("Uninstall ANITO", "Remove what this installer set up."),
            ],
        )
        if mode == 1 and pipx is None:
            print()
            if not ask_yes_no("The everyday install needs pipx. Install it for you now?"):
                print()
                continue
        break

    if mode == 3:
        return run_uninstall(pipx)

    heading("3. Review")
    print("  I'm about to:")
    if mode == 1:
        if pipx is None:
            print("   - install pipx for your user account")
        print("   - install ANITO from %s using pipx" % ROOT)
    else:
        print("   - %s a virtual environment in %s" % ("reuse" if venv_python(ROOT / ".venv").exists() else "create", ROOT / ".venv"))
        print("   - install ANITO and its dependencies into it (editable)")
    if ollama_url:
        print("   - save the Ollama address %s" % ollama_url)
    print()
    if not ask_yes_no("Go ahead?"):
        raise Cancelled()

    if mode == 1:
        launcher, on_path = install_everyday(py, pipx, ollama_url)
    else:
        launcher, on_path = install_development(py, ollama_url), False

    heading("5. Done")
    ok("ANITO is installed.")
    print()
    if mode == 1 and on_path:
        print("  Start it any time with:  %s" % bold("anito"))
    elif mode == 1:
        print("  Open a %s terminal window, then start it with:  %s" % (bold("new"), bold("anito")))
        print("  (A new window is needed so your PATH picks up the change.)")
        if launcher is not None:
            print("  Until then:  %s" % launcher)
    else:
        if launcher is not None:
            print("  Start it with:  %s" % bold(str(launcher)))
        print("  Or activate the environment first, then run anito:")
        if IS_WINDOWS:
            print("     .venv\\Scripts\\Activate.ps1")
        else:
            print("     source .venv/bin/activate")
    if IS_WINDOWS:
        print("  Tip: Windows Terminal shows ANITO best.")
    print()

    if launcher is not None and ask_yes_no("Start ANITO now?"):
        return subprocess.call([str(launcher)])
    return 0


def main() -> int:
    try:
        return run()
    except Cancelled:
        print("Installer cancelled.")
        return 130
    except StepFailed as exc:
        print()
        print(red("Something went wrong during: %s" % exc.label))
        for line in exc.tail:
            info(line)
        if exc.hint:
            print()
            print("  %s" % exc.hint)
        print()
        print("  The full log is at %s" % LOG_PATH)
        return 1


if __name__ == "__main__":
    sys.exit(main())
