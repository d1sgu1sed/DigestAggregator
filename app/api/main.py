"""HTTP API агрегатора: uvicorn app.api.main:app"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.collector import seed_sources
from app.config import settings
from app.db.session import session_scope
from app.llm import fail_interrupted_digests
from app.scheduler import create_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def seed_default_sources() -> None:
    with session_scope() as session:
        seed_sources(session)
        if interrupted := fail_interrupted_digests(session):
            logger.warning("Прервано генераций дайджеста при прошлой остановке: %d", interrupted)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Действия при запуске и окончании работы приложения"""
    seed_default_sources()

    app.state.scheduler = None
    if settings.scheduler_enabled:
        scheduler = create_scheduler(settings.collect_interval_minutes)
        scheduler.start()
        app.state.scheduler = scheduler
        logger.info("Планировщик запущен: сбор каждые %d мин.", settings.collect_interval_minutes)
    try:
        yield
    finally:
        if app.state.scheduler is not None:
            app.state.scheduler.shutdown(wait=False)
            logger.info("Планировщик остановлен")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Digest Aggregator",
        description="Сбор новостей из RSS/API",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
