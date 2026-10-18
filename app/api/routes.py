from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import SessionDep, get_scheduler
from app.api.schemas import (
    CollectOut,
    DigestOut,
    HealthOut,
    SchedulerStatus,
    SourceOut,
)
from app.collector import CollectionInProgressError, run_collection
from app.db.models import DigestPeriod, NewsItem, Source
from app.digest import build_digest, render_text
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


@router.get("/sources", response_model=list[SourceOut], tags=["sources"])
def list_sources(session: SessionDep):
    """Источники новостей, их статус и количество сохранённых новостей"""
    news_count = (
        select(func.count(NewsItem.id))
        .where(NewsItem.source_id == Source.id)
        .correlate(Source)
        .scalar_subquery()
    )
    rows = session.execute(select(Source, news_count).order_by(Source.id)).all()
    return [
        SourceOut(
            id=source.id,
            name=source.name,
            type=source.type,
            url=source.url,
            is_active=source.is_active,
            last_fetched_at=source.last_fetched_at,
            last_error=source.last_error,
            news_count=count,
        )
        for source, count in rows
    ]


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
