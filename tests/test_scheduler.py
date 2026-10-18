"""Планировщик и единая точка запуска сбора."""

import logging
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from tenacity import wait_none

from app import scheduler as scheduler_module
from app.collector import CollectionInProgressError, CollectResult, run_collection
from app.scheduler import COLLECT_JOB_ID, collect_job, create_scheduler
from tests.helpers import fixture_bytes, mock_client, respond_with

# --- create_scheduler ---------------------------------------------------------


@pytest.fixture
def started():
    """Запускает планировщик на паузе (задачи не выполняются) и гасит после теста."""
    schedulers = []

    def start(*args, **kwargs):
        s = create_scheduler(*args, **kwargs)
        s.start(paused=True)
        schedulers.append(s)
        return s

    yield start
    for s in schedulers:
        s.shutdown(wait=False)


def test_job_configuration(started):
    job = started(15).get_job(COLLECT_JOB_ID)

    assert job.func is collect_job
    assert job.trigger.interval == timedelta(minutes=15)
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.misfire_grace_time is None


def test_first_run_immediately(started):
    job = started(30).get_job(COLLECT_JOB_ID)

    assert job.next_run_time - datetime.now(UTC) < timedelta(seconds=5)


def test_first_run_after_interval(started):
    job = started(30, run_immediately=False).get_job(COLLECT_JOB_ID)

    assert job.next_run_time - datetime.now(UTC) > timedelta(minutes=29)


def test_scheduler_uses_utc(started):
    job = started(30).get_job(COLLECT_JOB_ID)

    assert job.next_run_time.utcoffset() == timedelta(0)


# --- collect_job ----------------------------------------------------------------


def test_collect_job_logs_summary(monkeypatch, caplog):
    results = [CollectResult("A", fetched=5, saved=3), CollectResult("B", error="timeout")]
    monkeypatch.setattr(scheduler_module, "run_collection", lambda: results)

    with caplog.at_level(logging.INFO, logger="app.scheduler"):
        collect_job()

    assert "новых новостей 3, с ошибкой: B" in caplog.text


def test_collect_job_skips_when_busy(monkeypatch, caplog):
    def busy():
        raise CollectionInProgressError

    monkeypatch.setattr(scheduler_module, "run_collection", busy)

    with caplog.at_level(logging.INFO, logger="app.scheduler"):
        collect_job()  # не бросает

    assert "Предыдущий сбор" in caplog.text


def test_collect_job_swallows_unexpected_error(monkeypatch, caplog):
    def broken():
        raise RuntimeError("db is down")

    monkeypatch.setattr(scheduler_module, "run_collection", broken)

    collect_job()

    assert "db is down" in caplog.text
    assert caplog.records[-1].levelno == logging.ERROR


@pytest.fixture
def session_factory(session):
    @contextmanager
    def factory():
        yield session

    return factory


def test_run_collection_collects(session_factory, session, source):
    handler, _ = respond_with(fixture_bytes("rss.xml"))

    [result] = run_collection(session_factory, mock_client(handler), wait=wait_none())

    assert (result.source, result.saved) == ("Example", 2)


def test_run_collection_rejects_parallel_run(session_factory, session, source):
    """Пока идёт сбор, второй запуск сразу получает CollectionInProgressError"""
    entered, release = threading.Event(), threading.Event()

    def slow_handler(request):
        entered.set()
        release.wait(5)
        return respond_with(fixture_bytes("rss.xml"))[0](request)

    worker = threading.Thread(
        target=lambda: run_collection(session_factory, mock_client(slow_handler), wait=wait_none())
    )
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(CollectionInProgressError):
            run_collection(session_factory, mock_client(slow_handler), wait=wait_none())
    finally:
        release.set()
        worker.join(5)


def test_run_collection_releases_lock_after_error(session):
    @contextmanager
    def broken_factory():
        raise RuntimeError("no db")
        yield 

    with pytest.raises(RuntimeError):
        run_collection(broken_factory)

    # Блокировка снята
    @contextmanager
    def factory():
        yield session

    assert run_collection(factory, mock_client(respond_with(b"")[0])) == []
