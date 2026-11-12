"""Product identity. Change the name here and in pyproject.toml to rebrand."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Nori"
COMMAND = "nori"
TAGLINE = "终端里的 AI 编程搭档"
ACCENT = "#5fd7af"  # brand colour, used for the pet and highlights

APP_DIR_NAME = ".nori"
LEGACY_DIR_NAME = ".code_agent"  # the directory used before the rename


def short_path(path: Path) -> str:
    """A path for display, with the home directory abbreviated to `~`."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text


def app_dir(home: Path | None = None) -> Path:
    """Per-user directory holding config, history, skills and the pet's state."""
    return (home or Path.home()) / APP_DIR_NAME


def migrate_legacy_dir(home: Path | None = None) -> Path | None:
    """Move the pre-rename directory into place. Returns the new path if it moved."""
    home = home or Path.home()
    old, new = home / LEGACY_DIR_NAME, home / APP_DIR_NAME
    if new.exists() or not old.is_dir():
        return None
    try:
        old.rename(new)
    except OSError:
        return None
    return new
