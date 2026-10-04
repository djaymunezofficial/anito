"""ANITO: terminal control deck for local Ollama models."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from functools import partial

from textual import work
from textual.app import App, ComposeResult, SuspendNotSupported, SystemCommand
from textual.binding import Binding
from textual.screen import ModalScreen, Screen

from anito.config import THEMES, load_config, save_config
from anito.dialogs.modals import ConfirmDialog, HelpDialog
from anito.utils.history import HistoryStore
from anito.tabs.chat import ChatHome
from anito.tabs.models import ModelsPane, fetch_catalog
from anito.tabs.settings import SettingsModal
from anito.themes import THEME_LABELS, register_themes, textual_name
from anito.utils.ollama_api import (
    OllamaClient,
    OllamaConnectionError,
    OllamaError,
)
from anito.utils.ollama_control import (
    Action,
    OllamaController,
    Outcome,
    detect,
    is_local_url,
    wait_for_state,
)

# Shown in the Help dialog. Keep in step with BINDINGS below.
_HELP_KEYS = (
    ("F1", "Help"),
    ("F2", "Settings"),
    ("Ctrl+P", "Command palette"),
    ("Ctrl+N", "New chat"),
    ("Esc", "Stop the reply, or close a dialog"),
    ("Ctrl+S", "Save (in Settings)"),
    ("Ctrl+Q", "Quit"),
)


class AnitoApp(App[None]):
    CSS_PATH = "styles.tcss"
    TITLE = "ANITO"

    ENABLE_COMMAND_PALETTE = True
    COMMAND_PALETTE_BINDING = "ctrl+p"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f1", "show_help", "Help", show=False),
        Binding("f2", "show_settings", "Settings", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
        # Registering has to happen before the stylesheet loads, and the theme
        # must already be ours then, or the $an-* variables are undefined.
        register_themes(self)
        self.theme = textual_name(self.cfg.theme)

        self.ollama = OllamaClient(self.cfg.ollama_url)
        self.home = ChatHome(self.ollama, self.cfg, HistoryStore())
        self._controller: OllamaController | None = None
        self._settings: SettingsModal | None = None
        self._streaming = False
        self._loading: str | None = None

    def compose(self) -> ComposeResult:
        yield self.home

    def on_mount(self) -> None:
        self.home.set_checking()
        self._startup()

    # ------------------------------------------------------------------ #
    # Startup: find a way to control Ollama, auto-start it, read the models
    # ------------------------------------------------------------------ #
    @work(exclusive=True, group="startup")
    async def _startup(self) -> None:
        controller = detect()
        self._controller = await controller if inspect.isawaitable(controller) else controller
        if (
            self.cfg.ollama_auto_start
            and self._controller is not None
            and is_local_url(self.cfg.ollama_url)
            and not await self._is_up()
        ):
            await self._auto_start(self._controller)
        await self._load_catalog()

    async def _is_up(self) -> bool:
        try:
            await self.ollama.version()
        except OllamaError:
            return False
        return True

    async def _auto_start(self, controller: OllamaController) -> None:
        self.notify("Starting Ollama...")
        outcome = await controller.run(Action.START)
        if outcome.needs_terminal:
            outcome = self._run_in_terminal(controller, Action.START)
        if not outcome.ok:
            self.notify(outcome.message or "Could not start Ollama", severity="error")
        elif not await wait_for_state(self.ollama, running=True):
            self.notify("Ollama didn't answer in time", severity="warning")

    def _run_in_terminal(self, controller: OllamaController, action: Action) -> Outcome:
        # sudo or polkit wants a password, so hand the terminal over for a moment.
        try:
            with self.suspend():
                return controller.run_in_terminal(action)
        except SuspendNotSupported:
            return Outcome(False, "This terminal can't ask for a password. Start Ollama yourself.")

    # ------------------------------------------------------------------ #
    # Model catalog
    # ------------------------------------------------------------------ #
    async def _load_catalog(self) -> None:
        try:
            catalog = await fetch_catalog(self.ollama)
        except OllamaError as exc:
            self.home.set_offline(str(exc), offline=isinstance(exc, OllamaConnectionError))
        else:
            self.home.set_models(catalog.models, catalog.running)

    @work(exclusive=True, group="catalog")
    async def refresh_catalog(self) -> None:
        self.home.set_checking()
        await self._load_catalog()

    def _refresh_models(self) -> None:
        """Re-read the models. With Settings open its table does it and reports back."""
        if self._settings is not None:
            self.home.set_checking()
            self._settings.query_one(ModelsPane).refresh_models()
        else:
            self.refresh_catalog()

    def on_models_pane_loaded(self, event: ModelsPane.Loaded) -> None:
        self.home.set_models(event.models, event.running)

    def on_models_pane_failed(self, event: ModelsPane.Failed) -> None:
        self.home.set_offline(event.error, offline=event.offline)

    def on_models_pane_load_requested(self, event: ModelsPane.LoadRequested) -> None:
        event.stop()
        if self._loading:
            self.notify(f"Wait for {self._loading} to finish loading", severity="warning")
            return
        self._load_model(event.name)

    @work(group="load-model")
    async def _load_model(self, name: str) -> None:
        # The app does the loading, so it carries on if Settings is closed meanwhile.
        self._set_loading(name)
        try:
            await self.ollama.load(name, keep_alive=self.cfg.keep_alive)
        except OllamaError as exc:
            self.notify(str(exc), title=f"Could not load {name}", severity="error")
        else:
            self.notify(f"Loaded {name}")
        self._set_loading(None)
        if self._settings is not None:
            self._settings.query_one(ModelsPane).refresh_models()
        else:
            await self._load_catalog()

    def _set_loading(self, name: str | None) -> None:
        self._loading = name
        if self._settings is not None:
            self._settings.set_loading(name)

    # ------------------------------------------------------------------ #
    # Messages from the home screen
    # ------------------------------------------------------------------ #
    def on_chat_home_stream_state(self, event: ChatHome.StreamState) -> None:
        self._streaming = event.active
        if self._settings is not None:
            self._settings.set_streaming(event.active)

    def on_chat_home_help_requested(self, event: ChatHome.HelpRequested) -> None:
        event.stop()
        self.action_show_help()

    def on_chat_home_settings_requested(self, event: ChatHome.SettingsRequested) -> None:
        event.stop()
        self.action_show_settings()

    async def on_chat_home_quit_requested(self, event: ChatHome.QuitRequested) -> None:
        event.stop()
        await self.action_quit()

    # ------------------------------------------------------------------ #
    # Help and Settings
    # ------------------------------------------------------------------ #
    def action_show_help(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.push_screen(HelpDialog(_HELP_KEYS))

    def action_show_settings(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        modal = SettingsModal(
            self.cfg,
            self.ollama,
            self._controller,
            streaming=self._streaming,
            loading=self._loading,
        )
        self._settings = modal
        self.push_screen(modal, self._on_settings_closed)

    def _on_settings_closed(self, _result: None) -> None:
        self._settings = None
        self.home.refresh_context()

    def on_settings_modal_saved(self, event: SettingsModal.Saved) -> None:
        self.home.refresh_context()
        if event.url_changed:
            self.ollama.base_url = self.cfg.ollama_url
            self.home.refresh_url()
            self._refresh_models()

    # ------------------------------------------------------------------ #
    # Command palette
    # ------------------------------------------------------------------ #
    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        # Textual's own theme picker lists its built-in themes, which don't
        # define the $an-* colors. Ours replaces it below.
        for command in super().get_system_commands(screen):
            if command.title != "Theme":
                yield command
        yield SystemCommand("Settings", "Open the settings popup", self.action_show_settings)
        yield SystemCommand("Help", "Show keys and tips", self.action_show_help)
        yield SystemCommand("New chat", "Start an empty conversation", self.home.action_new_chat)
        yield SystemCommand("Stop generation", "Cancel the reply that is streaming", self.home.action_stop_generation)
        yield SystemCommand("Refresh models", "Reload the installed model list", self._refresh_models)
        for key in THEMES:
            yield SystemCommand(
                f"Theme: {THEME_LABELS[key]}",
                "Switch the color theme",
                partial(self._set_theme, key),
            )

    def _set_theme(self, key: str) -> None:
        self.theme = textual_name(key)
        self.cfg.theme = key
        try:
            save_config(self.cfg)
        except OSError as exc:
            self.notify(f"Theme applied, but could not save it: {exc}", severity="warning")

    # ------------------------------------------------------------------ #
    # Quitting
    # ------------------------------------------------------------------ #
    async def action_quit(self) -> None:
        # Quitting mid-reply throws the reply away, so ask first.
        if not self._streaming:
            self.exit()
            return

        def on_done(confirmed: bool | None) -> None:
            if confirmed:
                self.exit()

        self.push_screen(
            ConfirmDialog(
                "Quit ANITO",
                "A reply is still streaming. Quit anyway?",
                confirm_label="Quit",
                danger=True,
            ),
            on_done,
        )

    async def on_unmount(self) -> None:
        await self.ollama.close()


def main() -> None:
    AnitoApp().run()


if __name__ == "__main__":
    main()
