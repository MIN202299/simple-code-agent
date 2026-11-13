"""Collects every string the code passes to `t()`, for the catalog completeness test."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src" / "nori"


def _constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "text"` assignments (including parenthesised concatenations)."""
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    value = ast.literal_eval(node.value)
                except ValueError:
                    continue
                if isinstance(value, str):
                    found[target.id] = value
    return found


def translation_keys() -> set[str]:
    """All English texts that can reach `t()`."""
    from nori import pet
    from nori.branding import TAGLINE
    from nori.commands import BUILTIN_COMMANDS, MODE_DESCRIPTIONS
    from nori.permissions import BACKGROUND, OTHER_TOOL
    from nori.risk import REASONS
    from nori.tui import MODE_LABELS, SHORTCUTS

    keys: set[str] = set()
    all_constants: dict[str, str] = {}
    trees = {path: ast.parse(path.read_text("utf-8")) for path in SOURCE.glob("*.py")}
    for tree in trees.values():
        all_constants.update(_constants(tree))
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "t" or not node.args:
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Name) and argument.id in all_constants:
                keys.add(all_constants[argument.id])
                continue
            try:
                value = ast.literal_eval(argument)
            except ValueError:
                continue  # a variable: covered by the data tables below
            if isinstance(value, str):
                keys.add(value)
    # Texts kept in data tables and translated where they are shown.
    keys.add(TAGLINE)
    keys.update(command.description for command in BUILTIN_COMMANDS)
    keys.update(description for _key, description in SHORTCUTS)
    keys.update(pet.TITLES)
    keys.update(pet.THINKING_VERBS + pet.DEFAULT_TOOL_VERBS + pet.WRITING_VERBS)
    for verbs in pet.TOOL_VERBS.values():
        keys.update(verbs)
    keys.update(("Endpoint", "API key", "Config"))  # labels of Session.info
    keys.update(MODE_DESCRIPTIONS.values())
    keys.update(REASONS)
    keys.update((BACKGROUND, OTHER_TOOL))
    keys.update(MODE_LABELS.values())
    return keys


if __name__ == "__main__":
    for key in sorted(translation_keys()):
        print(repr(key))
