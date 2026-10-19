import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, joinedload

from app.db.models import Digest, DigestPeriod, DigestStatus, NewsItem
from app.db.repository import period_bounds
from app.llm.client import LLMError, OllamaClient
from app.llm.models import LLMModel
from app.llm.prompts import EVENTS_SYSTEM, parse_list

logger = logging.getLogger(__name__)

CHUNK_CHARS = 3000
EVENTS_LIMIT = {DigestPeriod.DAY: 7, DigestPeriod.WEEK: 10}
MAX_REDUCE_ROUNDS = 5
PERIOD_TITLES = {DigestPeriod.DAY: "день", DigestPeriod.WEEK: "неделю"}


class DigestInProgressError(Exception):
    """Дайджест уже генерируется - второй параллельно не запускаем"""


class DigestNotFoundError(Exception):
    """Дайджеста с таким id нет"""


_generation_lock = threading.Lock()


def select_titles(
    session: Session, start: datetime, end: datetime, per_source_per_day: int
) -> tuple[list[str], int]:
    """Заголовки за период: не больше per_source_per_day свежих от источника в день"""
    news = session.scalars(
        select(NewsItem)
        .options(joinedload(NewsItem.source))
        .where(NewsItem.published_at >= start, NewsItem.published_at < end)
        .order_by(NewsItem.published_at.desc())
    ).all()

    taken: dict[tuple[int, object], int] = defaultdict(int)
    selected = []
    for n in news:
        key = (n.source_id, n.published_at.date())
        if taken[key] < per_source_per_day:
            taken[key] += 1
            selected.append(n)
    selected.reverse()
    return [f"[{n.published_at:%d.%m}] {n.title}" for n in selected], len(news)


def chunk_lines(lines: list[str], max_chars: int = CHUNK_CHARS) -> list[list[str]]:
    chunks: list[list[str]] = [[]]
    size = 0
    for line in lines:
        if chunks[-1] and size + len(line) > max_chars:
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += len(line) + 1
    return [c for c in chunks if c]


def extract_events(client: OllamaClient, model: LLMModel, lines: list[str], limit: int) -> list[str]:
    """Главные события из строк любой длины - по шагам, пока не останется одна порция"""
    for round_ in range(1, MAX_REDUCE_ROUNDS + 1):
        chunks = chunk_lines(lines)
        events = []
        for i, chunk in enumerate(chunks, 1):
            logger.info("Дайджест: раунд %d, порция %d/%d", round_, i, len(chunks))
            answer = client.chat(
                model, EVENTS_SYSTEM.format(limit=limit), "\n".join(chunk), max_tokens=60 * limit
            )
            events.extend(parse_list(answer))
        if not events:
            raise LLMError("Модель не вернула ни одного события в виде списка")
        if len(chunks) == 1:
            return events[:limit]
        lines = events
    return lines[:limit]


def render_llm_digest(digest: Digest, events: list[str], selected: int) -> str:
    fmt = "%d.%m.%Y %H:%M"
    header = [
        f"Главные события за {PERIOD_TITLES[digest.period]}",
        f"Период: {digest.period_start:{fmt}} - {digest.period_end:{fmt}} UTC",
        f"Модель: {digest.llm_model}; новостей в выборке: {selected} из {digest.news_count}",
        "",
    ]
    return "\n".join(header + [f"{i}. {e}" for i, e in enumerate(events, 1)]) + "\n"


def start_llm_digest(
    session: Session,
    period: DigestPeriod,
    model: LLMModel,
    *,
    now: datetime | None = None,
) -> Digest:
    """Создаёт задачу генерации и занимает блокировку"""
    if not _generation_lock.acquire(blocking=False):
        raise DigestInProgressError("Дайджест уже генерируется, дождитесь окончания")
    try:
        start, end = period_bounds(period, now)
        digest = Digest(
            period=period,
            period_start=start,
            period_end=end,
            news_count=0,
            status=DigestStatus.PENDING,
            llm_model=str(model),
        )
        session.add(digest)
        session.flush()
        return digest
    except BaseException:
        _generation_lock.release()
        raise


def abandon_llm_digest() -> None:
    """Снимает блокировку"""
    _generation_lock.release()


def run_llm_digest(
    digest_id: int,
    session_factory: Callable[[], AbstractContextManager[Session]],
    client_factory: Callable[[], OllamaClient],
    per_source_per_day: int,
) -> None:
    """Фоновая генерация дайджеста"""
    try:
        with session_factory() as session:
            digest = session.get(Digest, digest_id)
            digest.status = DigestStatus.RUNNING
            session.commit()

            try:
                lines, total = select_titles(
                    session, digest.period_start, digest.period_end, per_source_per_day
                )
                digest.news_count = total
                if not lines:
                    content = "За этот период новостей нет.\n"
                else:
                    with client_factory() as client:
                        events = extract_events(
                            client, LLMModel(digest.llm_model), lines, EVENTS_LIMIT[digest.period]
                        )
                    content = render_llm_digest(digest, events, len(lines))
            except LLMError as exc:
                logger.warning("Дайджест %s не сгенерирован: %s", digest_id, exc)
                digest.status, digest.error = DigestStatus.FAILED, str(exc)
            except Exception as exc:
                logger.exception("Дайджест %s: непредвиденная ошибка", digest_id)
                digest.status, digest.error = DigestStatus.FAILED, f"Внутренняя ошибка: {exc}"
            else:
                digest.status, digest.content = DigestStatus.DONE, content
                logger.info("Дайджест %s готов", digest_id)
            digest.finished_at = datetime.now(UTC)
    finally:
        _generation_lock.release()


def get_digest(session: Session, digest_id: int) -> Digest:
    digest = session.get(Digest, digest_id)
    if digest is None:
        raise DigestNotFoundError(f"Дайджест с id={digest_id} не найден")
    return digest


def fail_interrupted_digests(session: Session) -> int:
    """Задачи, прерванные остановкой сервиса, помечаются как неудачные"""
    result = session.execute(
        update(Digest)
        .where(Digest.status.in_([DigestStatus.PENDING, DigestStatus.RUNNING]))
        .values(
            status=DigestStatus.FAILED,
            error="Генерация прервана перезапуском сервиса",
            finished_at=datetime.now(UTC),
        )
    )
    return result.rowcount
