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


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ('"practice"', "practice"),      # так строки пишут в документации
        ("'practice'", "practice"),
        ('  "practice"  ', "practice"),
        ('"demo"', "practice"),          # кавычки поверх синонима
    ],
)
def test_quotes_do_not_leak_into_the_value(monkeypatch, given: str, expected: str) -> None:
    """KEY="value" из примера переносят в панель вместе с кавычками.

    Кавычка уезжает внутрь значения, Literal его отвергает, и сервис не
    поднимается — при том что глазами значение выглядит правильным.
    """
    monkeypatch.setenv("OANDA_ENVIRONMENT", given)
    assert config.Settings().oanda_environment == expected


@pytest.mark.parametrize("given", ['"abc123"', "'abc123'", "  abc123  ", '" abc123 "'])
def test_quotes_are_stripped_from_secrets_too(monkeypatch, given: str) -> None:
    """Токен в кавычках ушёл бы в заголовок как Bearer "abc123" и получил 401."""
    monkeypatch.setenv("OANDA_API_TOKEN", given)
    assert config.Settings().oanda_api_token == "abc123"


def test_empty_quotes_read_as_unset(monkeypatch) -> None:
    monkeypatch.setenv("OANDA_ENVIRONMENT", '""')
    assert config.Settings().oanda_environment == "practice"


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


# --------------------------------------------------------------------------- #
# Список администраторов
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "given",
    [
        "561540261, 645625357",
        "561540261,645625357",
        "561540261; 645625357",
        '"561540261, 645625357"',
        "  561540261 ,  645625357  ",
    ],
)
def test_admin_list_in_the_singular_variable_is_understood(monkeypatch, given: str) -> None:
    """ADMIN_TG_ID и ADMIN_TG_IDS различаются одной буквой — это ловушка конфига.

    Человек, которому нужны два администратора, пишет оба номера в ту
    переменную, которую уже видит в панели. Раньше это роняло запуск.
    """
    monkeypatch.setenv("ADMIN_TG_ID", given)
    monkeypatch.delenv("ADMIN_TG_IDS", raising=False)

    settings = config.Settings(_env_file=None)
    assert settings.admin_tg_id == 561540261       # владелец — первый
    assert settings.admin_ids == [561540261, 645625357]


def test_both_variables_merge(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_TG_ID", "561540261, 645625357")
    monkeypatch.setenv("ADMIN_TG_IDS", "999")
    assert config.Settings(_env_file=None).admin_ids == [561540261, 645625357, 999]


def test_single_id_is_unchanged(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_TG_ID", "561540261")
    monkeypatch.delenv("ADMIN_TG_IDS", raising=False)
    settings = config.Settings(_env_file=None)
    assert settings.admin_tg_id == 561540261
    assert settings.admin_ids == [561540261]


def test_owner_is_the_first_in_the_list(monkeypatch) -> None:
    """Порядок не косметика: с владельца нельзя снять роль из бота."""
    monkeypatch.setenv("ADMIN_TG_ID", "645625357, 561540261")
    monkeypatch.delenv("ADMIN_TG_IDS", raising=False)
    assert config.Settings(_env_file=None).admin_tg_id == 645625357


def test_garbage_in_the_list_still_refuses(monkeypatch) -> None:
    """Прощать всё нельзя: буквы вместо номера — это ошибка, а не список."""
    monkeypatch.setenv("ADMIN_TG_ID", "не-номер, 645625357")
    with pytest.raises(ValidationError):
        config.Settings(_env_file=None)
