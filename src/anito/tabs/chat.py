"""Home screen: sidebar (brand, saved chats, status) and the streaming chat.

The app owns everything outside this widget (Settings, Help, quitting, loading
models). ChatHome tells it what the user asked for through messages and is fed
model state through set_models(), set_offline() and set_checking().
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterable, Sequence
from datetime import datetime

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Markdown, Static
from textual.worker import Worker

from anito import __version__
from anito.config import Config, save_config
from anito.dialogs.modals import ConfirmDialog, ModelPickerDialog
from anito.utils.history import Chat, ChatSummary, HistoryStore
from anito.utils.ollama_api import ChatChunk, ModelInfo, OllamaClient, OllamaError, RunningModel

CURSOR = "\u258c"
# Each update re-lays out the growing message, so cap redraws at about ten a second.
FLUSH_INTERVAL = 0.1
TOKEN_SEGMENTS = 9
TITLE_WIDTH = 28

HISTORY_OPEN = "History \u25be"
HISTORY_CLOSED = "History \u25b8"

# Two-row block lettering for A N I T O.
_BRAND = "\u2584\u2580\u2588 \u2588\u2584 \u2588 \u2588 \u2580\u2588\u2580 \u2588\u2580\u2588\n\u2588\u2580\u2588 \u2588 \u2580\u2588 \u2588  \u2588  \u2588\u2584\u2588"
_TAGLINE = "Sovereign local AI in your terminal."

_ROLE_NAMES = {"user": "You", "system": "System", "error": "Error"}


def _version_label() -> str:
    major_minor = ".".join(__version__.split(".")[:2])
    return f"V.{major_minor} BETA"


def _plain(text: str) -> str:
    # Button labels are parsed as markup, so square brackets in a chat title would break them.
    return text.replace("[", "(").replace("]", ")")


def _label(text: str, limit: int = TITLE_WIDTH) -> str:
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return _plain(text)


def _short(count: int) -> str:
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}".rstrip("0").rstrip(".") + "k"


def _estimate_tokens(messages: Sequence[dict[str, str]]) -> int:
    # About four characters per token. Only used until a real count comes back.
    return sum(len(m["content"]) for m in messages) // 4


def _stats_text(final: ChatChunk | None) -> str:
    if final is None or not final.eval_count:
        return ""
    if final.eval_duration > 0:
        rate = final.eval_count / (final.eval_duration / 1e9)
        return f"{final.eval_count} tokens, {rate:.1f} tok/s"
    return f"{final.eval_count} tokens"


class ChatMessage(Vertical):
    def __init__(
        self,
        role: str,
        content: str,
        *,
        label: str,
        streaming: bool = False,
        markdown: bool = False,
    ) -> None:
        classes = f"message {role} streaming" if streaming else f"message {role}"
        super().__init__(classes=classes)
        self._label = f"{label}  {datetime.now().strftime('%H:%M')}"
        # markup=False so brackets in model text can't be read as Rich markup.
        self.body: Static | Markdown = (
            Markdown(content, classes="message-body")
            if markdown
            else Static(content, classes="message-body", markup=False)
        )

    def compose(self) -> ComposeResult:
        yield Static(self._label, classes="message-role", markup=False)
        yield self.body

    def finalize(self, text: str) -> None:
        # Plain text while streaming; Markdown is parsed once at the end to keep CPU use down.
        self.remove_class("streaming")
        self.body.remove()
        self.body = Markdown(text, classes="message-body")
        self.mount(self.body)


class HistoryItem(Horizontal):
    """One saved chat: its title (opens it) and a delete button."""

    class Open(Message):
        def __init__(self, chat_id: str) -> None:
            super().__init__()
            self.chat_id = chat_id

    class Delete(Message):
        def __init__(self, chat_id: str, title: str) -> None:
            super().__init__()
            self.chat_id = chat_id
            self.title = title

    def __init__(self, summary: ChatSummary, *, active: bool) -> None:
        super().__init__(classes="history-item active" if active else "history-item")
        self.chat_id = summary.id
        self._title = summary.title

    def compose(self) -> ComposeResult:
        yield Button(_label(self._title), classes="link history-title")
        yield Button("\u2715", classes="link history-delete")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.has_class("history-delete"):
            self.post_message(self.Delete(self.chat_id, self._title))
        else:
            self.post_message(self.Open(self.chat_id))


class ChatHome(Horizontal):
    BINDINGS = [
        Binding("ctrl+n", "new_chat", "New chat"),
        Binding("escape", "stop_generation", "Stop"),
    ]

    class ModelChanged(Message):
        """The active model changed. An empty string means none is available."""

        def __init__(self, model: str) -> None:
            super().__init__()
            self.model = model

    class StreamState(Message):
        """A reply started (True) or ended (False)."""

        def __init__(self, active: bool) -> None:
            super().__init__()
            self.active = active

    class HelpRequested(Message):
        """The Help link was pressed. The app shows the dialog."""

    class SettingsRequested(Message):
        """The Settings link was pressed. The app shows the popup."""

    class QuitRequested(Message):
        """The close button in the corner was pressed."""

    def __init__(
        self,
        client: OllamaClient,
        config: Config,
        store: HistoryStore | None = None,
    ) -> None:
        super().__init__(id="home")
        self._ollama = client
        self._config = config
        self._store = store or HistoryStore()
        self._models: list[str] = []
        self._loaded: set[str] = set()
        self._current = ""
        self._online = False
        self._chat: Chat | None = None
        self._history: list[dict[str, str]] = []
        self._used = 0
        self._streaming = False
        self._stream_worker: Worker[None] | None = None
        self._bubble: ChatMessage | None = None
        self._reply = ""
        self._history_open = True
        self._save_warned = False

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        with Vertical(id="sidebar"):
            yield Static(_BRAND, id="brand-title", markup=False)
            yield Static(_TAGLINE, id="brand-tagline", markup=False)
            yield Static(_version_label(), id="brand-version", markup=False)

            with Vertical(id="history-section"):
                with Horizontal(id="history-header"):
                    yield Button(HISTORY_OPEN, id="history-toggle", classes="link")
                    yield Button("New Chat", id="btn-new-chat", classes="link")
                yield VerticalScroll(id="history-list")

            with Vertical(id="status-section"):
                with Vertical(id="status-box"):
                    with Horizontal(classes="status-row"):
                        yield Static("Ollama:", classes="status-key", markup=False)
                        yield Static("CHECKING", id="status-conn", classes="checking", markup=False)
                        yield Static("", id="status-url", markup=False)
                    with Horizontal(classes="status-row"):
                        yield Static("Model:", classes="status-key", markup=False)
                        yield Button("none", id="status-model", classes="link", disabled=True)
                with Horizontal(id="tokens-row"):
                    yield Static("Tokens:", id="tokens-label", markup=False)
                    yield Static("", id="tokens-bar", markup=False)
                    yield Static("", id="tokens-count", markup=False)

            with Horizontal(id="sidebar-footer"):
                yield Button("Help", id="btn-help", classes="link")
                yield Button("Settings", id="btn-settings", classes="link")

        with Vertical(id="main"):
            with Horizontal(id="main-topbar"):
                yield Button("\u2715", id="btn-close")
            yield Static("", id="chat-notice", classes="notice hidden", markup=False)
            yield VerticalScroll(id="chat-log")
            yield Static("", id="chat-stats", markup=False)
            with Horizontal(id="chat-input-row"):
                yield Input(placeholder="", id="chat-input", disabled=True)
                yield Button("Send", id="btn-send", variant="primary", disabled=True)
                yield Button("Stop", id="btn-stop", variant="error", classes="hidden")

    def on_mount(self) -> None:
        self.refresh_url()
        self._sync_controls()
        self._update_tokens()
        self._refresh_history()

    # ------------------------------------------------------------------ #
    # Calls from the app
    # ------------------------------------------------------------------ #
    @property
    def current_model(self) -> str:
        return self._current

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def set_checking(self) -> None:
        self._set_connection("checking")

    def set_models(self, models: Iterable[ModelInfo], running: Iterable[RunningModel] = ()) -> None:
        self._online = True
        self._models = [m.name for m in models]
        self._loaded = {r.name for r in running}
        self._set_connection("online")
        previous = self._current
        if previous not in self._models:
            self._current = self._default_model()
            if previous:
                after = f"Using {self._current}." if self._current else "No models left."
                self._add_message("system", f"{previous} is no longer installed. {after}")
        self._set_notice(
            None
            if self._models
            else "No models installed. Open Settings, then Models, and pull one."
        )
        self._sync_model_label()
        self._sync_controls()
        if self._current != previous:
            self.post_message(self.ModelChanged(self._current))
        field = self.query_one("#chat-input", Input)
        if not field.disabled and self.app.focused is None:
            field.focus()

    def set_offline(self, message: str, *, offline: bool = True) -> None:
        self._online = False
        self._models = []
        self._loaded = set()
        self._set_connection("offline" if offline else "error")
        self._set_notice(f"{message}\nStart Ollama in Settings, then refresh the Models page.", error=True)
        self._sync_controls()

    def refresh_url(self) -> None:
        self.query_one("#status-url", Static).update(f"({self._config.ollama_url})")

    def refresh_context(self) -> None:
        """Call after the context length setting changes."""
        self._update_tokens()

    # ------------------------------------------------------------------ #
    # Status box
    # ------------------------------------------------------------------ #
    def _set_connection(self, state: str) -> None:
        labels = {"online": "ACTIVE", "offline": "OFFLINE", "error": "ERROR", "checking": "CHECKING"}
        css = {"online": "online", "checking": "checking"}.get(state, "offline")
        conn = self.query_one("#status-conn", Static)
        conn.set_classes(css)
        conn.update(labels[state])

    def _sync_model_label(self) -> None:
        self.query_one("#status-model", Button).label = _plain(self._current or "none")

    def _update_tokens(self) -> None:
        limit = self._config.num_ctx
        used = max(0, self._used)
        ratio = min(1.0, used / limit) if limit > 0 else 0.0
        filled = 0 if used == 0 else max(1, round(ratio * TOKEN_SEGMENTS))
        bar = Text()
        for index in range(TOKEN_SEGMENTS):
            bar.append("\u25a0 ", style="" if index < filled else "dim")
        widget = self.query_one("#tokens-bar", Static)
        widget.update(bar)
        widget.set_classes("full" if ratio >= 0.9 else "warn" if ratio >= 0.7 else "")
        self.query_one("#tokens-count", Static).update(f"{_short(used)} / {_short(limit)}")

    def _default_model(self) -> str:
        if self._config.last_model in self._models:
            return self._config.last_model
        return self._models[0] if self._models else ""

    def _set_notice(self, text: str | None, *, error: bool = False) -> None:
        notice = self.query_one("#chat-notice", Static)
        if text is None:
            notice.set_classes("notice hidden")
            return
        notice.set_classes("notice error" if error else "notice")
        notice.update(text)

    def _set_stats(self, text: str) -> None:
        self.query_one("#chat-stats", Static).update(text)

    def _sync_controls(self) -> None:
        streaming = self._streaming
        ready = self._online and bool(self._current)
        field = self.query_one("#chat-input", Input)
        field.disabled = streaming or not ready
        if streaming:
            field.placeholder = "Reply in progress. Press Esc to stop."
        elif not self._online:
            field.placeholder = "Waiting for Ollama..."
        elif not self._current:
            field.placeholder = "No model available."
        else:
            field.placeholder = "Type anything to start"
        send = self.query_one("#btn-send", Button)
        send.disabled = field.disabled
        send.set_class(streaming, "hidden")
        self.query_one("#btn-stop", Button).set_class(not streaming, "hidden")
        self.query_one("#status-model", Button).disabled = streaming or not self._online
        self.query_one("#btn-new-chat", Button).disabled = streaming

    # ------------------------------------------------------------------ #
    # History list
    # ------------------------------------------------------------------ #
    @work(exclusive=True, group="history")
    async def _refresh_history(self) -> None:
        # Reading every saved chat touches the disk, so keep it off the UI thread.
        summaries = await asyncio.to_thread(self._store.list_chats)
        container = self.query_one("#history-list", VerticalScroll)
        await container.remove_children()
        active = self._chat.id if self._chat else None
        if summaries:
            await container.mount_all(
                [HistoryItem(summary, active=summary.id == active) for summary in summaries]
            )
        else:
            await container.mount(Static("No saved chats yet.", classes="history-empty", markup=False))

    def _mark_active(self) -> None:
        active = self._chat.id if self._chat else None
        for item in self.query(HistoryItem):
            item.set_class(item.chat_id == active, "active")

    def _toggle_history(self) -> None:
        self._history_open = not self._history_open
        self.query_one("#history-list", VerticalScroll).set_class(not self._history_open, "hidden")
        self.query_one("#history-toggle", Button).label = (
            HISTORY_OPEN if self._history_open else HISTORY_CLOSED
        )

    def on_history_item_open(self, event: HistoryItem.Open) -> None:
        event.stop()
        if self._streaming:
            self.notify("Stop the reply before opening another chat", severity="warning")
            return
        if self._chat is not None and self._chat.id == event.chat_id:
            return
        self._open_chat(event.chat_id)

    def on_history_item_delete(self, event: HistoryItem.Delete) -> None:
        event.stop()
        if self._streaming:
            self.notify("Stop the reply first", severity="warning")
            return
        chat_id = event.chat_id

        def on_done(confirmed: bool | None) -> None:
            if confirmed:
                self._delete_chat(chat_id)

        self.app.push_screen(
            ConfirmDialog(
                "Delete chat",
                f'Delete "{event.title}"? This can\'t be undone.',
                confirm_label="Delete",
                danger=True,
            ),
            on_done,
        )

    def _delete_chat(self, chat_id: str) -> None:
        try:
            self._store.delete(chat_id)
        except OSError as exc:
            self.notify(str(exc), title="Delete failed", severity="error")
            return
        if self._chat is not None and self._chat.id == chat_id:
            self._reset_chat()
        self.notify("Chat deleted")
        self._refresh_history()

    def _open_chat(self, chat_id: str) -> None:
        chat = self._store.load(chat_id)
        if chat is None:
            self.notify("That chat can't be opened", severity="error")
            self._refresh_history()
            return
        self._chat = chat
        self._history = list(chat.messages)
        self._used = _estimate_tokens(self._history)

        log = self.query_one("#chat-log", VerticalScroll)
        log.remove_children()
        log.mount_all(
            [
                ChatMessage(
                    m["role"],
                    m["content"],
                    label=(chat.model or "Assistant") if m["role"] == "assistant" else "You",
                    markdown=m["role"] == "assistant",
                )
                for m in self._history
            ]
        )
        self.call_after_refresh(log.scroll_end, animate=False)
        self._set_stats("")
        self._update_tokens()
        self._mark_active()
        field = self.query_one("#chat-input", Input)
        if not field.disabled:
            field.focus()

    def _save_chat(self) -> None:
        chat = self._chat
        if chat is None:
            return
        chat.model = self._current
        chat.messages = list(self._history)
        try:
            self._store.save(chat)
        except OSError as exc:
            if not self._save_warned:
                self._save_warned = True
                self.notify(f"Could not save this chat: {exc}", severity="warning")
            return
        self._refresh_history()

    # ------------------------------------------------------------------ #
    # Messages in the log
    # ------------------------------------------------------------------ #
    def _add_message(
        self, role: str, content: str, *, label: str | None = None, streaming: bool = False
    ) -> ChatMessage:
        message = ChatMessage(
            role, content, label=label or _ROLE_NAMES.get(role, role.title()), streaming=streaming
        )
        log = self.query_one("#chat-log", VerticalScroll)
        log.mount(message)
        log.scroll_end(animate=False)
        return message

    # ------------------------------------------------------------------ #
    # Buttons and input
    # ------------------------------------------------------------------ #
    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id
        if button_id == "btn-send":
            self._submit()
        elif button_id == "btn-stop":
            self.action_stop_generation()
        elif button_id == "btn-new-chat":
            self.action_new_chat()
        elif button_id == "history-toggle":
            self._toggle_history()
        elif button_id == "status-model":
            self._pick_model()
        elif button_id == "btn-help":
            self.post_message(self.HelpRequested())
        elif button_id == "btn-settings":
            self.post_message(self.SettingsRequested())
        elif button_id == "btn-close":
            self.post_message(self.QuitRequested())

    def _pick_model(self) -> None:
        if self._streaming:
            self.notify("Stop the reply before switching models", severity="warning")
            return
        self.app.push_screen(
            ModelPickerDialog(self._models, self._current, self._loaded), self._on_model_picked
        )

    def _on_model_picked(self, name: str | None) -> None:
        if not name or name == self._current:
            return
        if self._streaming:
            self.notify("Stop the reply before switching models", severity="warning")
            return
        self._current = name
        self._config.last_model = name
        try:
            save_config(self._config)
        except OSError:
            # A failed write isn't worth interrupting the chat for.
            pass
        self._sync_model_label()
        self._sync_controls()
        self._add_message("system", f"Switched to {name}.")
        self.post_message(self.ModelChanged(name))

    # ------------------------------------------------------------------ #
    # Streaming
    # ------------------------------------------------------------------ #
    def _submit(self) -> None:
        field = self.query_one("#chat-input", Input)
        text = field.value.strip()
        if self._streaming or field.disabled or not text:
            return
        field.value = ""
        if self._chat is None:
            self._chat = Chat.start(self._current, text)
        self._history.append({"role": "user", "content": text})
        self._add_message("user", text)
        self._save_chat()
        self._begin_stream()

    def _begin_stream(self) -> None:
        model = self._current
        self._streaming = True
        self._reply = ""
        self._bubble = self._add_message("assistant", CURSOR, label=model, streaming=True)

        messages = list(self._history)
        if self._config.system_prompt:
            messages.insert(0, {"role": "system", "content": self._config.system_prompt})

        self._set_stats("Waiting for the model...")
        self._sync_controls()
        self.post_message(self.StreamState(True))
        self._stream_worker = self._run_chat(model, messages)
        # The disabled input can't hold focus, and Esc only works with focus inside this widget.
        self.call_after_refresh(self.query_one("#btn-stop", Button).focus)

    @work
    async def _run_chat(self, model: str, messages: list[dict[str, str]]) -> None:
        cfg = self._config
        phase = "waiting"
        last_flush = 0.0
        final: ChatChunk | None = None
        try:
            async with contextlib.aclosing(
                self._ollama.chat(
                    model,
                    messages,
                    temperature=cfg.temperature,
                    num_ctx=cfg.num_ctx,
                    keep_alive=cfg.keep_alive,
                )
            ) as stream:
                async for chunk in stream:
                    if chunk.content:
                        self._reply += chunk.content
                        if phase != "generating":
                            phase = "generating"
                            self._set_stats("Generating...")
                        now = time.monotonic()
                        if now - last_flush >= FLUSH_INTERVAL:
                            last_flush = now
                            self._render_stream()
                    elif chunk.thinking and phase == "waiting":
                        phase = "thinking"
                        self._set_stats("Thinking...")
                    if chunk.done:
                        final = chunk
        except OllamaError as exc:
            self._finish("error", error=str(exc))
            return
        if final is None:
            self._finish("error", error="Ollama ended the reply early")
        else:
            self._finish("done", final=final)

    def _render_stream(self) -> None:
        if self._bubble is None:
            return
        log = self.query_one("#chat-log", VerticalScroll)
        pinned = log.scroll_y >= log.max_scroll_y - 1
        self._bubble.body.update(self._reply + CURSOR)
        # Follow the text only if the user hasn't scrolled up to read.
        if pinned:
            log.scroll_end(animate=False)

    def _finish(
        self, outcome: str, *, error: str | None = None, final: ChatChunk | None = None
    ) -> None:
        if not self._streaming:
            return
        focused = self.app.focused
        had_focus = focused is None or self in focused.ancestors_with_self
        text = self._reply
        bubble = self._bubble
        self._streaming = False
        self._stream_worker = None
        self._bubble = None

        if bubble is not None:
            if text.strip():
                bubble.finalize(text)
                self._history.append({"role": "assistant", "content": text})
            else:
                bubble.remove()

        if outcome == "error" and error:
            self._add_message("error", error)
            self._set_stats("")
        elif outcome == "stopped":
            self._set_stats("Stopped.")
        else:
            if not text.strip():
                self._add_message("system", "The model returned an empty reply.")
            self._set_stats(_stats_text(final))

        if final is not None and (final.prompt_eval_count or final.eval_count):
            self._used = final.prompt_eval_count + final.eval_count
        else:
            self._used = _estimate_tokens(self._history)
        self._update_tokens()
        self._save_chat()

        self._sync_controls()
        self.post_message(self.StreamState(False))
        if had_focus:
            self.call_after_refresh(self.query_one("#chat-input", Input).focus)

    def action_stop_generation(self) -> None:
        if not self._streaming:
            return
        if self._stream_worker is not None:
            # The worker is suspended on the network, so it never runs again after this.
            self._stream_worker.cancel()
        self._finish("stopped")

    def action_new_chat(self) -> None:
        if self._streaming:
            self.notify("Stop the reply first", severity="warning")
            return
        # Chats are saved as they go, so there is nothing to confirm.
        self._reset_chat()

    def _reset_chat(self) -> None:
        self._chat = None
        self._history = []
        self._used = 0
        self.query_one("#chat-log", VerticalScroll).remove_children()
        self._set_stats("")
        self._update_tokens()
        self._mark_active()
        field = self.query_one("#chat-input", Input)
        if not field.disabled:
            field.focus()
