from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.models import LLMModel
from sqlalchemy import URL

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _pg_url(user: str, password: SecretStr, host: str, port: int, db: str) -> URL:
    # экранируем спецсимволы в пароле
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password.get_secret_value(),
        host=host,
        port=port,
        database=db,
    )


class Settings(BaseSettings):
    """Настройки приложения

    Читаются из переменных окружения и файла .env в корне проекта;
    переменные окружения имеют приоритет над .env. Тот же .env
    использует docker-compose.yml.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Рабочая БД
    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432

    # Тестовая БД
    test_postgres_user: str
    test_postgres_password: SecretStr
    test_postgres_db: str
    test_postgres_host: str = "127.0.0.1"
    test_postgres_port: int = 5433

    # RSS-лента Google Alerts
    google_alerts_feed_url: str | None = None

    # Планировщик сбора новостей (запускается вместе с API)
    scheduler_enabled: bool = True
    collect_interval_minutes: int = Field(default=30, gt=0)

    # LLM для описаний новостей
    ollama_url: str = "http://127.0.0.1:11434"
    llm_model: LLMModel = LLMModel.GEMMA3_4B
    llm_timeout_seconds: float = Field(default=120, gt=0)
    llm_digest_per_source_per_day: int = Field(default=10, gt=0)

    @property
    def database_url(self) -> URL:
        return _pg_url(
            self.postgres_user,
            self.postgres_password,
            self.postgres_host,
            self.postgres_port,
            self.postgres_db,
        )

    @property
    def test_database_url(self) -> URL:
        return _pg_url(
            self.test_postgres_user,
            self.test_postgres_password,
            self.test_postgres_host,
            self.test_postgres_port,
            self.test_postgres_db,
        )


settings = Settings()
