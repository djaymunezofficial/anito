"""Persistent user configuration for ANITO."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

APP_NAME = "anito"

DEFAULT_OLLAMA_URL = "http://localhost:11434"
TEMPERATURE_RANGE = (0.0, 2.0)
NUM_CTX_RANGE = (256, 131072)


def get_config_dir() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        root = Path(xdg) if xdg else Path.home() / ".config"
    return root / APP_NAME


CONFIG_DIR = get_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"


# Validators raise ValueError with a message that can be shown to the user as-is.

def parse_temperature(raw: Any) -> float:
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise ValueError("Temperature must be a number, e.g. 0.7") from None
    low, high = TEMPERATURE_RANGE
    # Chained comparison is False for nan, so it is rejected here too.
    if not low <= value <= high:
        raise ValueError(f"Temperature must be between {low} and {high}")
    return value


def parse_num_ctx(raw: Any) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError("Context length must be a whole number, e.g. 4096") from None
    low, high = NUM_CTX_RANGE
    if not low <= value <= high:
        raise ValueError(f"Context length must be between {low} and {high}")
    return value


def parse_url(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("Ollama URL must be text")
    value = raw.strip().rstrip("/")
    if not value:
        raise ValueError("Ollama URL cannot be empty")
    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    return value


def parse_text(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("Expected text")
    return raw


@dataclass
class Config:
    ollama_url: str = DEFAULT_OLLAMA_URL
    temperature: float = 0.7
    num_ctx: int = 4096
    system_prompt: str = ""
    last_model: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> Config:
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        # Invalid or missing fields keep their defaults.
        for name, parser in _PARSERS.items():
            if name in data:
                try:
                    setattr(cfg, name, parser(data[name]))
                except (ValueError, TypeError):
                    pass
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PARSERS: dict[str, Callable[[Any], Any]] = {
    "ollama_url": parse_url,
    "temperature": parse_temperature,
    "num_ctx": parse_num_ctx,
    "system_prompt": parse_text,
    "last_model": parse_text,
}


def load_config() -> Config:
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
        return Config.from_dict(json.loads(text))
    except (OSError, ValueError):
        return Config()


def save_config(cfg: Config) -> None:
    """Write the config to disk. Raises OSError on failure."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False)

    # Write to a temp file and replace, so a failed write can't corrupt the old config.
    fd, tmp_name = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, CONFIG_FILE)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
