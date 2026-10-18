import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collector.http import FetchError, create_client, fetch
from app.collector.parsers import ParseError, Parser, get_api_parser, parse_feed
from app.db.models import Source, SourceType
from app.db.repository import save_news
from app.db.session import session_scope

logger = logging.getLogger(__name__)

class CollectionInProgressError(Exception):
    """Сбор уже идёт, второй параллельно не запускаем"""


_collect_lock = threading.Lock()

@dataclass(frozen=True, slots=True)
class CollectResult:
    source: str
    fetched: int = 0
    saved: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def get_parser(source: Source) -> Parser:
    if source.type in (SourceType.RSS, SourceType.GOOGLE_ALERTS):
        return parse_feed
    return get_api_parser(source.url)


def collect_source(session: Session, source: Source, client: httpx.Client, **fetch_options) -> CollectResult:
    """Скачивает, разбирает и сохраняет новости одного источника"""
    source.last_fetched_at = datetime.now(UTC)
    try:
        items = get_parser(source)(fetch(client, source.url, **fetch_options))
    except (FetchError, ParseError) as exc:
        source.last_error = str(exc)
        logger.warning("Источник %r недоступен: %s", source.name, exc)
        return CollectResult(source.name, error=str(exc))

    saved = save_news(session, source.id, items)
    source.last_error = None
    logger.info("Источник %r: получено %d, новых %d", source.name, len(items), saved)
    return CollectResult(source.name, fetched=len(items), saved=saved)


def collect_all(session: Session, client: httpx.Client | None = None, **fetch_options) -> list[CollectResult]:
    """Собирает новости со всех активных источников, фиксируя каждый отдельно"""
    sources = session.scalars(select(Source).where(Source.is_active).order_by(Source.id)).all()
    own_client = client is None
    client = client or create_client()
    try:
        results = []
        for source in sources:
            results.append(collect_source(session, source, client, **fetch_options))
            session.commit()
        return results
    finally:
        if own_client:
            client.close()


def run_collection(
    session_factory: Callable[[], AbstractContextManager[Session]] = session_scope,
    client: httpx.Client | None = None,
    **fetch_options,
) -> list[CollectResult]:
    """Единая точка запуска сбора для планировщика и API"""
    if not _collect_lock.acquire(blocking=False):
        raise CollectionInProgressError("Сбор новостей уже выполняется")
    try:
        with session_factory() as session:
            return collect_all(session, client, **fetch_options)
    finally:
        _collect_lock.release()
