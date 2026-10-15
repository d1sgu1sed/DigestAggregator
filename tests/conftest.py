from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Source, SourceType

TEST_DATABASE_URL = settings.test_database_url
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


@pytest.fixture(scope="session")
def alembic_cfg() -> Config:
    cfg = Config(str(ALEMBIC_INI))
    url = TEST_DATABASE_URL.render_as_string(hide_password=False)
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def engine(alembic_cfg: Config) -> Iterator[Engine]:
    """Тестовая БД со схемой, накатанной реальными миграциями"""
    engine = create_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 3})
    try:
        engine.connect().close()
    except OperationalError as exc:
        pytest.skip(f"Тестовая БД недоступна ({TEST_DATABASE_URL}): {exc.orig}")

    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    yield engine
    command.downgrade(alembic_cfg, "base")
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Сессия внутри внешней транзакции, которая откатывается после теста"""
    with engine.connect() as conn:
        trans = conn.begin()
        session = Session(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        yield session
        session.close()
        trans.rollback()


@pytest.fixture
def source(session: Session) -> Source:
    src = Source(name="Example", type=SourceType.RSS, url="https://example.com/rss")
    session.add(src)
    session.flush()
    return src


@pytest.fixture
def other_source(session: Session) -> Source:
    src = Source(name="Other", type=SourceType.API, url="https://other.com/api")
    session.add(src)
    session.flush()
    return src

