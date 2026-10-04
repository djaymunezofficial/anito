"""Modal dialogs shared across the app."""

from __future__ import annotations

import contextlib
from collections.abc import Collection, Sequence

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, ProgressBar, Static
from textual.widgets.option_list import Option
from textual.worker import Worker

from anito.utils.ollama_api import OllamaClient, OllamaError, PullProgress


class ConfirmDialog(ModalScreen[bool]):
    """Yes/no prompt. Dismisses with True only when the user confirms."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self,
        title: str,
        message: str,
        *,
        confirm_label: str = "Confirm",
        danger: bool = False,
    ) -> None:
        super().__init__()
        self._heading = title
        self._body_text = message
        self._confirm_label = confirm_label
        self._danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog danger" if self._danger else "dialog"):
            yield Label(self._heading, classes="dialog-title", markup=False)
            yield Static(self._body_text, classes="dialog-body", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="btn-cancel")
                yield Button(
                    self._confirm_label,
                    id="btn-confirm",
                    variant="error" if self._danger else "primary",
                )

    def on_mount(self) -> None:
        # Cancel is the safe default for destructive prompts.
        target = "#btn-cancel" if self._danger else "#btn-confirm"
        self.query_one(target, Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "btn-confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ModelPickerDialog(ModalScreen[str | None]):
    """Pick one of the installed models. Dismisses with its name, or None if cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self,
        models: Sequence[str],
        current: str,
        loaded: Collection[str] = (),
    ) -> None:
        super().__init__()
        self._models = list(models)
        self._current = current
        self._loaded = set(loaded)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Choose model", classes="dialog-title")
            if self._models:
                yield OptionList(*self._options(), id="picker-list")
            else:
                yield Static(
                    "No models installed. Pull one from Settings, under Models.",
                    classes="dialog-body",
                    markup=False,
                )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="btn-cancel")
                yield Button("Use", id="btn-use", variant="primary", disabled=not self._models)

    def _options(self) -> list[Option]:
        options = []
        for name in self._models:
            is_current = name == self._current
            label = Text.assemble(
                (f"> {name}" if is_current else f"  {name}", "bold" if is_current else ""),
                ("  loaded", "dim") if name in self._loaded else "",
            )
            options.append(Option(label, id=name))
        return options

    def on_mount(self) -> None:
        if not self._models:
            self.query_one("#btn-cancel", Button).focus()
            return
        options = self.query_one("#picker-list", OptionList)
        if self._current in self._models:
            options.highlighted = self._models.index(self._current)
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id != "btn-use":
            self.action_cancel()
            return
        index = self.query_one("#picker-list", OptionList).highlighted
        if index is None:
            self.notify("Select a model first", severity="warning")
            return
        self.dismiss(self._models[index])

    def action_cancel(self) -> None:
        self.dismiss(None)


def _columns(rows: Sequence[tuple[str, str]]) -> list[str]:
    width = max((len(left) for left, _ in rows), default=0)
    return [f"  {left.ljust(width)}  {right}" for left, right in rows]


class HelpDialog(ModalScreen[None]):
    """Lists the keys and slash commands. The caller supplies both, so they can't drift from the app."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("enter", "close", "Close", show=False),
    ]

    def __init__(
        self,
        keys: Sequence[tuple[str, str]],
        commands: Sequence[tuple[str, str]] = (),
    ) -> None:
        super().__init__()
        self._keys = keys
        self._commands = commands

    def compose(self) -> ComposeResult:
        lines = ["Keys", *_columns(self._keys)]
        if self._commands:
            lines += ["", "Slash commands", *_columns(self._commands)]
        with Vertical(classes="dialog wide"):
            yield Label("Help", classes="dialog-title")
            with VerticalScroll(id="help-body"):
                yield Static("\n".join(lines), markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Close", id="btn-close-help", variant="primary")

    def on_mount(self) -> None:
        # Leave room for the title, buttons and padding on short terminals.
        body = self.query_one("#help-body", VerticalScroll)
        body.styles.max_height = max(5, min(24, self.app.size.height - 10))
        body.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_close()

    def action_close(self) -> None:
        self.dismiss(None)


def _validate_model_name(name: str) -> str | None:
    if not name:
        return "Enter a model name to pull"
    if any(ch.isspace() for ch in name):
        return "Model names can't contain spaces"
    return None


class PullDialog(ModalScreen[str | None]):
    """Ask for a model name and download it. Dismisses with the name, or None if cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, client: OllamaClient) -> None:
        super().__init__()
        self._ollama = client
        self._pull_worker: Worker[None] | None = None
        self._last_key: tuple[str, int] | None = None
        self._saw_success = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Pull model", classes="dialog-title")
            yield Static("Model name, e.g. qwen3:8b", classes="dialog-body", markup=False)
            yield Input(placeholder="name[:tag]", id="pull-name")
            yield ProgressBar(show_eta=False, id="pull-progress", classes="hidden")
            yield Static("", id="pull-status", classes="hidden", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="btn-cancel")
                yield Button("Pull", id="btn-pull", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#pull-name", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.input.remove_class("invalid")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._start_pull()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "btn-pull":
            self._start_pull()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        if self._pull_worker is not None:
            self._pull_worker.cancel()
            self._pull_worker = None
            self.notify("Pull cancelled")
        self.dismiss(None)

    def _start_pull(self) -> None:
        if self._pull_worker is not None:
            return
        field = self.query_one("#pull-name", Input)
        name = field.value.strip()
        error = _validate_model_name(name)
        if error:
            field.add_class("invalid")
            self.notify(error, severity="warning")
            return
        self._enter_busy()
        self._pull_worker = self._run_pull(name)

    def _enter_busy(self) -> None:
        self._last_key = None
        self._saw_success = False
        self.query_one("#pull-name", Input).disabled = True
        self.query_one("#btn-pull", Button).disabled = True
        bar = self.query_one("#pull-progress", ProgressBar)
        bar.update(total=None, progress=0)
        bar.remove_class("hidden")
        status = self.query_one("#pull-status", Static)
        status.set_classes("")
        status.update("Starting...")
        self.query_one("#btn-cancel", Button).focus()

    def _fail(self, message: str) -> None:
        self._pull_worker = None
        field = self.query_one("#pull-name", Input)
        field.disabled = False
        self.query_one("#btn-pull", Button).disabled = False
        self.query_one("#pull-progress", ProgressBar).add_class("hidden")
        status = self.query_one("#pull-status", Static)
        status.set_classes("notice error")
        status.update(message)
        field.focus()

    def _on_progress(self, progress: PullProgress) -> None:
        if progress.status == "success":
            self._saw_success = True
        pct = -1
        if progress.total > 0:
            pct = min(100, int(progress.completed * 100 / progress.total))
        # Ollama emits many lines per second; only redraw when something visible changed.
        key = (progress.status, pct)
        if key == self._last_key:
            return
        self._last_key = key
        bar = self.query_one("#pull-progress", ProgressBar)
        if pct >= 0:
            bar.update(total=100, progress=pct)
        else:
            bar.update(total=None)
        self.query_one("#pull-status", Static).update(progress.status)

    @work
    async def _run_pull(self, name: str) -> None:
        try:
            async with contextlib.aclosing(self._ollama.pull(name)) as stream:
                async for progress in stream:
                    self._on_progress(progress)
        except OllamaError as exc:
            self._fail(str(exc))
            return
        # A stream that ends without a final "success" line means the download did not finish.
        if not self._saw_success:
            self._fail("Download ended before it finished. Try pulling again.")
            return
        self.notify(f"Pulled {name}")
        self.dismiss(name)
