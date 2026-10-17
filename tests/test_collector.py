"""Сбор новостей end-to-end: подменённая сеть + реальная тестовая БД."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from tenacity import wait_none

from app.collector import collect_all, collect_source, seed_sources
from app.collector.service import get_parser
from app.collector.sources import DEFAULT_SOURCES, SourceConfig, configured_sources
from app.config import settings
from app.db.models import NewsItem, Source, SourceType
from tests.helpers import fixture_bytes, mock_client, respond_with

NO_WAIT = {"wait": wait_none()}


def count_news(session, source=None) -> int:
    stmt = select(func.count()).select_from(NewsItem)
    if source is not None:
        stmt = stmt.where(NewsItem.source_id == source.id)
    return session.scalar(stmt)


def add_source(session, name, type, url, **kwargs) -> Source:
    src = Source(name=name, type=type, url=url, **kwargs)
    session.add(src)
    session.flush()
    return src


@pytest.fixture
def rss_source(session):
    return add_source(session, "RSS", SourceType.RSS, "https://example.com/rss")


@pytest.fixture
def hn_source(session):
    return add_source(session, "HN", SourceType.API, "https://hn.algolia.com/api/v1/search_by_date")


@pytest.fixture
def router():
    """Транспорт, отвечающий по адресу: url -> тело ответа/статус/исключение."""
    routes: dict[str, object] = {}

    def handler(request: httpx.Request):
        step = routes[str(request.url)]
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("simulated", request=request)
        if isinstance(step, int):
            return httpx.Response(step)
        return httpx.Response(200, content=step)

    return routes, mock_client(handler)


def test_collect_rss_saves_news(session, rss_source):
    handler, _ = respond_with(fixture_bytes("rss.xml"))

    result = collect_source(session, rss_source, mock_client(handler), **NO_WAIT)

    assert result.ok
    assert (result.fetched, result.saved) == (2, 2)
    assert count_news(session, rss_source) == 2
    assert rss_source.last_error is None
    assert datetime.now(UTC) - rss_source.last_fetched_at < timedelta(minutes=1)


def test_collect_api_saves_news(session, hn_source):
    handler, _ = respond_with(fixture_bytes("hn.json"))

    result = collect_source(session, hn_source, mock_client(handler), **NO_WAIT)

    assert (result.fetched, result.saved) == (2, 2)
    titles = session.scalars(select(NewsItem.title).order_by(NewsItem.title)).all()
    assert titles == ["Ask HN: How do you read news?", "Show HN: A tiny RSS reader"]


def test_google_alerts_source_uses_feed_parser(session):
    src = add_source(session, "GA", SourceType.GOOGLE_ALERTS, "https://www.google.com/alerts/feeds/1/2")
    handler, _ = respond_with(fixture_bytes("google_alerts.xml"))

    assert collect_source(session, src, mock_client(handler), **NO_WAIT).saved == 1
    assert session.scalar(select(NewsItem.url)) == "https://python.org/news/315"


def test_repeated_collection_saves_nothing_new(session, rss_source):
    handler, _ = respond_with(fixture_bytes("rss.xml"))
    client = mock_client(handler)

    collect_source(session, rss_source, client, **NO_WAIT)
    result = collect_source(session, rss_source, client, **NO_WAIT)

    assert (result.fetched, result.saved) == (2, 0)
    assert count_news(session) == 2


def test_network_failure_recorded_in_source(session, rss_source):
    handler, calls = respond_with(httpx.ConnectTimeout)

    result = collect_source(session, rss_source, mock_client(handler), **NO_WAIT)

    assert not result.ok
    assert "ConnectTimeout" in result.error
    assert len(calls) == 3  # были повторы
    assert rss_source.last_error == result.error
    assert rss_source.last_fetched_at is not None
    assert count_news(session) == 0


def test_parse_failure_recorded_in_source(session, rss_source):
    handler, _ = respond_with(b"<html>Service unavailable")

    result = collect_source(session, rss_source, mock_client(handler), **NO_WAIT)

    assert "Некорректная лента" in result.error
    assert rss_source.last_error == result.error


def test_unknown_api_recorded_as_error(session):
    src = add_source(session, "Unknown", SourceType.API, "https://api.unknown.example/news")
    handler, calls = respond_with(b"{}")

    result = collect_source(session, src, mock_client(handler), **NO_WAIT)

    assert "Нет парсера" in result.error


def test_success_clears_previous_error(session, rss_source):
    rss_source.last_error = "old error"
    handler, _ = respond_with(fixture_bytes("rss.xml"))

    collect_source(session, rss_source, mock_client(handler), **NO_WAIT)

    assert rss_source.last_error is None


def test_collect_all_isolates_failing_source(session, router, rss_source, hn_source):
    routes, client = router
    routes[rss_source.url] = httpx.ConnectError
    routes[hn_source.url] = fixture_bytes("hn.json")

    results = collect_all(session, client, **NO_WAIT)

    assert [(r.source, r.ok) for r in results] == [("RSS", False), ("HN", True)]
    assert count_news(session, hn_source) == 2
    assert rss_source.last_error is not None


def test_collect_all_skips_inactive_sources(session, router, rss_source):
    add_source(session, "Off", SourceType.RSS, "https://off.example/rss", is_active=False)
    routes, client = router
    routes[rss_source.url] = fixture_bytes("rss.xml")

    results = collect_all(session, client, **NO_WAIT)

    assert [r.source for r in results] == ["RSS"]  # к off.example запроса не было


def test_collect_all_without_sources(session, router):
    _, client = router
    assert collect_all(session, client) == []


def test_seed_sources_is_idempotent(session):
    configs = [
        SourceConfig("A", SourceType.RSS, "https://a.example/rss"),
        SourceConfig("B", SourceType.API, "https://hn.algolia.com/api/v1/search"),
    ]

    seed_sources(session, configs)
    seed_sources(session, configs)

    assert session.scalars(select(Source.name).order_by(Source.name)).all() == ["A", "B"]


def test_default_sources_have_parsers():
    for cfg in DEFAULT_SOURCES:
        assert get_parser(Source(name=cfg.name, type=cfg.type, url=cfg.url))


def test_google_alerts_added_only_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "google_alerts_feed_url", None)
    assert all(s.type is not SourceType.GOOGLE_ALERTS for s in configured_sources())

    monkeypatch.setattr(settings, "google_alerts_feed_url", "https://www.google.com/alerts/feeds/1/2")
    assert configured_sources()[-1].type is SourceType.GOOGLE_ALERTS
