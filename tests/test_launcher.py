"""What appears in the launcher, and how each entry is started.

The window itself is not tested - `scripts/check_windows.py` opens it - but
everything that *decides* anything is here, which is the rule the three settings
windows already follow.

The one that matters most is discovery. A launcher holding a list of scripts is a
second copy of what the project consists of, and it fails silently in the worst
direction: a button that never appears looks exactly like a feature nobody wrote.
So a script declares itself with a marker, and the tests below pin both halves of
that - a marked file is offered, an unmarked one is invisible without anybody
naming it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comfyui_mcp import i18n
from comfyui_mcp.launcher import (
    Entry,
    as_text,
    console_command,
    discover,
    parse_marker,
)


def test_a_batch_marker_is_read():
    order, label = parse_marker('@echo off\nREM LAUNCHER 30: "Install" "Установка"\n')
    assert order == 30
    assert label.en == "Install"
    assert label.ru == "Установка"


def test_a_shell_marker_is_read():
    order, label = parse_marker('#!/usr/bin/env bash\n# LAUNCHER 10: "Run" "Запуск"\n')
    assert order == 10
    assert label.of(i18n.RU) == "Запуск"


def test_a_file_without_a_marker_is_not_offered():
    assert parse_marker("@echo off\nREM just a script\n") is None


def test_a_half_translated_marker_is_not_offered():
    """i18n.Text refuses to exist half-translated; here that must not raise."""
    assert parse_marker('REM LAUNCHER 10: "Install" ""\n') is None
    assert parse_marker('REM LAUNCHER 10: "" "Установка"\n') is None


def test_a_marker_without_a_number_goes_last():
    order, _ = parse_marker('REM LAUNCHER: "Later" "Позже"')
    assert order > 1000


def test_the_marker_is_found_anywhere_in_the_head_but_must_own_its_line():
    assert parse_marker('  # LAUNCHER 5: "A" "Б"  ') is not None
    assert parse_marker('REM see LAUNCHER 5: "A" "Б" for how this works') is None


def write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(body, encoding="utf-8")
    return path


def test_only_marked_scripts_are_offered_and_in_order(tmp_path):
    write(tmp_path, "zeta.bat", 'REM LAUNCHER 10: "First" "Первый"')
    write(tmp_path, "alpha.bat", 'REM LAUNCHER 20: "Second" "Второй"')
    write(tmp_path, "logo.bat", "@echo off\necho hello")          
    write(tmp_path, "notes.txt", 'REM LAUNCHER 5: "No" "Нет"')    

    found = discover(tmp_path, name="nt")
    assert [e.name for e in found] == ["zeta.bat", "alpha.bat"]
    assert [e.label.en for e in found] == ["First", "Second"]


def test_only_this_platforms_half_is_offered(tmp_path):
    write(tmp_path, "install.bat", 'REM LAUNCHER 10: "Install" "Установка"')
    write(tmp_path, "install.sh", '# LAUNCHER 10: "Install" "Установка"')

    assert [e.name for e in discover(tmp_path, name="nt")] == ["install.bat"]
    assert [e.name for e in discover(tmp_path, name="posix")] == ["install.sh"]


def test_a_script_that_is_not_there_is_simply_not_offered(tmp_path):
    """A release drops minify_bridge and release; nothing has to know that."""
    write(tmp_path, "install.bat", 'REM LAUNCHER 10: "Install" "Установка"')
    assert [e.name for e in discover(tmp_path, name="nt")] == ["install.bat"]


def test_an_empty_folder_is_not_an_error(tmp_path):
    assert discover(tmp_path, name="nt") == []


def test_the_real_project_offers_install_first():
    found = discover()
    assert found, "the project root should offer something"
    assert found[0].name.startswith("install.")
    names = [e.name for e in found]
    assert not any(n.startswith(("lang.", "logo.", "launcher.")) for n in names), names


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_no_script_is_offered_twice(platform):
    """A wrapper and the script it wraps must not both carry a marker.

    install_node.ps1 has a .bat over it - a .ps1 has no file association on
    Windows and cannot be double-clicked - and the marker belongs to exactly one
    of the pair. Two entries doing the same thing is the failure mode that
    declaring-in-place invites, and the only one it does not prevent by itself.
    """
    found = discover(name=platform)
    labels = [e.label.en for e in found]
    assert len(labels) == len(set(labels)), labels
    stems = [e.path.stem.lower() for e in found]
    assert len(stems) == len(set(stems)), stems


def test_both_platforms_offer_the_same_things():
    """The halves are twins; a marker added to one and not the other is a slip."""
    assert [e.label.en for e in discover(name="nt")] == [
        e.label.en for e in discover(name="posix")
    ]


def test_a_batch_file_is_started_in_its_own_console():
    made = console_command(Path(r"C:\p\install.bat"), name="nt")
    assert made[:4] == ["cmd", "/c", "start", ""]
    assert made[-1] == r"C:\p\install.bat"


def test_a_powershell_script_is_not_left_to_its_default_handler():
    """Opening a .ps1 by association lands in Notepad rather than running it."""
    made = console_command(Path(r"C:\p\install_node.ps1"), name="nt")
    assert "powershell" in made
    assert "-File" in made
    assert made[-1] == r"C:\p\install_node.ps1"


def test_the_first_terminal_present_wins():
    script = Path("/p/install.sh")
    made = console_command(
        script, name="posix",
        which=lambda n: "/usr/bin/konsole" if n == "konsole" else None,
    )
    assert made[0] == "konsole"
    assert made[-2:] == ["bash", str(script)]


def test_with_no_terminal_it_still_runs():
    script = Path("/p/install.sh")
    made = console_command(script, name="posix", which=lambda _n: None)
    assert made == ["bash", str(script)]


def test_the_text_listing_names_both_the_file_and_the_label():
    entries = [Entry(Path("/p/install.bat"), 10, i18n.Text(en="Install", ru="Установка"))]
    assert "install.bat" in as_text(entries, i18n.EN)
    assert "Install" in as_text(entries, i18n.EN)
    assert "Установка" in as_text(entries, i18n.RU)


@pytest.mark.parametrize("lang", [i18n.EN, i18n.RU])
def test_every_real_marker_speaks_both_languages(lang):
    """Building the listing constructs each Text, and a half-translated one raises."""
    assert as_text(discover(), lang)
