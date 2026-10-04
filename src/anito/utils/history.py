"""Saved chats: one small JSON file per conversation."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anito.config import CHATS_DIR

FORMAT_VERSION = 1
TITLE_LIMIT = 40

# Ids end up in file names, so only characters that are safe there are accepted.
_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_ROLES = frozenset({"user", "assistant"})


def make_title(text: str, limit: int = TITLE_LIMIT) -> str:
    title = " ".join(text.split())
    if not title:
        return "New chat"
    if len(title) > limit:
        title = title[: limit - 1].rstrip() + "\u2026"
    return title


def new_chat_id() -> str:
    # Millisecond timestamp first, so file names sort in creation order.
    return f"{int(time.time() * 1000):013x}-{secrets.token_hex(2)}"


def _number(value: Any) -> float:
    # json.loads accepts NaN and Infinity, which would break sorting.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


@dataclass
class ChatSummary:
    id: str
    title: str
    model: str
    updated: float


@dataclass
class Chat:
    id: str
    title: str
    model: str
    created: float
    updated: float
    messages: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def start(cls, model: str, first_message: str) -> Chat:
        now = time.time()
        return cls(new_chat_id(), make_title(first_message), model, now, now)

    @classmethod
    def from_dict(cls, chat_id: str, data: Any) -> Chat | None:
        """Build a chat from untrusted data. Returns None if nothing usable is in it."""
        if not isinstance(data, dict):
            return None
        messages = [
            {"role": item["role"], "content": item["content"]}
            for item in data.get("messages") or []
            if isinstance(item, dict)
            and item.get("role") in _ROLES
            and isinstance(item.get("content"), str)
        ]
        if not messages:
            return None
        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            first_user = next((m["content"] for m in messages if m["role"] == "user"), "")
            title = make_title(first_user)
        model = data.get("model")
        return cls(
            id=chat_id,
            title=title,
            model=model if isinstance(model, str) else "",
            created=_number(data.get("created")),
            updated=_number(data.get("updated")),
            messages=messages,
        )

    def to_dict(self) -> dict[str, Any]:
        # The id is the file name, so it isn't repeated inside.
        return {
            "version": FORMAT_VERSION,
            "title": self.title,
            "model": self.model,
            "created": self.created,
            "updated": self.updated,
            "messages": self.messages,
        }


class HistoryStore:
    def __init__(self, directory: Path = CHATS_DIR) -> None:
        self._dir = directory

    def _path(self, chat_id: str) -> Path | None:
        if not _ID_PATTERN.fullmatch(chat_id):
            return None
        return self._dir / f"{chat_id}.json"

    @staticmethod
    def _read(path: Path) -> Chat | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return Chat.from_dict(path.stem, data)

    def list_chats(self) -> list[ChatSummary]:
        """Newest first. Files that can't be read are skipped, not reported."""
        try:
            paths = list(self._dir.glob("*.json"))
        except OSError:
            return []
        summaries = []
        for path in paths:
            if not _ID_PATTERN.fullmatch(path.stem):
                continue
            chat = self._read(path)
            if chat is not None:
                summaries.append(ChatSummary(chat.id, chat.title, chat.model, chat.updated))
        summaries.sort(key=lambda s: (s.updated, s.id), reverse=True)
        return summaries

    def load(self, chat_id: str) -> Chat | None:
        path = self._path(chat_id)
        return self._read(path) if path is not None else None

    def save(self, chat: Chat) -> None:
        """Write the chat and stamp it as updated. Raises OSError if the write fails."""
        path = self._path(chat.id)
        if path is None:
            raise ValueError(f"Invalid chat id: {chat.id!r}")
        chat.updated = time.time()
        payload = json.dumps(chat.to_dict(), indent=2, ensure_ascii=False)

        self._dir.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and replace, so a failed write can't damage the saved chat.
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=".chat-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def delete(self, chat_id: str) -> bool:
        """Remove a chat. Returns False if there was nothing to remove. Raises OSError on failure."""
        path = self._path(chat_id)
        if path is None or not path.exists():
            return False
        path.unlink()
        return True
