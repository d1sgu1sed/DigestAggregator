from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Source, SourceType
from app.db.repository import get_or_create_source


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    type: SourceType
    url: str


DEFAULT_SOURCES = [
    SourceConfig("BBC News - World", SourceType.RSS, "https://feeds.bbci.co.uk/news/world/rss.xml"),
    SourceConfig("Meduza", SourceType.RSS, "https://meduza.io/rss/all"),
    SourceConfig(
        "Hacker News",
        SourceType.API,
        "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=50",
    ),
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
