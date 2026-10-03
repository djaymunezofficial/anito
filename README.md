# ANITO ᜀᜈᜒᜆᜓ

A terminal control deck for local [Ollama](https://ollama.com) models. Chat, manage models and tune generation settings from one keyboard-driven interface. ANITO only talks to the Ollama server you point it at, so your conversations stay on your machine.

## Features

- **Chat**: streaming replies rendered as Markdown, a model sidebar, and Esc to stop a reply at any time.
- **Models**: installed models with size, family, parameters and quantization. Pull with live progress, unload from memory, and delete with a confirmation step.
- **Settings**: temperature, context length, system prompt and Ollama URL, validated before anything is saved.
- **Command palette**: `Ctrl+P` reaches every action.
- **Status bar**: connection state, active model and context length are always visible.
- **Kanagawa Wave** color scheme.

## Requirements

- Python 3.10 or newer
- [Ollama](https://ollama.com) installed and running (default `http://localhost:11434`)
- A terminal with true-color support

## Install

```bash
git clone https://github.com/<you>/anito.git
cd anito

pipx install .      # isolated install, recommended
# or, for development:
pip install -e .
```

Then run:

```bash
anito
```

After pulling new changes, reinstall with `pipx install --force .` (editable installs pick them up on their own).

## Keys

| Where | Key | Action |
|---|---|---|
| Everywhere | `Ctrl+P` | Command palette |
| Everywhere | `F1` `F2` `F3` | Chat, Models, Settings |
| Everywhere | `Ctrl+Q` | Quit (asks first if a reply is streaming) |
| Chat | `Enter` | Send |
| Chat | `Esc` | Stop the reply |
| Chat | `Ctrl+N` | New chat |
| Models | `R` / `P` / `U` / `D` | Refresh / Pull / Unload / Delete |
| Settings | `Ctrl+S` | Save |

Every button also works with the mouse.

## Configuration

Settings are saved to `config.json` in:

- Linux and macOS: `$XDG_CONFIG_HOME/anito/` if set, otherwise `~/.config/anito/`
- Windows: `%APPDATA%\anito\`

| Key | Default | Meaning |
|---|---|---|
| `ollama_url` | `http://localhost:11434` | Where Ollama is running |
| `temperature` | `0.7` | 0.0 to 2.0 |
| `num_ctx` | `4096` | Context length in tokens, 256 to 131072 |
| `system_prompt` | empty | Sent before every chat |
| `last_model` | empty | Remembered between runs |

You can edit it in the Settings tab or by hand. Invalid values fall back to their defaults.

## Project layout

```text
anito/
├── pyproject.toml
├── README.md
└── src/
    └── anito/
        ├── __init__.py        # __version__
        ├── app.py             # App, status bar, command palette
        ├── config.py          # config file and validators
        ├── styles.tcss        # stylesheet
        ├── tabs/
        │   ├── chat.py        # chat log and streaming
        │   ├── models.py      # model table and toolbar
        │   └── settings.py    # settings form
        ├── dialogs/
        │   └── modals.py      # confirm and pull dialogs
        ├── utils/
        │   ├── system.py      # CPU/RAM monitor and SystemStatus widget
        │   └── ollama_api.py  # async Ollama client
        └── skills/
            ├── base.py        # Skill, SkillRegistry
            └── vscode.py      # /code <filename>
```

## Building blocks

**System stats.** `anito.utils.system.SystemStatus` is a one-line CPU/RAM label that refreshes itself in a background worker. Add it to any layout:

```python
from anito.utils.system import SystemStatus

yield SystemStatus(id="status-sys", classes="status-item")
```

**Skills.** A skill is one capability that works as a slash command (`/code main.py`) and can be offered to a model as a tool. `anito.skills.base` provides the `Skill` base class and a `SkillRegistry` that parses typed commands and runs tool calls. `anito.skills.vscode.VSCodeSkill` opens a file or folder with `code <filename>`.

```python
from anito.skills.base import SkillRegistry
from anito.skills.vscode import VSCodeSkill

registry = SkillRegistry()
registry.register(VSCodeSkill())

result = await registry.run_command("/code main.py")  # None if the text isn't a command
```

To add a skill, subclass `Skill` in `skills/` and register it the same way.

## Publishing to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<you>/anito.git
git push -u origin main
```

Add a `.gitignore` first so build files stay out of the repo:

```text
__pycache__/
*.egg-info/
.venv/
build/
dist/
```

## Baybayin branding guide

**What the name means.** *Anito* refers to ancestral spirits, and the carved figures that represent them, in pre-colonial Philippine belief. ANITO's name is written in Baybayin as **ᜀᜈᜒᜆᜓ**.

**How to read it.** Baybayin is an abugida: each character is a consonant with an inherent *a*, and a mark above or below changes the vowel.

| Glyph | Sound | Parts |
|---|---|---|
| ᜀ | a | vowel A |
| ᜈᜒ | ni | ᜈ (na) + ᜒ (i/e mark) |
| ᜆᜓ | to / tu | ᜆ (ta) + ᜓ (o/u mark) |

The marks are shared, so ᜒ covers both *i* and *e*, and ᜓ covers both *o* and *u*. That is why ᜀᜈᜒᜆᜓ reads as "Anito".

**Where to use it**

- At the top of the README and on the repository's social preview image.
- Next to the Latin wordmark in the About text, release notes and other branding.

**Rules**

1. Always pair it with the Latin wordmark **ANITO**. The Baybayin is part of the identity, not a replacement for readable text.
2. Keep the spelling exactly **ᜀᜈᜒᜆᜓ**. Don't respell it, mix in other scripts, or alter the glyph shapes.
3. Keep functional UI text (buttons, labels, errors) in plain Latin text.
4. Use it with respect for what *anito* means. Avoid joke or novelty uses.
5. Have a Baybayin reader review any new use before it ships.

**Rendering.** Baybayin is in the Unicode Tagalog block (U+1700 to U+171F). The terminal font, or its fallback fonts, must include those glyphs. [Noto Sans Tagalog](https://fonts.google.com/noto/specimen/Noto+Sans+Tagalog) does. If you see empty boxes, the font is missing them. Use the plain **ANITO** wordmark there; the app itself doesn't depend on Baybayin to work.
