"""Color themes. styles.tcss reads every color from the $an-* variables defined here."""

from __future__ import annotations

from textual.app import App
from textual.theme import Theme

from anito.config import THEMES as THEME_KEYS

# Everything styles.tcss refers to. A palette missing one would make the stylesheet fail to load.
REQUIRED_COLORS = (
    "an-bg",
    "an-bg-dark",
    "an-surface",
    "an-surface-hi",
    "an-border",
    "an-fg",
    "an-fg-dim",
    "an-muted",
    "an-sel",
    "an-primary",
    "an-on-primary",
    "an-user",
    "an-assistant",
    "an-accent",
    "an-link",
    "an-ok",
    "an-warn",
    "an-error",
    "an-think",
    "an-ok-bg",
    "an-warn-bg",
    "an-error-bg",
)

THEME_LABELS = {
    "monochrome": "Monochrome",
    "kanagawa": "Kanagawa",
}

# Greys only. Status colors share white, so state is carried by text, bold and bars as well.
_MONOCHROME = {
    "an-bg": "#0B0B0B",
    "an-bg-dark": "#000000",
    "an-surface": "#1E1E1E",
    "an-surface-hi": "#3F3F3F",
    "an-border": "#5A5A5A",
    "an-fg": "#FFFFFF",
    "an-fg-dim": "#BDBDBD",
    "an-muted": "#8A8A8A",
    "an-sel": "#505050",
    "an-primary": "#FFFFFF",
    "an-on-primary": "#000000",
    "an-user": "#FFFFFF",
    "an-assistant": "#B0B0B0",
    "an-accent": "#FFFFFF",
    "an-link": "#FFFFFF",
    "an-ok": "#FFFFFF",
    "an-warn": "#FFFFFF",
    "an-error": "#FFFFFF",
    "an-think": "#8A8A8A",
    "an-ok-bg": "#1E1E1E",
    "an-warn-bg": "#2B2B2B",
    "an-error-bg": "#4A4A4A",
}

# Kanagawa Wave.
_KANAGAWA = {
    "an-bg": "#1F1F28",
    "an-bg-dark": "#16161D",
    "an-surface": "#2A2A37",
    "an-surface-hi": "#363646",
    "an-border": "#54546D",
    "an-fg": "#DCD7BA",
    "an-fg-dim": "#C8C093",
    "an-muted": "#727169",
    "an-sel": "#2D4F67",
    "an-primary": "#2D4F67",
    "an-on-primary": "#DCD7BA",
    "an-user": "#7FB4CA",
    "an-assistant": "#98BB6C",
    "an-accent": "#E6C384",
    "an-link": "#7E9CD8",
    "an-ok": "#98BB6C",
    "an-warn": "#E6C384",
    "an-error": "#E46876",
    "an-think": "#957FB8",
    "an-ok-bg": "#2B3328",
    "an-warn-bg": "#49443C",
    "an-error-bg": "#43242B",
}

_PALETTES = {
    "monochrome": _MONOCHROME,
    "kanagawa": _KANAGAWA,
}


def textual_name(key: str) -> str:
    """The name Textual knows a theme by, for a config key like 'kanagawa'."""
    return f"anito-{key}"


def _build(key: str) -> Theme:
    colors = _PALETTES[key]
    missing = [name for name in REQUIRED_COLORS if name not in colors]
    if missing:
        raise ValueError(f"Theme {key!r} is missing colors: {', '.join(missing)}")
    return Theme(
        name=textual_name(key),
        primary=colors["an-primary"],
        secondary=colors["an-fg-dim"],
        accent=colors["an-accent"],
        warning=colors["an-warn"],
        error=colors["an-error"],
        success=colors["an-ok"],
        foreground=colors["an-fg"],
        background=colors["an-bg"],
        surface=colors["an-surface"],
        panel=colors["an-surface-hi"],
        dark=True,
        variables=dict(colors),
    )


def register_themes(app: App) -> None:
    """Register every theme. Call this in the App's __init__, before the stylesheet loads."""
    for key in THEME_KEYS:
        app.register_theme(_build(key))
