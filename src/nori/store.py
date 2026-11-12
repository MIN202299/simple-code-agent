"""Saved conversations, so a session can be resumed later.

Each session is one JSON file in `~/.nori/sessions/`. Files hold the full
conversation, including file contents and command output the agent saw.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .branding import app_dir

MAX_LISTED_FILES = 200
TITLE_CHARS = 80


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def new_session_id() -> str:
    return f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


@dataclass
class SessionRecord:
    id: str
    workspace: str
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)
    title: str = ""
    model: str = ""
    history: list[Any] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    context_tokens: int = 0

    @property
    def turns(self) -> int:
        """Number of user messages, as a rough size of the conversation."""
        return sum(
            1 for item in self.history if isinstance(item, dict) and item.get("role") == "user"
        )


def sessions_dir(home: Path | None = None) -> Path:
    return app_dir(home) / "sessions"


class SessionStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or sessions_dir()

    def _path(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.json"

    def save(self, record: SessionRecord) -> None:
        """Write the session atomically; failures are ignored (never break a turn)."""
        record.updated = _now()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self._path(record.id).with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(asdict(record), file, ensure_ascii=False, default=str)
            os.replace(temporary, self._path(record.id))
        except OSError:
            pass

    def _read(self, path: Path) -> SessionRecord | None:
        try:
            data = json.loads(path.read_text("utf-8"))
            if not isinstance(data.get("history"), list):
                return None
            known = {key: data[key] for key in SessionRecord.__dataclass_fields__ if key in data}
            return SessionRecord(**known)
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def load(self, session_id: str) -> SessionRecord | None:
        """Load by id; a unique prefix of an id also works."""
        if not session_id or any(ch in session_id for ch in "/\\."):
            return None
        exact = self._path(session_id)
        if exact.is_file():
            return self._read(exact)
        matches = sorted(self.directory.glob(f"{session_id}*.json"))
        return self._read(matches[0]) if len(matches) == 1 else None

    def list(self, workspace: Path, limit: int = 20) -> list[SessionRecord]:
        """Sessions for a workspace, most recently updated first."""
        try:
            files = sorted(
                self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
        except OSError:
            return []
        records = []
        for path in files[:MAX_LISTED_FILES]:
            record = self._read(path)
            if record is not None and record.workspace == str(workspace) and record.history:
                records.append(record)
                if len(records) >= limit:
                    break
        return records

    def latest(self, workspace: Path) -> SessionRecord | None:
        records = self.list(workspace, limit=1)
        return records[0] if records else None
