"""Hardware stats for the status bar, sampled off the UI thread."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import psutil
from textual import work
from textual.widgets import Static

GIB = 1024**3
DEFAULT_INTERVAL = 2.0


@dataclass(frozen=True)
class SystemStats:
    cpu_percent: float
    ram_used: int  # bytes
    ram_total: int  # bytes

    @property
    def ram_percent(self) -> float:
        return self.ram_used * 100 / self.ram_total if self.ram_total else 0.0


def read_stats() -> SystemStats | None:
    """Sample CPU and RAM. Returns None if the platform won't report them.

    cpu_percent(interval=None) returns at once and measures the time since the
    previous call, so call it from one place at a steady pace.
    """
    try:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
    except (psutil.Error, OSError):
        return None
    # total - available is the memory programs are actually holding; "used" varies by OS.
    return SystemStats(cpu, mem.total - mem.available, mem.total)


def format_stats(stats: SystemStats | None) -> str:
    if stats is None:
        return "CPU --  RAM --"
    return (
        f"CPU {stats.cpu_percent:.0f}%  "
        f"RAM {stats.ram_used / GIB:.1f}/{stats.ram_total / GIB:.1f} GB"
    )


class SystemStatus(Static):
    """One-line CPU/RAM readout that refreshes itself in the background.

    Drop it into any layout, e.g. the status bar:
        yield SystemStatus(id="status-sys", classes="status-item")
    """

    def __init__(
        self,
        *,
        interval: float = DEFAULT_INTERVAL,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        # markup=False: the text is ours, but there is no reason to parse it.
        super().__init__(format_stats(None), id=id, classes=classes, markup=False)
        self._interval = interval

    def on_mount(self) -> None:
        self._poll()

    @work(exclusive=True, group="system-stats")
    async def _poll(self) -> None:
        # The first cpu_percent reading is meaningless, so prime it and take the
        # first real sample shortly after. The worker ends when the widget is removed.
        await asyncio.to_thread(psutil.cpu_percent, None)
        delay = 0.5
        while True:
            await asyncio.sleep(delay)
            self.update(format_stats(await asyncio.to_thread(read_stats)))
            delay = self._interval
