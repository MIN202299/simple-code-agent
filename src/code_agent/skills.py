"""Skills: reusable instruction packs stored as `<dir>/<name>/SKILL.md`.

A skill is a directory containing a `SKILL.md` file with YAML-style front matter
(`name`, `description`) followed by instructions, plus any supporting files.
Only names and descriptions go into the system prompt; the agent loads the full
instructions on demand with the `load_skill` tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SKILL_FILE = "SKILL.md"
MAX_DESCRIPTION_CHARS = 500
_NAME_RE = re.compile(r"[^a-z0-9_-]+")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    directory: Path
    body: str

    def render(self) -> str:
        """The text handed to the model when the skill is loaded."""
        return (
            f"# Skill: {self.name}\n"
            f"Skill directory: {self.directory}\n"
            "Files referenced by the skill are relative to that directory; read them "
            "with read_file using the absolute path.\n\n"
            f"{self.body.strip()}\n"
        )


def skill_dirs(workspace: Path, home: Path | None = None) -> list[Path]:
    """Directories searched for skills, highest priority first."""
    home = home or Path.home()
    return [
        workspace / ".agents" / "skills",
        workspace / ".claude" / "skills",
        home / ".code_agent" / "skills",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
    ]


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split `---` front matter from the body.

    Handles the subset of YAML that skill files use: `key: value` pairs, quoted
    values, and multi-line values (block scalars or indented continuation lines).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return {}, text
    meta: dict[str, str] = {}
    key: str | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            if key is not None:
                meta[key] = f"{meta[key]} {line.strip()}".strip()
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        key = name.strip()
        value = value.strip()
        if value in (">", "|", ">-", "|-", ">+", "|+"):
            value = ""
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key] = value
    return meta, "\n".join(lines[end + 1 :])


def load_skill_file(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = parse_front_matter(text)
    name = _NAME_RE.sub("-", (meta.get("name") or path.parent.name).strip().lower()).strip("-")
    if not name:
        return None
    description = meta.get("description", "").strip()
    if not description:
        # Fall back to the first non-heading line of the body.
        description = next(
            (ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")),
            "",
        )
    return Skill(name, description[:MAX_DESCRIPTION_CHARS], path.parent.resolve(), body)


def discover_skills(workspace: Path, home: Path | None = None) -> dict[str, Skill]:
    """Find all skills. Earlier directories win when names collide."""
    skills: dict[str, Skill] = {}
    for root in skill_dirs(workspace, home):
        if not root.is_dir():
            continue
        for skill_file in sorted(root.glob(f"*/{SKILL_FILE}")):
            skill = load_skill_file(skill_file)
            if skill is not None:
                skills.setdefault(skill.name, skill)
    return skills


def skills_prompt(skills: dict[str, Skill]) -> str:
    """System-prompt section listing the available skills."""
    if not skills:
        return ""
    listing = "\n".join(f"- {s.name}: {s.description}" for s in skills.values())
    return (
        "\n# Skills\n"
        "Skills are instruction packs for specific kinds of tasks. When a task matches "
        "a skill below, call load_skill with its name BEFORE starting the task, then "
        "follow the instructions it returns.\n"
        f"{listing}\n"
    )
