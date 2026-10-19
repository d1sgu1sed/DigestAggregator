from dataclasses import dataclass
from enum import StrEnum

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collector.http import FetchError, fetch
from app.collector.parsers import ParseError, parse_feed
from app.config import settings
from app.db.dedup import normalize_url
from app.db.models import NewsItem, Source, SourceType
from app.db.repository import get_or_create_source

# Пользователь может добавить только ленты, т.к. для API нужен отдельный парсер
FEED_SOURCE_TYPES = (SourceType.RSS, SourceType.GOOGLE_ALERTS)


class FeedType(StrEnum):
    """Типы источников, которые пользователь может добавить сам (только ленты)"""

    RSS = SourceType.RSS.value
    GOOGLE_ALERTS = SourceType.GOOGLE_ALERTS.value


class SourceExistsError(Exception):
    """Источник с таким названием или адресом уже есть"""


class InvalidFeedError(Exception):
    """По адресу нет работающей RSS/Atom-ленты"""


class SourceNotFoundError(Exception):
    """Источника с таким id нет"""


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    type: SourceType
    url: str


DEFAULT_SOURCES = [
    SourceConfig("Хабр", SourceType.RSS, "https://habr.com/ru/rss/news/?fl=ru"),
    SourceConfig("Коммерсантъ", SourceType.RSS, "https://www.kommersant.ru/RSS/news.xml"),
]


def configured_sources() -> list[SourceConfig]:
    sources = list(DEFAULT_SOURCES)
    if settings.google_alerts_feed_url:
        sources.append(
            SourceConfig("Google Alerts", SourceType.GOOGLE_ALERTS, settings.google_alerts_feed_url)
        )
    return sources


def seed_sources(session: Session, sources: list[SourceConfig] | None = None) -> list[Source]:
    """Добавляет в БД источники, которых там ещё нет"""
    return [
        get_or_create_source(session, s.name, s.type, s.url)
        for s in (configured_sources() if sources is None else sources)
    ]


def list_sources_with_counts(session: Session) -> list[tuple[Source, int]]:
    """Все источники и число сохранённых новостей каждого."""
    news_count = (
        select(func.count(NewsItem.id))
        .where(NewsItem.source_id == Source.id)
        .correlate(Source)
        .scalar_subquery()
    )
    return [tuple(row) for row in session.execute(select(Source, news_count).order_by(Source.id))]


def find_source_by_url(session: Session, url: str) -> Source | None:
    """Ищет источник по адресу с учётом нормализации"""
    target = normalize_url(url)
    for source in session.scalars(select(Source)):
        if normalize_url(source.url) == target:
            return source
    return None


def verify_feed(client: httpx.Client, url: str) -> int:
    """Скачивает и разбирает ленту"""
    try:
        return len(parse_feed(fetch(client, url, attempts=1)))
    except FetchError as exc:
        raise InvalidFeedError(f"Лента недоступна: {exc}") from exc
    except ParseError as exc:
        raise InvalidFeedError(f"По адресу нет RSS/Atom-ленты: {exc}") from exc


def add_feed_source(
    session: Session,
    name: str,
    url: str,
    type: SourceType = SourceType.RSS,
    *,
    client: httpx.Client | None = None,
) -> Source:
    """Добавляет пользовательскую ленту"""
    if type not in FEED_SOURCE_TYPES:
        raise ValueError(f"Можно добавить только ленту, а не {type}")

    if session.scalar(select(Source).where(Source.name == name)) is not None:
        raise SourceExistsError(f"Источник с названием {name!r} уже есть")
    if (existing := find_source_by_url(session, url)) is not None:
        raise SourceExistsError(f"Эта лента уже добавлена как {existing.name!r}")

    if client is not None:
        verify_feed(client, url)

    source = Source(name=name, type=type, url=url)
    try:
        with session.begin_nested():
            session.add(source)
            session.flush()
    except IntegrityError as exc:
        raise SourceExistsError("Источник с таким названием или адресом уже есть") from exc
    return source


def remove_source(session: Session, source_id: int) -> None:
    """Удаляет источник вместе со всеми его новостями"""
    source = session.get(Source, source_id)
    if source is None:
        raise SourceNotFoundError(f"Источник с id={source_id} не найден")
    session.delete(source)
    session.flush()
