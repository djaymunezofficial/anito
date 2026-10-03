"""Settings tab: edit generation options and the Ollama URL."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Label, Static, TextArea

from anito.config import (
    NUM_CTX_RANGE,
    TEMPERATURE_RANGE,
    Config,
    parse_num_ctx,
    parse_temperature,
    parse_text,
    parse_url,
    save_config,
)

# Order matches the form, so the first error reported is the topmost field.
_INPUT_FIELDS = (
    ("temperature", "#set-temperature", parse_temperature),
    ("num_ctx", "#set-num-ctx", parse_num_ctx),
    ("ollama_url", "#set-url", parse_url),
)


class SettingsTab(VerticalScroll):
    """Edits the shared Config in place. Nothing is applied unless every field is valid."""

    BINDINGS = [Binding("ctrl+s", "save_settings", "Save")]

    class Saved(Message):
        """Posted after settings are applied. The Config is already updated when this arrives."""

        def __init__(self, url_changed: bool) -> None:
            super().__init__()
            self.url_changed = url_changed

    def __init__(self, config: Config) -> None:
        super().__init__(id="settings-form")
        self._config = config

    def compose(self) -> ComposeResult:
        cfg = self._config
        temp_low, temp_high = TEMPERATURE_RANGE
        ctx_low, ctx_high = NUM_CTX_RANGE

        yield Label("Temperature", classes="field-label")
        yield Static(
            f"{temp_low} to {temp_high}. Lower is more focused, higher is more varied.",
            classes="field-hint",
        )
        yield Input(value=str(cfg.temperature), id="set-temperature")

        yield Label("Context length", classes="field-label")
        yield Static(
            f"Tokens of context, {ctx_low} to {ctx_high}. Larger values use more memory.",
            classes="field-hint",
        )
        yield Input(value=str(cfg.num_ctx), id="set-num-ctx")

        yield Label("System prompt", classes="field-label")
        yield Static("Sent before every chat. Leave empty for none.", classes="field-hint")
        yield TextArea(cfg.system_prompt, id="set-system-prompt", classes="field-multiline")

        yield Label("Ollama URL", classes="field-label")
        yield Static("Where Ollama is running. Applied as soon as you save.", classes="field-hint")
        yield Input(value=cfg.ollama_url, id="set-url")

        with Horizontal(classes="form-actions"):
            yield Button("Save", id="btn-save", variant="primary")
            yield Button("Revert", id="btn-revert")

    def set_streaming(self, active: bool) -> None:
        """Lock the URL while a reply streams; the other fields only take effect on the next message."""
        self.query_one("#set-url", Input).disabled = active

    def on_input_changed(self, event: Input.Changed) -> None:
        event.input.remove_class("invalid")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        event.text_area.remove_class("invalid")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_save_settings()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "btn-save":
            self.action_save_settings()
        elif event.button.id == "btn-revert":
            self.action_revert_settings()

    def action_save_settings(self) -> None:
        values: dict[str, Any] = {}
        first_error: str | None = None
        first_bad: Input | None = None

        for name, selector, parser in _INPUT_FIELDS:
            field = self.query_one(selector, Input)
            try:
                values[name] = parser(field.value)
            except ValueError as exc:
                field.add_class("invalid")
                if first_error is None:
                    first_error, first_bad = str(exc), field

        if first_error is not None:
            self.notify(first_error, title="Can't save", severity="warning")
            if first_bad is not None:
                first_bad.focus()
            return

        values["system_prompt"] = parse_text(self.query_one("#set-system-prompt", TextArea).text).strip()

        url_changed = values["ollama_url"] != self._config.ollama_url
        for name, value in values.items():
            setattr(self._config, name, value)

        try:
            save_config(self._config)
        except OSError as exc:
            self.notify(
                f"Applied for this session, but could not save to disk: {exc}",
                title="Settings not saved",
                severity="warning",
            )
        else:
            self.notify("Settings saved")

        # Show the cleaned-up values, e.g. a URL that got http:// added.
        self._load_fields()
        self.post_message(self.Saved(url_changed))

    def action_revert_settings(self) -> None:
        self._load_fields()
        self.notify("Changes discarded")

    def _load_fields(self) -> None:
        cfg = self._config
        self.query_one("#set-temperature", Input).value = str(cfg.temperature)
        self.query_one("#set-num-ctx", Input).value = str(cfg.num_ctx)
        self.query_one("#set-system-prompt", TextArea).text = cfg.system_prompt
        self.query_one("#set-url", Input).value = cfg.ollama_url
        for widget in self.query(".invalid"):
            widget.remove_class("invalid")
