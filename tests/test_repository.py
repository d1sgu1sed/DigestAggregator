"""Интеграционные тесты репозитория на реальном PostgreSQL."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db.models import DigestPeriod, NewsItem, Source, SourceType
from app.db.repository import (
    get_news_for_period,
    get_or_create_source,
    period_bounds,
    save_news,
)
from tests.factories import make_item


def count_news(session) -> int:
    return session.scalar(select(func.count()).select_from(NewsItem))


def test_get_or_create_source_creates_new(session):
    src = get_or_create_source(session, "Habr", SourceType.RSS, "https://habr.com/rss")

    assert src.id is not None
    assert src.is_active is True
    assert src.created_at is not None


def test_get_or_create_source_returns_existing(session):
    first = get_or_create_source(session, "Habr", SourceType.RSS, "https://habr.com/rss")
    second = get_or_create_source(session, "Habr", SourceType.RSS, "https://habr.com/rss")

    assert first.id == second.id
    assert session.scalar(select(func.count()).select_from(Source)) == 1


def test_save_news_inserts_items(session, source):
    inserted = save_news(session, source.id, [make_item(1), make_item(2), make_item(3)])

    assert inserted == 3
    assert count_news(session) == 3


def test_save_news_empty_input(session, source):
    assert save_news(session, source.id, []) == 0
    assert count_news(session) == 0


def test_save_news_persists_all_fields(session, source):
    published = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    item = make_item(
        1,
        published_at=published,
        url="https://www.example.com/news/1?utm_source=rss",
        summary="Short summary",
        author="John Doe",
    )
    save_news(session, source.id, [item])

    news = session.scalar(select(NewsItem))
    assert isinstance(news.id, uuid.UUID)
    assert news.source_id == source.id
    assert news.external_id == "guid-1"
    assert news.url == item.url  # оригинальный URL сохраняется как есть
    assert len(news.url_hash) == 64
    assert news.title == "News 1"
    assert news.summary == "Short summary"
    assert news.author == "John Doe"
    assert news.published_at == published
    assert news.fetched_at is not None
    assert news.llm_summary is None


def test_save_news_accepts_generator(session, source):
    assert save_news(session, source.id, (make_item(i) for i in range(5))) == 5


def test_repeated_save_does_not_duplicate(session, source):
    items = [make_item(1), make_item(2)]

    assert save_news(session, source.id, items) == 2
    assert save_news(session, source.id, items) == 0
    assert count_news(session) == 2


def test_duplicates_within_one_batch_are_skipped(session, source):
    assert save_news(session, source.id, [make_item(1), make_item(1)]) == 1
    assert count_news(session) == 1


def test_same_url_with_tracking_garbage_is_duplicate(session, source):
    save_news(session, source.id, [make_item(1)])
    dup = make_item(
        1, external_id="another-guid", url="https://www.example.com/news/1/?utm_source=tg#top"
    )

    assert save_news(session, source.id, [dup]) == 0
    assert count_news(session) == 1


def test_same_guid_in_same_source_is_duplicate(session, source):
    save_news(session, source.id, [make_item(1)])
    edited = make_item(1, url="https://example.com/news/1-edited", title="Edited")

    assert save_news(session, source.id, [edited]) == 0
    assert session.scalar(select(NewsItem.title)) == "News 1"  # первая версия не перезаписана


def test_same_guid_in_different_sources_is_allowed(session, source, other_source):
    save_news(session, source.id, [make_item(1)])
    same_guid = make_item(1, url="https://other.com/news/1")

    assert save_news(session, other_source.id, [same_guid]) == 1
    assert count_news(session) == 2


def test_same_article_from_different_sources_is_duplicate(session, source, other_source):
    save_news(session, source.id, [make_item(1)])
    same_url = make_item(1, external_id="other-feed-id")

    assert save_news(session, other_source.id, [same_url]) == 0
    assert count_news(session) == 1


def test_batch_with_mix_of_new_and_existing(session, source):
    save_news(session, source.id, [make_item(1), make_item(2)])

    assert save_news(session, source.id, [make_item(1), make_item(2), make_item(3)]) == 1
    assert count_news(session) == 3


def test_period_bounds_day():
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    assert period_bounds(DigestPeriod.DAY, now) == (now - timedelta(days=1), now)


def test_period_bounds_week():
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    assert period_bounds(DigestPeriod.WEEK, now) == (now - timedelta(days=7), now)


def test_period_bounds_defaults_to_now_utc():
    before = datetime.now(UTC)
    start, end = period_bounds(DigestPeriod.DAY)

    assert before <= end <= datetime.now(UTC)
    assert end.tzinfo is not None
    assert end - start == timedelta(days=1)


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def seed_timeline(session, source_id):
    save_news(
        session,
        source_id,
        [
            make_item(1, published_at=NOW - timedelta(hours=1)),
            make_item(2, published_at=NOW - timedelta(hours=20)),
            make_item(3, published_at=NOW - timedelta(days=3)),
            make_item(4, published_at=NOW - timedelta(days=10)),
            make_item(5, published_at=NOW + timedelta(hours=1)),
        ],
    )


def titles(news):
    return [n.title for n in news]


def test_news_for_day(session, source):
    seed_timeline(session, source.id)
    news = get_news_for_period(session, *period_bounds(DigestPeriod.DAY, NOW))

    assert titles(news) == ["News 1", "News 2"]


def test_news_for_week(session, source):
    seed_timeline(session, source.id)
    news = get_news_for_period(session, *period_bounds(DigestPeriod.WEEK, NOW))

    assert titles(news) == ["News 1", "News 2", "News 3"]


def test_news_sorted_newest_first(session, source):
    seed_timeline(session, source.id)
    news = get_news_for_period(session, NOW - timedelta(days=30), NOW + timedelta(days=1))

    dates = [n.published_at for n in news]
    assert dates == sorted(dates, reverse=True)


def test_period_start_inclusive_end_exclusive(session, source):
    start, end = NOW - timedelta(days=1), NOW
    save_news(
        session,
        source.id,
        [make_item(1, published_at=start), make_item(2, published_at=end)],
    )

    assert titles(get_news_for_period(session, start, end)) == ["News 1"]


def test_news_for_period_limit(session, source):
    seed_timeline(session, source.id)
    news = get_news_for_period(session, NOW - timedelta(days=30), NOW, limit=2)

    assert titles(news) == ["News 1", "News 2"]


def test_news_for_period_empty(session, source):
    assert get_news_for_period(session, *period_bounds(DigestPeriod.DAY, NOW)) == []


def test_news_for_period_loads_source(session, source):
    save_news(session, source.id, [make_item(1, published_at=NOW - timedelta(hours=1))])
    session.expunge_all()

    [news] = get_news_for_period(session, *period_bounds(DigestPeriod.DAY, NOW))
    session.expunge_all() # источник должен быть загружен заранее, без повторного запроса

    assert news.source.name == "Example"
