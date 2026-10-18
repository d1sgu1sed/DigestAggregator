"""Проверка ограничений схемы на уровне PostgreSQL."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.db import session as db_session
from app.db.models import Digest, DigestPeriod, NewsItem, Source, SourceType
from app.db.repository import save_news
from tests.factories import make_item


def assert_violates(session, obj, constraint: str):
    with pytest.raises(IntegrityError, match=constraint):
        with session.begin_nested():
            session.add(obj)
            session.flush()


def test_enum_stored_as_value(session, source):
    raw = session.execute(text("SELECT type::text FROM sources WHERE id = :id"), {"id": source.id})
    assert raw.scalar() == "rss"


def test_source_url_unique(session, source):
    dup = Source(name="Another name", type=SourceType.RSS, url=source.url)
    assert_violates(session, dup, "uq_sources_url")


def test_source_name_unique(session, source):
    dup = Source(name=source.name, type=SourceType.RSS, url="https://new.com/rss")
    assert_violates(session, dup, "uq_sources_name")


def test_news_url_hash_unique_via_orm(session, source):
    """Уникальность держит сама БД, а не только ON CONFLICT в репозитории."""
    save_news(session, source.id, [make_item(1)])
    existing = session.scalar(select(NewsItem))

    dup = NewsItem(
        source_id=source.id,
        external_id="new-guid",
        url=existing.url,
        url_hash=existing.url_hash,
        title="Dup",
        published_at=datetime.now(UTC),
    )
    assert_violates(session, dup, "uq_news_items_url_hash")


def test_news_requires_existing_source(session):
    orphan = NewsItem(
        source_id=999_999,
        external_id="x",
        url="https://example.com/x",
        url_hash="0" * 64,
        title="Orphan",
        published_at=datetime.now(UTC),
    )
    assert_violates(session, orphan, "fk_news_items_source_id_sources")


def test_deleting_source_cascades_to_news(session, source, other_source):
    save_news(session, source.id, [make_item(1), make_item(2)])
    save_news(session, other_source.id, [make_item(3)])

    session.execute(delete(Source).where(Source.id == source.id))

    assert session.scalar(select(func.count()).select_from(NewsItem)) == 1


def test_source_tracks_fetch_errors(session, source):
    source.last_fetched_at = datetime.now(UTC)
    source.last_error = "httpx.ConnectTimeout: timed out"
    session.flush()
    session.expire(source)

    assert source.last_error.startswith("httpx.ConnectTimeout")


def test_digest_saved(session):
    end = datetime(2026, 10, 14, tzinfo=UTC)
    digest = Digest(
        period=DigestPeriod.WEEK,
        period_start=end - timedelta(weeks=1),
        period_end=end,
        news_count=2,
        content="- News 1\n- News 2",
    )
    session.add(digest)
    session.flush()
    session.expire(digest)

    assert digest.id is not None
    assert digest.period is DigestPeriod.WEEK
    assert digest.created_at is not None


@pytest.fixture
def scoped(session, monkeypatch):
    """Перенаправляет session_scope на тестовое соединение."""
    monkeypatch.setattr(db_session, "SessionLocal", lambda: session)
    monkeypatch.setattr(session, "close", lambda: None)  # сессию закроет фикстура
    return db_session.session_scope


def test_session_scope_commits(scoped, session):
    with scoped() as s:
        s.add(Source(name="Committed", type=SourceType.RSS, url="https://c.com/rss"))

    assert session.scalar(select(Source.name)) == "Committed"


def test_session_scope_rolls_back_on_error(scoped, session):
    with pytest.raises(RuntimeError):
        with scoped() as s:
            s.add(Source(name="Lost", type=SourceType.RSS, url="https://l.com/rss"))
            s.flush()
            raise RuntimeError("boom")

    assert session.scalar(select(func.count()).select_from(Source)) == 0
