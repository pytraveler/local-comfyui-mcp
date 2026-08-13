"""Tests for the two languages the human-facing half speaks.

The module is pure, so all of it is covered here — including the part that was a
real bug: `--print` raised `UnicodeEncodeError` on a cp866 console, which is the
default console codepage on a Russian Windows, because the prose in this project
is written with em dashes and cp866 has none.
"""

from __future__ import annotations

import os

import pytest

from comfyui_mcp import configure, configure_clients as CC, configure_comfy as CF, i18n
from comfyui_mcp import toolsets as T


def test_a_text_carries_both_languages_and_hands_over_the_one_asked_for():
    text = i18n.Text(en="Save", ru="Сохранить")
    assert text.of("en") == "Save"
    assert text.of("ru") == "Сохранить"


def test_an_unknown_language_gets_the_english():
    # `of` is the last step before something is drawn, so it must not raise.
    assert i18n.Text(en="Save", ru="Сохранить").of("de") == "Save"


def test_half_a_translation_refuses_to_exist():
    # The one mistake this shape still permits, caught where it is made rather
    # than by a test that walks a table — most of these are built inside a
    # window's layout code, where nothing could enumerate them.
    with pytest.raises(ValueError, match="half-translated"):
        i18n.Text(en="Save", ru="")
    with pytest.raises(ValueError, match="half-translated"):
        i18n.Text(en="   ", ru="Сохранить")


def test_nothing_to_say_is_allowed_in_both():
    # Group.warning and Field.hint are empty for some entries; that is not a
    # missing translation, it is a missing sentence.
    empty = i18n.Text(en="", ru="")
    assert not empty
    assert empty.of("ru") == ""


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("ru", "ru"),
        ("RU", "ru"),
        ("ru-RU", "ru"),
        ("ru_RU.UTF-8", "ru"),
        # What Windows itself writes, in English whatever the locale.
        ("Russian_Russia.1251", "ru"),
        ("en", "en"),
        ("en-GB", "en"),
        ("English_United States.1252", "en"),
    ],
)
def test_anything_locale_shaped_reduces_to_a_language(spec: str, expected: str):
    assert i18n.normalise(spec) == expected


@pytest.mark.parametrize("spec", ["", "   ", "C", "POSIX", "de", "zh-CN", None])
def test_a_locale_we_do_not_speak_is_not_guessed_at(spec):
    assert i18n.normalise(spec) is None


def test_an_explicit_setting_wins_over_the_machine(monkeypatch):
    monkeypatch.setitem(os.environ, "LANGUAGE", "ru")
    assert i18n.resolve("en") == "en"


def test_an_empty_setting_asks_the_machine(monkeypatch):
    monkeypatch.setitem(os.environ, "LANGUAGE", "ru_RU.UTF-8")
    assert i18n.resolve("") == "ru"
    assert i18n.resolve(None) == "ru"


def test_a_setting_naming_a_language_we_do_not_speak_falls_back_to_english(monkeypatch):
    # Not to the machine's own: somebody who wrote COMFYUI_LANG=de is saying they
    # do not want the local default, and honouring the half we understood is the
    # more faithful reading of that.
    monkeypatch.setitem(os.environ, "LANGUAGE", "ru")
    assert i18n.resolve("de") == "en"


def test_the_first_entry_of_a_preference_list_wins(monkeypatch):
    # LANGUAGE is colon-separated on POSIX.
    monkeypatch.setitem(os.environ, "LANGUAGE", "ru:en")
    assert i18n.detect() == "ru"


def test_detect_never_raises_however_hostile_the_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", {"LANG": "\x00nonsense"})
    assert i18n.detect() in i18n.LANGS


def test_a_command_line_flag_overrides_the_env_file():
    assert i18n.from_args(["--print", "--lang=ru"]) == "ru"
    assert i18n.from_args(["--lang=en"]) == "en"


def test_an_em_dash_survives_a_console_that_has_one():
    line = "logs — the only place a failed node says anything"
    assert i18n.printable(line, "utf-8") == line
    assert i18n.printable(line, "cp1251") == line


def test_an_em_dash_becomes_a_dash_on_a_console_that_has_none():
    # cp866 is the default console codepage on a Russian Windows, and it has no
    # em dash. This raised UnicodeEncodeError and printed nothing at all.
    assert i18n.printable("logs — one place", "cp866") == "logs - one place"


def test_folding_touches_punctuation_and_never_letters():
    # Transliterating would make the text unreadable in the name of printing it.
    assert i18n.fold_punctuation("Логи — «одно» место…") == 'Логи - "одно" место...'


def test_json_is_escaped_rather_than_folded():
    # Folding is wrong for a JSON payload: a guillemet becoming a quote inside a
    # string would break the document. `--list-tools` asks this question instead.
    assert i18n.can_encode("logs — one place", "utf-8")
    assert not i18n.can_encode("logs — one place", "cp866")
    assert i18n.can_encode("logs - one place", "cp866")


def test_an_unknown_encoding_is_not_a_reason_to_lose_the_text():
    line = "steps — 30"
    assert i18n.printable(line, "not-a-codec") == "steps - 30"
    assert i18n.printable(line, None) == line


def test_a_speaker_takes_a_text_or_the_two_strings_directly():
    say = i18n.speaker("ru")
    assert say(i18n.Text(en="Cancel", ru="Отмена")) == "Отмена"
    assert say(en="Cancel", ru="Отмена") == "Отмена"


def test_a_speaker_resolves_whatever_it_was_handed():
    assert i18n.speaker("ru-RU")(en="Cancel", ru="Отмена") == "Отмена"


def test_every_group_speaks_both_languages():
    for group in T.GROUPS:
        assert group.title.en and group.title.ru
        assert group.summary.en and group.summary.ru


def test_the_badge_names_every_risk_class():
    # Two different sentences about the same five classes — one has to fit in a
    # coloured chip, the other has to be worth reading — so they live apart and
    # this is what keeps them describing the same set.
    assert set(configure.RISK_LABEL) == set(T.RISKS)
    assert set(configure.RISK_COLOUR) == set(T.RISKS)


def test_the_catalogue_comes_back_in_the_language_it_was_asked_for():
    registry = [T.Tool(name="comfy_status", group="status", risk="reads", enabled=True)]
    english = T.catalogue(registry, "en")
    russian = T.catalogue(registry, "ru")
    assert english["groups"][0]["title"] == "Status"
    assert russian["groups"][0]["title"] == "Состояние"
    assert english["risks"]["reads"] != russian["risks"]["reads"]


def test_a_dependency_warning_is_written_in_both_languages():
    registry = [
        T.Tool(name="run_workflow", group="run", risk="runs", enabled=True),
        T.Tool(name="get_progress", group="run", risk="reads", enabled=False),
    ]
    assert "is on and get_progress is not" in T.warnings(registry, "en")[0]
    assert "включён, а get_progress - нет" in T.warnings(registry, "ru")[0]


def test_every_field_and_client_speaks_both_languages():
    for field in CF.FIELDS:
        assert field.label.en and field.label.ru
    for client in CC.CLIENTS:
        assert client.note.en and client.note.ru
    for shape in CC.SHAPES.values():
        assert shape.summary.en and shape.summary.ru


def test_a_refused_merge_says_why_in_both_languages_and_reads_as_english_in_a_traceback():
    with pytest.raises(CC.MergeRefused) as caught:
        CC.merge("anything: at all\n", CC.HERMES, {})
    assert "only generated here" in str(caught.value)
    assert "только генерируется" in caught.value.of("ru")
