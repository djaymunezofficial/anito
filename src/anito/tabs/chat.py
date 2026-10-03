"""Chat tab: model sidebar and streaming conversation."""

from __future__ import annotations

import contextlib
import time
from datetime import datetime

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Label, Markdown, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import Worker

from anito.config import Config, save_config
from anito.dialogs.modals import ConfirmDialog
from anito.utils.ollama_api import ChatChunk, ModelInfo, OllamaClient, OllamaError

CURSOR = "\u258c"
# Each update re-lays out the growing message, so cap redraws at about ten a second.
FLUSH_INTERVAL = 0.1

_ROLE_NAMES = {"user": "You", "system": "System", "error": "Error"}


def _stats_text(final: ChatChunk | None) -> str:
    if final is None or not final.eval_count:
        return ""
    if final.eval_duration > 0:
        rate = final.eval_count / (final.eval_duration / 1e9)
        return f"{final.eval_count} tokens, {rate:.1f} tok/s"
    return f"{final.eval_count} tokens"


class ChatMessage(Vertical):
    def __init__(self, role: str, content: str, *, label: str, streaming: bool = False) -> None:
        classes = f"message {role} streaming" if streaming else f"message {role}"
        super().__init__(classes=classes)
        self._label = f"{label}  {datetime.now().strftime('%H:%M')}"
        # markup=False so brackets in model text can't be read as Rich markup.
        self.body: Static | Markdown = Static(content, classes="message-body", markup=False)

    def compose(self) -> ComposeResult:
        yield Static(self._label, classes="message-role", markup=False)
        yield self.body

    def finalize(self, text: str) -> None:
        # Plain text while streaming; Markdown is parsed once at the end to keep CPU use down.
        self.remove_class("streaming")
        self.body.remove()
        self.body = Markdown(text, classes="message-body")
        self.mount(self.body)


class ChatTab(Horizontal):
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

    def __init__(self, client: OllamaClient, config: Config) -> None:
        super().__init__(id="chat-layout")
        self._ollama = client
        self._config = config
        self._models: list[str] = []
        self._current = ""
        self._online = False
        self._history: list[dict[str, str]] = []
        self._streaming = False
        self._stream_worker: Worker[None] | None = None
        self._bubble: ChatMessage | None = None
        self._reply = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="chat-sidebar"):
            yield Label("Local models", id="sidebar-title")
            yield OptionList(id="model-list")
            yield Button("New chat", id="btn-new-chat")
        with Vertical(id="chat-main"):
            yield Static("", id="chat-notice", classes="notice hidden", markup=False)
            yield VerticalScroll(id="chat-log")
            yield Static("", id="chat-stats", markup=False)
            with Horizontal(id="chat-input-row"):
                yield Input(placeholder="", id="chat-input", disabled=True)
                yield Button("Send", id="btn-send", variant="primary", disabled=True)
                yield Button("Stop", id="btn-stop", variant="error", classes="hidden")

    def on_mount(self) -> None:
        self._sync_controls()

    def set_models(self, models: list[ModelInfo]) -> None:
        self._online = True
        self._models = [m.name for m in models]
        previous = self._current
        if previous not in self._models:
            self._current = self._default_model()
            if previous:
                after = f"Using {self._current}." if self._current else "No models left."
                self._add_message("system", f"{previous} is no longer installed. {after}")
        self._set_notice(
            None if self._models else "No models installed. Open the Models tab and pull one."
        )
        self._rebuild_options()
        self._sync_controls()
        if self._current != previous:
            self.post_message(self.ModelChanged(self._current))

    def set_offline(self, message: str) -> None:
        self._online = False
        self._models = []
        self._set_notice(f"{message}\nCheck Ollama, then refresh on the Models tab.", error=True)
        self._rebuild_options()
        self._sync_controls()

    def _default_model(self) -> str:
        if self._config.last_model in self._models:
            return self._config.last_model
        return self._models[0] if self._models else ""

    def _rebuild_options(self) -> None:
        options = self.query_one("#model-list", OptionList)
        options.clear_options()
        options.add_options(
            [
                Option(
                    Text(f"> {name}", style="bold") if name == self._current else Text(f"  {name}"),
                    id=name,
                )
                for name in self._models
            ]
        )
        if self._current in self._models:
            options.highlighted = self._models.index(self._current)

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
            field.placeholder = "Type a message and press Enter"
        send = self.query_one("#btn-send", Button)
        send.disabled = field.disabled
        send.set_class(streaming, "hidden")
        self.query_one("#btn-stop", Button).set_class(not streaming, "hidden")
        self.query_one("#model-list", OptionList).disabled = streaming
        self.query_one("#btn-new-chat", Button).disabled = streaming

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

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Only Enter or a click switches models; moving the highlight must not.
        event.stop()
        name = event.option.id
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
        self._rebuild_options()
        self._sync_controls()
        self._add_message("system", f"Switched to {name}.")
        self.post_message(self.ModelChanged(name))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "btn-send":
            self._submit()
        elif event.button.id == "btn-stop":
            self.action_stop_generation()
        elif event.button.id == "btn-new-chat":
            self.action_new_chat()

    def _submit(self) -> None:
        field = self.query_one("#chat-input", Input)
        text = field.value.strip()
        if self._streaming or field.disabled or not text:
            return
        field.value = ""
        self._history.append({"role": "user", "content": text})
        self._add_message("user", text)
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
        # The disabled input can't hold focus, and Esc only works with focus inside this tab.
        self.call_after_refresh(self.query_one("#btn-stop", Button).focus)

    @work
    async def _run_chat(self, model: str, messages: list[dict[str, str]]) -> None:
        cfg = self._config
        phase = "waiting"
        last_flush = 0.0
        final: ChatChunk | None = None
        try:
            async with contextlib.aclosing(
                self._ollama.chat(model, messages, temperature=cfg.temperature, num_ctx=cfg.num_ctx)
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
        if not self._history:
            self._reset_chat()
            return

        def on_done(confirmed: bool | None) -> None:
            if confirmed:
                self._reset_chat()

        self.app.push_screen(
            ConfirmDialog(
                "New chat",
                "Start a new chat? The current conversation will be cleared.",
                confirm_label="New chat",
                danger=True,
            ),
            on_done,
        )

    def _reset_chat(self) -> None:
        self.query_one("#chat-log", VerticalScroll).remove_children()
        self._history.clear()
        self._set_stats("")
        self.notify("New chat started")
        field = self.query_one("#chat-input", Input)
        if not field.disabled:
            field.focus()
