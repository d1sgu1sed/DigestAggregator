import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.collector import CollectionInProgressError, run_collection

logger = logging.getLogger(__name__)

COLLECT_JOB_ID = "collect_news"


def collect_job() -> None:
    """Сбор новостей со всех активных источников"""
    try:
        results = run_collection()
    except CollectionInProgressError:
        logger.info("Предыдущий сбор ещё не закончился")
        return
    except Exception:
        logger.exception("Сбор завершился с ошибкой")
        return

    saved = sum(r.saved for r in results)
    failed = [r.source for r in results if not r.ok]
    logger.info(
        "Собрано новостей из %d источников: новых новостей %d, с ошибкой: %s",
        len(results), saved, ", ".join(failed) or "нет",
    )


def create_scheduler(interval_minutes: int, *, run_immediately: bool = True) -> BackgroundScheduler:
    """Планировщик со сбором новостей каждые interval_minutes минут
    """
    scheduler = BackgroundScheduler(timezone=UTC)
    job_options = {}
    if run_immediately:
        # Первый сбор сразу после старта
        job_options["next_run_time"] = datetime.now(UTC)
    scheduler.add_job(
        collect_job,
        IntervalTrigger(minutes=interval_minutes, timezone=UTC),
        id=COLLECT_JOB_ID,
        max_instances=1, # не запускать новый сбор, пока идёт предыдущий
        coalesce=True, # пропущенные запуски не накапливаем
        misfire_grace_time=None, # опоздавший запуск всё равно выполнить
        **job_options,
    )
    return scheduler
