import uuid
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import HttpClientDep, LLMClientDep, SessionDep, get_scheduler
from app.api.schemas import (
    CollectOut,
    DigestOut,
    HealthOut,
    LLMDigestOut,
    NewsDescriptionOut,
    SchedulerStatus,
    SourceCreate,
    SourceOut,
)
from app.collector import (
    CollectionInProgressError,
    InvalidFeedError,
    SourceExistsError,
    SourceNotFoundError,
    add_feed_source,
    list_sources_with_counts,
    remove_source,
    run_collection,
)
from app.config import settings
from app.db.models import Digest, DigestPeriod, DigestStatus, Source, SourceType
from app.digest import build_digest, render_text
from app.db.session import session_scope
from app.llm import (
    DigestInProgressError,
    DigestNotFoundError,
    LLMError,
    LLMModel,
    LLMUnavailableError,
    NewsNotFoundError,
    OllamaClient,
    abandon_llm_digest,
    describe_news,
    get_digest,
    run_llm_digest,
    start_llm_digest,
)
from app.scheduler import COLLECT_JOB_ID

router = APIRouter()


class DigestFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


@router.get("/health", response_model=HealthOut, tags=["service"])
def health(session: SessionDep, scheduler=Depends(get_scheduler)):
    """Состояние сервиса"""
    try:
        session.execute(text("SELECT 1"))
        database = "ok"
    except SQLAlchemyError:
        database = "unavailable"

    job = scheduler.get_job(COLLECT_JOB_ID) if scheduler else None
    body = HealthOut(
        status="ok" if database == "ok" else "degraded",
        database=database,
        scheduler=SchedulerStatus(
            enabled=scheduler is not None,
            running=bool(scheduler and scheduler.running),
            interval_minutes=int(job.trigger.interval.total_seconds() // 60) if job else None,
            next_run_at=job.next_run_time if job else None,
        ),
    )
    if database != "ok":
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=body.model_dump(mode="json"))
    return body


def _source_out(source: Source, news_count: int) -> SourceOut:
    return SourceOut(
        id=source.id,
        name=source.name,
        type=source.type,
        url=source.url,
        is_active=source.is_active,
        last_fetched_at=source.last_fetched_at,
        last_error=source.last_error,
        news_count=news_count,
    )


@router.get("/sources", response_model=list[SourceOut], tags=["sources"])
def list_sources(session: SessionDep):
    """Источники новостей, их статус и количество сохранённых новостей"""
    return [_source_out(source, count) for source, count in list_sources_with_counts(session)]


@router.post(
    "/sources",
    response_model=SourceOut,
    status_code=status.HTTP_201_CREATED,
    tags=["sources"],
    responses={
        409: {"description": "Источник с таким названием или адресом уже есть"},
        422: {"description": "Некорректные данные или по адресу нет работающей ленты"},
    },
)
def create_source(
    body: SourceCreate,
    session: SessionDep,
    client: HttpClientDep,
    verify: Annotated[
        bool, Query(description="Проверить ленту перед добавлением (скачать и разобрать)")
    ] = True,
):
    """Добавить свою RSS/Atom-ленту/Google Alerts ленту )"""
    try:
        source = add_feed_source(
            session,
            body.name,
            str(body.url),
            SourceType(body.type),
            client=client if verify else None,
        )
    except SourceExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvalidFeedError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    
    session.commit()
    return _source_out(source, news_count=0)


@router.delete(
    "/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sources"],
    responses={404: {"description": "Источник не найден"}},
)
def delete_source(source_id: int, session: SessionDep):
    """Удалить источник вместе со всеми его новостями"""
    try:
        remove_source(session, source_id)
    except SourceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/collect",
    response_model=CollectOut,
    tags=["collector"],
    responses={409: {"description": "Сбор уже выполняется"}},
)
def collect():
    """Запустить сбор новостей сейчас"""
    try:
        results = run_collection()
    except CollectionInProgressError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return CollectOut(saved=sum(r.saved for r in results), results=results)


