"""Settings tab: edit generation options and the Ollama URL."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Input, Label, Static, Switch, TextArea

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
from anito.tabs.models import ModelsPane
from anito.themes import THEME_LABELS, textual_name
from anito.utils.ollama_api import OllamaClient
from anito.utils.ollama_control import OllamaController

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
        yield Label("Start Ollama automatically", classes="field-label")
        yield Switch(value=cfg.ollama_auto_start, id="set-autostart")

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
        values["ollama_auto_start"] = self.query_one("#set-autostart", Switch).value

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
        self.query_one("#set-autostart", Switch).value = cfg.ollama_auto_start
        for widget in self.query(".invalid"):
            widget.remove_class("invalid")


class SettingsModal(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close_settings", "Close", show=False)]

    class Saved(Message):
        def __init__(self, url_changed: bool) -> None:
            super().__init__()
            self.url_changed = url_changed

    _PANES = {
        "nav-ollama": "pane-ollama",
        "nav-models": "pane-models",
        "nav-appearance": "pane-appearance",
        "nav-skills": "pane-skills",
    }

    def __init__(
        self,
        config: Config,
        client: OllamaClient,
        controller: OllamaController | None,
        *,
        streaming: bool = False,
        loading: str | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._client = client
        self._controller = controller
        self._streaming = streaming
        self._loading = loading

    def compose(self) -> ComposeResult:
        with Horizontal(id="settings-dialog"):
            with Vertical(id="settings-nav"):
                yield Static("Settings", id="settings-title", markup=False)
                yield Button("Ollama", id="nav-ollama", classes="nav-link active")
                yield Button("Models", id="nav-models", classes="nav-link")
                yield Button("Appearance", id="nav-appearance", classes="nav-link")
                yield Button("Skills", id="nav-skills", classes="nav-link")
                yield Button("Back", id="nav-back", classes="nav-link")
            with ContentSwitcher(id="settings-content", initial="pane-ollama"):
                with Vertical(id="pane-ollama", classes="pane"):
                    yield SettingsTab(self._config)
                with Vertical(id="pane-models", classes="pane"):
                    yield ModelsPane(
                        self._client,
                        streaming=self._streaming,
                        loading=self._loading,
                    )
                with Vertical(id="pane-appearance", classes="pane"):
                    yield Static("Appearance", classes="pane-title", markup=False)
                    for key, label in THEME_LABELS.items():
                        yield Button(label, id=f"theme-{key}")
                with Vertical(id="pane-skills", classes="pane"):
                    yield Static("Skills", classes="pane-title", markup=False)
                    yield Static(
                        "Skills are available as slash commands and model tools.",
                        markup=False,
                    )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "nav-back":
            event.stop()
            self.dismiss(None)
            return
        pane = self._PANES.get(button_id)
        if pane is not None:
            event.stop()
            self.query_one("#settings-content", ContentSwitcher).current = pane
            for nav_id in self._PANES:
                self.query_one(f"#{nav_id}", Button).set_class(nav_id == button_id, "active")
            return
        if button_id.startswith("theme-"):
            event.stop()
            self._set_theme(button_id.removeprefix("theme-"))

    def on_settings_tab_saved(self, event: SettingsTab.Saved) -> None:
        event.stop()
        self.post_message(self.Saved(event.url_changed))

    def _set_theme(self, key: str) -> None:
        if key not in THEME_LABELS:
            return
        self.app.theme = textual_name(key)
        self._config.theme = key
        try:
            save_config(self._config)
        except OSError as exc:
            self.notify(f"Theme applied, but could not save it: {exc}", severity="warning")

    def set_streaming(self, active: bool) -> None:
        self._streaming = active
        if self.is_mounted:
            self.query_one("#set-url", Input).disabled = active
            self.query_one(ModelsPane).set_streaming(active)

    def set_loading(self, name: str | None) -> None:
        self._loading = name
        if self.is_mounted:
            self.query_one(ModelsPane).set_loading(name)

    def action_close_settings(self) -> None:
        self.dismiss(None)
