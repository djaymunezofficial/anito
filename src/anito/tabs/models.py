"""Models pane: list, load, pull, unload and delete installed Ollama models."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, DataTable, Static

from anito.dialogs.modals import ConfirmDialog, PullDialog
from anito.utils.ollama_api import (
    ModelInfo,
    OllamaClient,
    OllamaConnectionError,
    OllamaError,
    RunningModel,
)


def format_size(num_bytes: int) -> str:
    # Decimal units, to match what `ollama list` prints.
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


@dataclass(frozen=True)
class Catalog:
    models: list[ModelInfo]
    running: list[RunningModel]


async def fetch_catalog(client: OllamaClient) -> Catalog:
    """Installed models plus what is in memory. Raises OllamaError if the list can't be read."""
    models = await client.list_models()
    try:
        running = await client.running()
    except OllamaError:
        # Secondary info; a failure here shouldn't hide the model list.
        running = []
    return Catalog(models, running)


class ModelsPane(Vertical):
    """Owns the model table. Other parts of the app hear about it through Loaded and Failed."""

    BINDINGS = [
        Binding("r", "refresh_models", "Refresh"),
        Binding("p", "pull_model", "Pull"),
        Binding("l", "load_model", "Load"),
        Binding("u", "unload_model", "Unload"),
        Binding("d,delete", "delete_model", "Delete"),
    ]

    class Loaded(Message):
        def __init__(self, models: list[ModelInfo], running: list[RunningModel]) -> None:
            super().__init__()
            self.models = models
            self.running = running

    class Failed(Message):
        def __init__(self, error: str, offline: bool) -> None:
            super().__init__()
            self.error = error
            self.offline = offline

    class LoadRequested(Message):
        """The user asked to load a model. The app does the loading, so it survives closing Settings."""

        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    def __init__(
        self,
        client: OllamaClient,
        *,
        streaming: bool = False,
        loading: str | None = None,
    ) -> None:
        super().__init__(id="pane-models", classes="pane")
        self._ollama = client
        self._models: list[ModelInfo] = []
        self._running: dict[str, RunningModel] = {}
        # Per-model text that replaces the Loaded column while something is happening to it.
        self._busy: dict[str, str] = {}
        self._load_name: str | None = None
        self._refreshing = False
        self._working = False
        self._streaming = streaming
        if loading:
            self._load_name = loading
            self._busy[loading] = "loading..."

    def compose(self) -> ComposeResult:
        yield Static("Models", classes="pane-title")
        with Horizontal(classes="toolbar"):
            yield Button("Refresh", id="btn-refresh")
            yield Button("Pull", id="btn-pull-model", variant="primary")
            yield Button("Load", id="btn-load")
            yield Button("Unload", id="btn-unload", variant="warning")
            yield Button("Delete", id="btn-delete", variant="error")
        yield DataTable(id="models-table", cursor_type="row")
        yield Static("", id="models-empty", classes="empty-state hidden", markup=False)

    def on_mount(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.add_columns("Name", "Loaded", "Size", "Family", "Params", "Quant", "Modified")
        self._set_empty("Loading models...")
        self._sync_buttons()
        self.refresh_models()

    def refresh_models(self, *, announce: bool = False) -> None:
        self._refreshing = True
        self._sync_buttons()
        self._fetch(announce)

    def set_streaming(self, active: bool) -> None:
        """Called while a chat reply streams, so the model in use can't be pulled out from under it."""
        self._streaming = active
        self._sync_buttons()

    def set_loading(self, name: str | None) -> None:
        """Called by the app while it loads a model, so the table can show it."""
        if self._load_name:
            self._busy.pop(self._load_name, None)
        self._load_name = name
        if name:
            self._busy[name] = "loading..."
        self._render_table()
        self._sync_buttons()

    @work(exclusive=True, group="models-refresh")
    async def _fetch(self, announce: bool) -> None:
        # No handling for cancellation: it means a newer refresh replaced this one and owns the flags now.
        try:
            catalog = await fetch_catalog(self._ollama)
        except OllamaError as exc:
            self._refreshing = False
            self._show_error(exc)
            return
        self._refreshing = False
        self._show_models(catalog)
        if announce:
            self.notify("Model list refreshed")

    def _show_models(self, catalog: Catalog) -> None:
        self._models = catalog.models
        self._running = {m.name: m for m in catalog.running}
        self._render_table()
        self._set_empty(
            None if catalog.models else "No models installed yet. Press Pull to download one."
        )
        self._sync_buttons()
        self.post_message(self.Loaded(catalog.models, catalog.running))

    def _render_table(self) -> None:
        table = self.query_one("#models-table", DataTable)
        previous = self._selected()
        table.clear()
        # Text() objects keep model names from being parsed as Rich markup.
        for model in self._models:
            table.add_row(
                Text(model.name),
                Text(self._loaded_label(model.name)),
                Text(format_size(model.size), justify="right"),
                Text(model.family),
                Text(model.parameter_size),
                Text(model.quantization),
                Text(model.modified),
            )
        if previous is not None:
            for index, model in enumerate(self._models):
                if model.name == previous.name:
                    table.move_cursor(row=index)
                    break

    def _loaded_label(self, name: str) -> str:
        busy = self._busy.get(name)
        if busy:
            return busy
        running = self._running.get(name)
        return format_size(running.size) if running else "-"

    def _show_error(self, exc: OllamaError) -> None:
        self._models = []
        self._running = {}
        self.query_one("#models-table", DataTable).clear()
        offline = isinstance(exc, OllamaConnectionError)
        hint = (
            "Start Ollama (Settings, Ollama Settings), then press Refresh."
            if offline
            else "Press Refresh to try again."
        )
        self._set_empty(f"{exc}\n\n{hint}")
        self._sync_buttons()
        self.post_message(self.Failed(str(exc), offline))

    def _set_empty(self, text: str | None) -> None:
        self.query_one("#models-table", DataTable).set_class(text is not None, "hidden")
        empty = self.query_one("#models-empty", Static)
        empty.set_class(text is None, "hidden")
        empty.update(text or "")

    def _sync_buttons(self) -> None:
        idle = not (self._working or self._refreshing or self._streaming or self._load_name)
        can_modify = bool(self._models) and idle
        self.query_one("#btn-refresh", Button).disabled = self._working or self._refreshing
        self.query_one("#btn-pull-model", Button).disabled = self._working
        self.query_one("#btn-load", Button).disabled = not can_modify
        self.query_one("#btn-unload", Button).disabled = not can_modify
        self.query_one("#btn-delete", Button).disabled = not can_modify

    def _selected(self) -> ModelInfo | None:
        table = self.query_one("#models-table", DataTable)
        if not self._models or table.row_count == 0:
            return None
        index = table.cursor_row
        return self._models[index] if 0 <= index < len(self._models) else None

    def _selected_for_change(self) -> ModelInfo | None:
        if self._streaming:
            self.notify("Wait for the chat reply to finish, or stop it first", severity="warning")
            return None
        if self._load_name:
            self.notify(f"Wait for {self._load_name} to finish loading", severity="warning")
            return None
        if self._working or self._refreshing:
            return None
        model = self._selected()
        if model is None:
            self.notify("Select a model first", severity="warning")
        return model

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions = {
            "btn-refresh": self.action_refresh_models,
            "btn-pull-model": self.action_pull_model,
            "btn-load": self.action_load_model,
            "btn-unload": self.action_unload_model,
            "btn-delete": self.action_delete_model,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def action_refresh_models(self) -> None:
        if not (self._working or self._refreshing):
            self.refresh_models(announce=True)

    def action_pull_model(self) -> None:
        if self._working:
            return

        def on_done(name: str | None) -> None:
            if name:
                self.refresh_models()

        self.app.push_screen(PullDialog(self._ollama), on_done)

    def action_load_model(self) -> None:
        model = self._selected_for_change()
        if model is None:
            return
        if model.name in self._running:
            self.notify(f"{model.name} is already loaded")
            return
        self.post_message(self.LoadRequested(model.name))

    def action_unload_model(self) -> None:
        model = self._selected_for_change()
        if model is None:
            return
        self._begin_work(model.name, "unloading...")
        self._unload(model.name)

    def action_delete_model(self) -> None:
        model = self._selected_for_change()
        if model is None:
            return
        dialog = ConfirmDialog(
            "Delete model",
            f"Delete {model.name} ({format_size(model.size)})? This can't be undone.",
            confirm_label="Delete",
            danger=True,
        )

        def on_done(confirmed: bool | None) -> None:
            if confirmed:
                self._begin_work(model.name, "deleting...")
                self._delete(model.name)

        self.app.push_screen(dialog, on_done)

    def _begin_work(self, name: str, status: str) -> None:
        self._working = True
        self._busy[name] = status
        self._render_table()
        self._sync_buttons()

    def _end_work(self, name: str) -> None:
        self._working = False
        self._busy.pop(name, None)

    @work(group="models-action")
    async def _unload(self, name: str) -> None:
        severity = "information"
        try:
            # Check fresh state; the last refresh may be stale.
            if name in await self._ollama.running_models():
                await self._ollama.unload(name)
                message = f"Unloaded {name} from memory"
            else:
                message = f"{name} isn't loaded in memory"
        except OllamaError as exc:
            message, severity = str(exc), "error"
        self._end_work(name)
        self.notify(message, severity=severity)
        self.refresh_models()

    @work(group="models-action")
    async def _delete(self, name: str) -> None:
        try:
            await self._ollama.delete(name)
        except OllamaError as exc:
            self.notify(str(exc), title="Delete failed", severity="error")
        else:
            self.notify(f"Deleted {name}")
        self._end_work(name)
        self.refresh_models()
