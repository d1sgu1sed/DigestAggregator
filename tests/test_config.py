"""Настройки: чтение из окружения/.env и сборка URL подключения."""

import pytest
from pydantic import ValidationError
from sqlalchemy import make_url

from app.config import Settings

REQUIRED = {
    "POSTGRES_USER": "user",
    "POSTGRES_PASSWORD": "secret",
    "POSTGRES_DB": "news",
    "TEST_POSTGRES_USER": "test_user",
    "TEST_POSTGRES_PASSWORD": "test_secret",
    "TEST_POSTGRES_DB": "news_test",
}


@pytest.fixture
def env(monkeypatch):
    """Чистое окружение только с обязательными переменными"""
    for key in [*REQUIRED, "POSTGRES_HOST", "POSTGRES_PORT", "TEST_POSTGRES_HOST", "TEST_POSTGRES_PORT"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def load() -> Settings:
    return Settings(_env_file=None)


def test_database_url_built_from_parts(env):
    url = load().database_url

    assert url.drivername == "postgresql+psycopg"
    assert (url.username, url.password, url.database) == ("user", "secret", "news")
    assert (url.host, url.port) == ("127.0.0.1", 5432)


def test_test_database_url_uses_test_settings(env):
    url = load().test_database_url

    assert (url.username, url.database, url.port) == ("test_user", "news_test", 5433)


def test_env_overrides_defaults(env):
    env.setenv("POSTGRES_HOST", "db")
    env.setenv("POSTGRES_PORT", "6543")

    url = load().database_url
    assert (url.host, url.port) == ("db", 6543)


def test_password_with_special_chars_survives_roundtrip(env):
    env.setenv("POSTGRES_PASSWORD", "p@ss:w/rd%#")

    rendered = load().database_url.render_as_string(hide_password=False)
    assert make_url(rendered).password == "p@ss:w/rd%#"


def test_password_hidden_in_repr(env):
    settings = load()

    assert "secret" not in repr(settings)
    assert "secret" not in str(settings.database_url)


def test_missing_required_variable_fails(env):
    env.delenv("POSTGRES_PASSWORD")

    with pytest.raises(ValidationError, match="postgres_password"):
        load()


def test_reads_env_file(env, tmp_path):
    env.delenv("POSTGRES_DB")
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_DB=from_file\n")

    assert Settings(_env_file=env_file).postgres_db == "from_file"


def test_environment_has_priority_over_env_file(env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_DB=from_file\n")

    assert Settings(_env_file=env_file).postgres_db == "news"
