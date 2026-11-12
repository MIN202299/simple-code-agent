"""Nori the cat: the mascot that lives next to the input box.

The pet reacts to what the agent is doing and slowly levels up as tasks get
done. Its state (name, stats) is stored in `~/.nori/pet.json`.
"""

from __future__ import annotations

import datetime
import json
import math
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .branding import APP_NAME, app_dir

ART_WIDTH = 9
MOODS = ("idle", "thinking", "working", "waiting", "happy", "error", "sleeping")

# Level thresholds are on XP = 10 per finished task + 1 per tool call.
TITLES = ("小奶猫", "见习喵", "码农喵", "资深喵", "架构喵", "传说喵")

THINKING_VERBS = ("琢磨中", "舔爪子思考", "盯着代码发呆", "竖起耳朵", "转圈圈", "酝酿中", "挠头中")
TOOL_VERBS = {
    "run_shell": ("敲命令", "盯着终端", "等命令跑完"),
    "read_file": ("翻文件", "读代码"),
    "write_file": ("写文件", "敲键盘"),
    "edit_file": ("改代码", "动爪子修改"),
    "grep": ("嗅探代码", "翻箱倒柜"),
    "glob_files": ("找文件", "四处张望"),
    "list_dir": ("巡视目录",),
    "load_skill": ("学习新技能",),
    "update_plan": ("整理计划",),
    "spawn_agent": ("派出小伙伴", "召唤分身"),
}
DEFAULT_TOOL_VERBS = ("使用工具", "忙活中")
WRITING_VERBS = ("回复中", "组织语言", "喵喵说明")


def _frame(period: float, now: float, count: int = 2) -> int:
    return int(now / period) % count


def eyes(mood: str, now: float) -> str:
    """The three-character face for a mood at time `now` (seconds)."""
    if mood == "thinking":
        return ("o.O", "O.o")[_frame(0.6, now)]
    if mood == "working":
        return (">.<", "o.o")[_frame(0.35, now)]
    if mood == "happy":
        return "^.^"
    if mood == "error":
        return "x.x"
    if mood == "sleeping":
        return "-.-"
    if mood == "waiting":
        return "o.o"
    return "-.-" if now % 4.0 < 0.18 else "o.o"  # idle: blink now and then


def bubble(mood: str, now: float) -> str:
    """A small symbol floating next to the pet's ear."""
    if mood == "thinking":
        return ("?", " ")[_frame(0.6, now)]
    if mood == "waiting":
        return "!"
    if mood == "happy":
        return "♪"
    if mood == "sleeping":
        return ("z", "Z")[_frame(0.9, now)]
    return " "


def art(mood: str = "idle", now: float = 0.0) -> list[str]:
    """Three lines of ASCII art, each exactly ART_WIDTH wide."""
    paws = " > ^ < "
    if mood == "working":
        paws = (" >_^_< ", " > ^ < ")[_frame(0.35, now)]
    elif mood == "happy":
        paws = (" > ^ <~", " > ^ < ")[_frame(0.5, now)]
    lines = [f" /\\_/\\ {bubble(mood, now)}", f"( {eyes(mood, now)} )", paws]
    return [line.ljust(ART_WIDTH)[:ART_WIDTH] for line in lines]


def face(mood: str = "idle", now: float = 0.0) -> str:
    """Compact one-line face, e.g. `=^.^=`."""
    return f"={eyes(mood, now)}="


def verb(mood: str, tool: str | None = None) -> str:
    """A short phrase for the status line describing what the pet is up to."""
    if mood == "working" and tool:
        return random.choice(TOOL_VERBS.get(tool, DEFAULT_TOOL_VERBS))
    if mood == "writing":
        return random.choice(WRITING_VERBS)
    return random.choice(THINKING_VERBS)


@dataclass
class Pet:
    name: str = APP_NAME
    born: str = field(default_factory=lambda: datetime.date.today().isoformat())
    tasks: int = 0
    tools: int = 0
    hidden: bool = False
    path: Path | None = field(default=None, repr=False, compare=False)

    @property
    def xp(self) -> int:
        return self.tasks * 10 + self.tools

    @property
    def level(self) -> int:
        return 1 + int(math.sqrt(self.xp / 25))

    @property
    def title(self) -> str:
        return TITLES[min((self.level - 1) // 2, len(TITLES) - 1)]

    @property
    def next_level_xp(self) -> int:
        return 25 * self.level**2

    def record_task(self, tool_calls: int) -> bool:
        """Count a finished task. Returns True if the pet levelled up."""
        before = self.level
        self.tasks += 1
        self.tools += tool_calls
        self.save()
        return self.level > before

    def save(self) -> None:
        if self.path is None:
            return
        data = {k: v for k, v in asdict(self).items() if k != "path"}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
        except OSError:
            pass  # the pet is decoration; never fail a session over it


def pet_path(home: Path | None = None) -> Path:
    return app_dir(home) / "pet.json"


def load_pet(path: Path | None = None) -> Pet:
    """Load the pet, or adopt a new one if there is no (valid) state file."""
    path = path or pet_path()
    pet = Pet(path=path)
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return pet
    if not isinstance(data, dict):
        return pet
    for key, kind in (("name", str), ("born", str), ("tasks", int), ("tools", int), ("hidden", bool)):
        value = data.get(key)
        if isinstance(value, kind) and (kind is not str or value.strip()):
            setattr(pet, key, value)
    return pet
