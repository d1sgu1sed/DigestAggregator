"""Миграции Alembic должны совпадать с моделями и корректно откатываться."""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from app.db.base import Base


def test_migrations_match_models(engine):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)

    assert diff == [], f"Модели расходятся с миграциями: {diff}"


def test_downgrade_and_upgrade_roundtrip(engine, alembic_cfg):
    command.downgrade(alembic_cfg, "base")
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) <= {"alembic_version"}
        enums = conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'")).scalars()
        assert set(enums) == set()

    command.upgrade(alembic_cfg, "head")
    with engine.connect() as conn:
        assert {"sources", "news_items", "digests"} <= set(inspect(conn).get_table_names())