@router.get(
    "/digest",
    response_model=DigestOut,
    tags=["digest"],
    responses={200: {"content": {"text/plain": {}}, "description": "Сводка в JSON или текстом"}},
)
def digest(
    session: SessionDep,
    period: Annotated[DigestPeriod, Query(description="Период сводки")] = DigestPeriod.DAY,
    per_source: Annotated[int, Query(ge=1, le=100, description="Новостей на источник")] = 10,
    format: Annotated[DigestFormat, Query(description="json или text")] = DigestFormat.JSON,
):
    """Сводка новостей за день или неделю, сгруппированная по источникам."""
    report = build_digest(session, period, per_source=per_source)
    if format is DigestFormat.TEXT:
        filename = f"digest-{period}-{report.end:%Y-%m-%d}.txt"
        return PlainTextResponse(
            render_text(report),
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    return report


@router.post(
    "/news/{news_id}/describe",
    response_model=NewsDescriptionOut,
    tags=["llm"],
    responses={
        404: {"description": "Новость не найдена"},
        502: {"description": "Нейросеть вернула ошибку"},
        503: {"description": "Ollama не запущена или модель не скачана"},
    },
)
def describe(
    news_id: uuid.UUID,
    session: SessionDep,
    llm: LLMClientDep,
    model: Annotated[
        LLMModel | None, Query(description="Модель; по умолчанию - LLM_MODEL из настроек")
    ] = None,
    regenerate: Annotated[
        bool, Query(description="Сгенерировать заново, даже если описание уже есть")
    ] = False,
):
    """Описание новости нейросетью"""
    try:
        result = describe_news(
            session, news_id, llm, model or settings.llm_model, regenerate=regenerate
        )
    except NewsNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except LLMUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    session.commit()

    news = result.news
    return NewsDescriptionOut(
        id=news.id,
        source=news.source.name,
        title=news.title,
        url=news.url,
        published_at=news.published_at,
        summary=news.summary,
        llm_summary=news.llm_summary,
        llm_model=news.llm_model,
        cached=result.cached,
    )


def _ollama_client() -> OllamaClient:
    return OllamaClient(settings.ollama_url, timeout=settings.llm_timeout_seconds)


@router.post(
    "/digest/llm",
    response_model=LLMDigestOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["llm"],
    responses={
        409: {"description": "Другой дайджест уже генерируется"},
        503: {"description": "Ollama не запущена или модель не скачана"},
    },
)
def create_llm_digest(
    session: SessionDep,
    llm: LLMClientDep,
    background_tasks: BackgroundTasks,
    period: Annotated[DigestPeriod, Query(description="Период дайджеста")] = DigestPeriod.DAY,
    model: Annotated[
        LLMModel | None, Query(description="Модель; по умолчанию - LLM_MODEL из настроек")
    ] = None,
):
    """Запустить генерацию дайджеста нейросетью за день или неделю"""
    model = model or settings.llm_model
    try:
        llm.ensure_model(model)  # не создаём заведомо провальную задачу
        digest = start_llm_digest(session, period, model)
    except LLMUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except DigestInProgressError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    try:
        # Задача должна быть в БД до того, как её подхватит фоновая генерация.
        session.commit()
    except Exception:
        abandon_llm_digest()
        raise
    background_tasks.add_task(
        run_llm_digest,
        digest.id,
        session_scope,
        _ollama_client,
        settings.llm_digest_per_source_per_day,
    )
    return LLMDigestOut.model_validate(digest)


@router.get("/digest/llm", response_model=list[LLMDigestOut], tags=["llm"])
def list_llm_digests(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
):
    """Последние дайджесты от нейросети"""
    digests = session.scalars(
        select(Digest).where(Digest.llm_model.is_not(None)).order_by(Digest.id.desc()).limit(limit)
    ).all()
    return [LLMDigestOut.model_validate(d) for d in digests]


@router.get(
    "/digest/llm/{digest_id}",
    response_model=LLMDigestOut,
    tags=["llm"],
    responses={
        200: {"content": {"text/plain": {}}, "description": "Статус и дайджест в JSON или текстом"},
        404: {"description": "Дайджест не найден"},
        409: {"description": "format=text, а дайджест ещё не готов"},
    },
)
def read_llm_digest(
    digest_id: int,
    session: SessionDep,
    format: Annotated[DigestFormat, Query(description="json или text")] = DigestFormat.JSON,
):
    """Статус генерации и готовый дайджест"""
    try:
        digest = get_digest(session, digest_id)
    except DigestNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if format is DigestFormat.TEXT:
        if digest.status is not DigestStatus.DONE:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"Дайджест ещё не готов (статус: {digest.status})",
            )
        filename = f"llm-digest-{digest.period}-{digest.period_end:%Y-%m-%d}.txt"
        return PlainTextResponse(
            digest.content,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    return LLMDigestOut.model_validate(digest)
