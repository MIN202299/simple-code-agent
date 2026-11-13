"""File checkpoints: remember what files looked like before the agent changed them.

Before `write_file` or `edit_file` touches a file, its previous content is
recorded against the current turn. `/undo` restores the files of the most
recent turn that changed any. Only changes made through those two tools are
tracked: what a shell command did to the disk can not be undone here.
Checkpoints live in memory and are gone when the session ends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MAX_TURNS_KEPT = 50


@dataclass
class TurnCheckpoint:
    label: str  # the user's request, for display
    files: dict[Path, bytes | None] = field(default_factory=dict)  # None: did not exist


class Checkpoints:
    def __init__(self) -> None:
        self.turns: list[TurnCheckpoint] = []

    def begin_turn(self, label: str) -> None:
        """Start recording a new turn; turns without file changes are dropped later."""
        self.turns = [turn for turn in self.turns if turn.files][-MAX_TURNS_KEPT:]
        self.turns.append(TurnCheckpoint(" ".join(label.split())[:80]))

    def record(self, path: Path) -> None:
        """Remember `path` as it is now, unless this turn already did."""
        if not self.turns:
            self.begin_turn("")
        files = self.turns[-1].files
        if path in files:
            return
        try:
            files[path] = path.read_bytes() if path.is_file() else None
        except OSError:
            files[path] = None

    def last_changed(self) -> TurnCheckpoint | None:
        return next((turn for turn in reversed(self.turns) if turn.files), None)

    def undo(self) -> tuple[TurnCheckpoint, list[Path], list[Path]] | None:
        """Revert the most recent turn that changed files.

        Returns the turn, the files restored and the files deleted (they were
        created by that turn), or None if there is nothing to undo.
        """
        turn = self.last_changed()
        if turn is None:
            return None
        restored, deleted = [], []
        for path, content in turn.files.items():
            try:
                if content is None:
                    path.unlink(missing_ok=True)
                    deleted.append(path)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                    restored.append(path)
            except OSError:
                continue
        self.turns.remove(turn)
        return turn, restored, deleted
