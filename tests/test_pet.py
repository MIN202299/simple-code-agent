"""Tests for the pet and the legacy-directory migration."""

from __future__ import annotations

from nori import pet as petlib
from nori.i18n import set_language
from nori.branding import app_dir, migrate_legacy_dir
from nori.pet import Pet, load_pet


def test_art_is_always_three_lines_of_fixed_width():
    for mood in petlib.MOODS:
        for now in (0.0, 0.1, 0.4, 0.7, 1.3, 4.05):
            lines = petlib.art(mood, now)
            assert len(lines) == 3 and all(len(line) == petlib.ART_WIDTH for line in lines)
            assert petlib.face(mood, now) == f"={petlib.eyes(mood, now)}="


def test_moods_look_different():
    assert petlib.eyes("happy", 0) == "^.^" and petlib.eyes("error", 0) == "x.x"
    assert petlib.eyes("idle", 1.0) == "o.o" and petlib.eyes("idle", 4.05) == "-.-"  # blink
    assert {petlib.eyes("thinking", t) for t in (0.0, 0.7)} == {"o.O", "O.o"}
    assert petlib.bubble("waiting", 0) == "!" and petlib.bubble("idle", 0) == " "
    set_language("en")
    assert petlib.verb("working", "run_shell") in petlib.TOOL_VERBS["run_shell"]
    assert petlib.verb("working", "some_mcp_tool") in petlib.DEFAULT_TOOL_VERBS
    assert petlib.verb("thinking") in petlib.THINKING_VERBS
    set_language("zh")
    assert petlib.verb("working", "list_dir") == "巡视目录"


def test_levels_and_persistence(tmp_path):
    path = tmp_path / "dir" / "pet.json"
    pet = load_pet(path)
    assert (pet.level, pet.title, pet.tasks) == (1, "小奶猫", 0)

    assert pet.record_task(tool_calls=5) is False  # 15 xp
    assert pet.record_task(tool_calls=5) is True  # 30 xp -> level 2
    assert pet.level == 2 and pet.xp == 30 and pet.next_level_xp == 100

    pet.name = "小黑"
    pet.hidden = True
    pet.save()
    loaded = load_pet(path)
    assert (loaded.name, loaded.tasks, loaded.tools, loaded.hidden) == ("小黑", 2, 10, True)
    assert loaded.born == pet.born


def test_broken_state_file_gives_a_fresh_pet(tmp_path):
    path = tmp_path / "pet.json"
    for content in ("{not json", "[]", '{"name": "", "tasks": "many"}'):
        path.write_text(content)
        pet = load_pet(path)
        assert (pet.name, pet.tasks) == ("Nori", 0)
    Pet().save()  # a pet without a path is never written


def test_legacy_directory_is_migrated_once(tmp_path):
    assert migrate_legacy_dir(tmp_path) is None  # nothing to migrate
    old = tmp_path / ".code_agent"
    old.mkdir()
    (old / "config.json").write_text("{}")

    assert migrate_legacy_dir(tmp_path) == app_dir(tmp_path)
    assert (app_dir(tmp_path) / "config.json").exists() and not old.exists()

    old.mkdir()  # an existing new directory is never overwritten
    assert migrate_legacy_dir(tmp_path) is None and old.exists()
