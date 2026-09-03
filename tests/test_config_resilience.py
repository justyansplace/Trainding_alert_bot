"""Кривая переменная окружения не должна ронять сервис в цикл перезапусков.

Падение конфига происходит до подъёма HTTP-проверки живости: порт не
открывается вовсе, платформа видит только «service unavailable» и крутит
рестарт по кругу, не сообщая причины. Цена опечатки в регистре получается
непропорциональна самой опечатке.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from alert_bot import config
from alert_bot.main import _report_bad_config


@pytest.fixture(autouse=True)
def _base_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("ADMIN_TG_ID", "1")
    config._settings = None
    yield
    config._settings = None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("practice", "practice"),
        ("live", "live"),
        ("Practice", "practice"),      # регистр
        ("  practice  ", "practice"),  # пробелы при копировании
        ("demo", "practice"),          # так счёт называет сама площадка
        ("DEMO", "practice"),
        ("fxpractice", "practice"),
        ("fxtrade", "live"),
        ("real", "live"),
    ],
)
def test_oanda_environment_is_forgiving(monkeypatch, given: str, expected: str) -> None:
    monkeypatch.setenv("OANDA_ENVIRONMENT", given)
    assert config.Settings().oanda_environment == expected


@pytest.mark.parametrize("given", ["openai", "OpenAI", " anthropic ", "ANTHROPIC"])
def test_llm_provider_is_forgiving(monkeypatch, given: str) -> None:
    assert config.Settings(llm_provider=given).llm_provider == given.strip().lower()


def test_blank_variable_falls_back_to_default(monkeypatch) -> None:
    """Переменную часто создают и оставляют пустой — это «не задано», а не «мусор»."""
    monkeypatch.setenv("OANDA_ENVIRONMENT", "")
    monkeypatch.setenv("MAX_INSTRUMENTS", "")
    monkeypatch.setenv("AUTO_ARCHIVE_PCT", "   ")

    settings = config.Settings()
    assert settings.oanda_environment == "practice"
    assert settings.max_instruments == 25
    assert settings.auto_archive_pct == 3.0


def test_blank_required_variable_reads_as_missing(monkeypatch) -> None:
    """Пустой обязательный токен должен жаловаться на отсутствие, а не на тип."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(ValidationError) as caught:
        config.Settings()
    assert [e["type"] for e in caught.value.errors()] == ["missing"]


def test_truly_unknown_value_still_refuses(monkeypatch) -> None:
    """Прощать всё нельзя: неизвестная площадка — это ошибка, а не синоним."""
    monkeypatch.setenv("OANDA_ENVIRONMENT", "tickmill")
    with pytest.raises(ValidationError):
        config.Settings()


def test_message_names_the_value_and_the_allowed_ones(monkeypatch, capsys) -> None:
    """На хостинге значение скрыто звёздочками — его надо показать в логе."""
    monkeypatch.setenv("OANDA_ENVIRONMENT", "tickmill")
    with pytest.raises(ValidationError) as caught:
        config.Settings()

    _report_bad_config(caught.value)
    err = capsys.readouterr().err

    assert "OANDA_ENVIRONMENT" in err
    assert "tickmill" in err
    assert "practice" in err and "live" in err
    assert "Заданы неверно" in err


def test_message_separates_missing_from_invalid(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("OANDA_ENVIRONMENT", "tickmill")
    # _env_file=None: локальный .env подставил бы токен и «пропажи» не вышло бы.
    with pytest.raises(ValidationError) as caught:
        config.Settings(_env_file=None)

    _report_bad_config(caught.value)
    err = capsys.readouterr().err

    assert "Не заданы:" in err and "TELEGRAM_BOT_TOKEN" in err
    assert "Заданы неверно:" in err and "OANDA_ENVIRONMENT" in err


# --------------------------------------------------------------------------- #
# Опечатка в имени переменной
# --------------------------------------------------------------------------- #


def test_misnamed_variable_is_pointed_out() -> None:
    """extra="ignore" молча выбрасывает чужое имя — опечатка иначе тихая."""
    found = dict(
        config.unrecognized_env_vars(
            {"OANDA_API_KEY": "x", "TELEGRAM_TOKEN": "y", "ADMIN_ID": "1"}
        )
    )
    assert found["OANDA_API_KEY"] == "OANDA_API_TOKEN"
    assert found["TELEGRAM_TOKEN"] == "TELEGRAM_BOT_TOKEN"
    assert found["ADMIN_ID"] == "ADMIN_TG_ID"


def test_correct_names_are_not_flagged() -> None:
    assert config.unrecognized_env_vars(
        {"OANDA_API_TOKEN": "x", "ADMIN_TG_IDS": "1", "AUTO_ARCHIVE_PCT": "3"}
    ) == []


def test_foreign_variables_stay_quiet() -> None:
    """Хостинг подмешивает свои переменные — ругаться на них было бы шумом."""
    assert config.unrecognized_env_vars(
        {"PATH": "/usr/bin", "HOME": "/root", "PORT": "8080",
         "RAILWAY_SERVICE_ID": "abc", "LANG": "C.UTF-8"}
    ) == []
