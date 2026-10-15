from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, joinedload

from app.db.dedup import url_hash
from app.db.models import DigestPeriod, NewsItem, Source, SourceType

PERIOD_DELTAS = {
    DigestPeriod.DAY: timedelta(days=1),
    DigestPeriod.WEEK: timedelta(weeks=1),
}


@dataclass(frozen=True, slots=True)
class NewsItemData:
    """Новость в том виде, в каком её отдаёт парсер."""

    external_id: str
    url: str
    title: str
    published_at: datetime
    summary: str | None = None
    author: str | None = None


def get_or_create_source(session: Session, name: str, type: SourceType, url: str) -> Source:
    source = session.scalar(select(Source).where(Source.url == url))
    if source is None:
        source = Source(name=name, type=type, url=url)
        session.add(source)
        session.flush()
    return source


def save_news(session: Session, source_id: int, items: Iterable[NewsItemData]) -> int:
    """Вставляет новости, молча пропуская дубли. Возвращает число новых записей."""
    rows = [
        {
            "source_id": source_id,
            "external_id": item.external_id,
            "url": item.url,
            "url_hash": url_hash(item.url),
            "title": item.title,
            "summary": item.summary,
            "author": item.author,
            "published_at": item.published_at,
        }
        for item in items
    ]
    if not rows:
        return 0

    # ON CONFLICT DO NOTHING срабатывает на любое уникальное ограничение:
    # и url_hash, и (source_id, external_id).
    stmt = insert(NewsItem).values(rows).on_conflict_do_nothing().returning(NewsItem.id)
    return len(session.execute(stmt).all())


def period_bounds(period: DigestPeriod, now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or datetime.now(UTC)
    return end - PERIOD_DELTAS[period], end


def get_news_for_period(
    session: Session, start: datetime, end: datetime, limit: int | None = None
) -> Sequence[NewsItem]:
    stmt = (
        select(NewsItem)
        .options(joinedload(NewsItem.source))
        .where(NewsItem.published_at >= start, NewsItem.published_at < end)
        .order_by(NewsItem.published_at.desc())
        .limit(limit)
    )
    return session.scalars(stmt).all()
