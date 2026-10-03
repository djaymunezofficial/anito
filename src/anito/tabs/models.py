"""Models tab: list, pull, unload and delete installed Ollama models."""

from __future__ import annotations

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
)


def format_size(num_bytes: int) -> str:
    # Decimal units, to match what `ollama list` prints.
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


class ModelsTab(Vertical):
    """Owns the model list. Other widgets get it through the Loaded and Failed messages."""

    BINDINGS = [
        Binding("r", "refresh_models", "Refresh"),
        Binding("p", "pull_model", "Pull"),
        Binding("u", "unload_model", "Unload"),
        Binding("d,delete", "delete_model", "Delete"),
    ]

    class Loaded(Message):
        def __init__(self, models: list[ModelInfo], running: list[str]) -> None:
            super().__init__()
            self.models = models
            self.running = running

    class Failed(Message):
        def __init__(self, error: str, offline: bool) -> None:
            super().__init__()
            self.error = error
            self.offline = offline

    def __init__(self, client: OllamaClient) -> None:
        super().__init__()
        self._ollama = client
        self._models: list[ModelInfo] = []
        self._loading = False
        self._working = False
        self._streaming = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Button("Refresh", id="btn-refresh")
            yield Button("Pull", id="btn-pull-model", variant="primary")
            yield Button("Unload", id="btn-unload", variant="warning")
            yield Button("Delete", id="btn-delete", variant="error")
        yield DataTable(id="models-table", cursor_type="row")
        yield Static("", id="models-empty", classes="empty-state hidden", markup=False)

    def on_mount(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.add_columns("Name", "Size", "Family", "Params", "Quant", "Modified")
        self._set_empty("Loading models...")
        self._sync_buttons()
        self.refresh_models()

    def refresh_models(self, *, announce: bool = False) -> None:
        self._loading = True
        self._sync_buttons()
        self._load(announce)

    def set_streaming(self, active: bool) -> None:
        """Called while a chat reply streams, so the loaded model can't be pulled out from under it."""
        self._streaming = active
        self._sync_buttons()

    @work(exclusive=True, group="models-refresh")
    async def _load(self, announce: bool) -> None:
        # No handling for cancellation: it means a newer refresh replaced this one and owns the flags now.
        try:
            models = await self._ollama.list_models()
        except OllamaError as exc:
            self._loading = False
            self._show_error(exc)
            return
        try:
            running = await self._ollama.running_models()
        except OllamaError:
            # Secondary info; a failure here shouldn't hide the model list.
            running = []
        self._loading = False
        self._show_models(models, running)
        if announce:
            self.notify("Model list refreshed")

    def _show_models(self, models: list[ModelInfo], running: list[str]) -> None:
        table = self.query_one("#models-table", DataTable)
        previous = self._selected()
        self._models = models
        table.clear()
        # Text() objects keep model names from being parsed as Rich markup.
        for model in models:
            table.add_row(
                Text(model.name),
                Text(format_size(model.size), justify="right"),
                Text(model.family),
                Text(model.parameter_size),
                Text(model.quantization),
                Text(model.modified),
            )
        if previous is not None:
            for index, model in enumerate(models):
                if model.name == previous.name:
                    table.move_cursor(row=index)
                    break
        self._set_empty(None if models else "No models installed yet. Press Pull to download one.")
        self._sync_buttons()
        self.post_message(self.Loaded(models, running))

    def _show_error(self, exc: OllamaError) -> None:
        self._models = []
        self.query_one("#models-table", DataTable).clear()
        offline = isinstance(exc, OllamaConnectionError)
        hint = "Start Ollama, then press Refresh." if offline else "Press Refresh to try again."
        self._set_empty(f"{exc}\n\n{hint}")
        self._sync_buttons()
        self.post_message(self.Failed(str(exc), offline))

    def _set_empty(self, text: str | None) -> None:
        self.query_one("#models-table", DataTable).set_class(text is not None, "hidden")
        empty = self.query_one("#models-empty", Static)
        empty.set_class(text is None, "hidden")
        empty.update(text or "")

    def _sync_buttons(self) -> None:
        can_modify = bool(self._models) and not (self._working or self._loading or self._streaming)
        self.query_one("#btn-refresh", Button).disabled = self._working or self._loading
        self.query_one("#btn-pull-model", Button).disabled = self._working
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
        if self._working or self._loading:
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
            "btn-unload": self.action_unload_model,
            "btn-delete": self.action_delete_model,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def action_refresh_models(self) -> None:
        if not (self._working or self._loading):
            self.refresh_models(announce=True)

    def action_pull_model(self) -> None:
        if self._working:
            return

        def on_done(name: str | None) -> None:
            if name:
                self.refresh_models()

        self.app.push_screen(PullDialog(self._ollama), on_done)

    def action_unload_model(self) -> None:
        model = self._selected_for_change()
        if model is None:
            return
        self._working = True
        self._sync_buttons()
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
                self._working = True
                self._sync_buttons()
                self._delete(model.name)

        self.app.push_screen(dialog, on_done)

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
        self._working = False
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
        self._working = False
        self.refresh_models()
