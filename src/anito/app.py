"""ANITO: terminal control center for local Ollama models."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from anito import __version__
from anito.config import load_config
from anito.dialogs.modals import ConfirmDialog
from anito.tabs.chat import ChatTab
from anito.tabs.models import ModelsTab
from anito.tabs.settings import SettingsTab
from anito.utils.ollama_api import OllamaClient

W = TypeVar("W", bound=Widget)


class AnitoApp(App[None]):
    CSS_PATH = "styles.tcss"
    TITLE = "ANITO"
    SUB_TITLE = f"{__version__} beta"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f1", "show_tab('tab-chat')", "Chat", show=False),
        Binding("f2", "show_tab('tab-models')", "Models", show=False),
        Binding("f3", "show_tab('tab-settings')", "Settings", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
        self.ollama = OllamaClient(self.cfg.ollama_url)
        self._streaming = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="tab-chat", id="tabs"):
            with TabPane("Chat", id="tab-chat"):
                yield ChatTab(self.ollama, self.cfg)
            with TabPane("Models", id="tab-models"):
                yield ModelsTab(self.ollama)
            with TabPane("Settings", id="tab-settings"):
                yield SettingsTab(self.cfg)
        with Horizontal(id="status-bar"):
            yield Static("Ollama: connecting...", id="status-conn", classes="status-item checking", markup=False)
            yield Static("Model: none", id="status-model", classes="status-item", markup=False)
            yield Static("", id="status-ctx", classes="status-item", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._update_context()

    def _find(self, widget_type: type[W]) -> W:
        # The base screen stays at the bottom of the stack, so this works even with a modal open.
        return self.screen_stack[0].query_one(widget_type)

    def _set_connection(self, state: str, text: str) -> None:
        conn = self.query_one("#status-conn", Static)
        conn.set_classes(f"status-item {state}")
        conn.update(text)

    def _update_context(self) -> None:
        self.query_one("#status-ctx", Static).update(f"Context: {self.cfg.num_ctx} tokens")

    def on_models_tab_loaded(self, event: ModelsTab.Loaded) -> None:
        self._set_connection("online", f"Ollama: connected ({self.cfg.ollama_url})")
        self._find(ChatTab).set_models(event.models)

    def on_models_tab_failed(self, event: ModelsTab.Failed) -> None:
        self._set_connection("offline", "Ollama: offline" if event.offline else "Ollama: error")
        self._find(ChatTab).set_offline(event.error)

    def on_chat_tab_model_changed(self, event: ChatTab.ModelChanged) -> None:
        self.query_one("#status-model", Static).update(f"Model: {event.model or 'none'}")

    def on_chat_tab_stream_state(self, event: ChatTab.StreamState) -> None:
        self._streaming = event.active
        self._find(ModelsTab).set_streaming(event.active)
        self._find(SettingsTab).set_streaming(event.active)

    def on_settings_tab_saved(self, event: SettingsTab.Saved) -> None:
        self._update_context()
        if event.url_changed:
            self.ollama.base_url = self.cfg.ollama_url
            self._set_connection("checking", "Ollama: connecting...")
            self._find(ModelsTab).refresh_models()

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

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

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        # The theme picker would do nothing useful: colors come from styles.tcss.
        for command in super().get_system_commands(screen):
            if command.title != "Theme":
                yield command
        yield SystemCommand("New chat", "Clear the conversation and start over", self._cmd_new_chat)
        yield SystemCommand("Stop generation", "Cancel the reply that is streaming", self._cmd_stop)
        yield SystemCommand("Refresh models", "Reload the installed model list", self._cmd_refresh)
        yield SystemCommand("Pull model", "Download a model from the Ollama library", self._cmd_pull)
        yield SystemCommand("Go to Chat", "Switch to the Chat tab", self._cmd_show_chat)
        yield SystemCommand("Go to Models", "Switch to the Models tab", self._cmd_show_models)
        yield SystemCommand("Go to Settings", "Switch to the Settings tab", self._cmd_show_settings)

    def _cmd_new_chat(self) -> None:
        self.action_show_tab("tab-chat")
        self._find(ChatTab).action_new_chat()

    def _cmd_stop(self) -> None:
        self._find(ChatTab).action_stop_generation()

    def _cmd_refresh(self) -> None:
        self._find(ModelsTab).action_refresh_models()

    def _cmd_pull(self) -> None:
        self._find(ModelsTab).action_pull_model()

    def _cmd_show_chat(self) -> None:
        self.action_show_tab("tab-chat")

    def _cmd_show_models(self) -> None:
        self.action_show_tab("tab-models")

    def _cmd_show_settings(self) -> None:
        self.action_show_tab("tab-settings")

    async def on_unmount(self) -> None:
        await self.ollama.close()


def main() -> None:
    AnitoApp().run()


if __name__ == "__main__":
    main()
