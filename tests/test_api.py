"""HTTP API: эндпоинты на тестовой БД, без сети и без реального планировщика."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api import main as api_main
from app.api import routes
from app.api.deps import get_session
from app.collector import CollectionInProgressError, CollectResult
from app.config import settings
from app.db.repository import save_news
from app.scheduler import create_scheduler
from tests.factories import make_item


@pytest.fixture
def app(session):
    app = api_main.create_app()
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest.fixture
def client(app):
    # Без `with`: lifespan (источники по умолчанию + планировщик) не запускается.
    return TestClient(app)


def recent(hours: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


def test_health_without_scheduler(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "scheduler": {"enabled": False, "running": False, "interval_minutes": None, "next_run_at": None},
    }


def test_health_with_scheduler(app, client):
    scheduler = create_scheduler(15, run_immediately=False)
    scheduler.start(paused=True)
    app.state.scheduler = scheduler
    try:
        body = client.get("/health").json()["scheduler"]
    finally:
        scheduler.shutdown(wait=False)

    assert body["enabled"] is True
    assert body["running"] is True
    assert body["interval_minutes"] == 15
    assert body["next_run_at"] is not None


def test_health_database_down(app, client):
    class BrokenSession:
        def execute(self, *args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    app.dependency_overrides[get_session] = lambda: BrokenSession()
    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["detail"]["database"] == "unavailable"



def test_sources_with_news_count(client, session, source, other_source):
    save_news(session, source.id, [make_item(1), make_item(2)])
    source.last_error = "timeout"
    session.flush()

    body = client.get("/sources").json()

    assert [(s["name"], s["news_count"]) for s in body] == [("Example", 2), ("Other", 0)]
    assert body[0]["type"] == "rss"
    assert body[0]["last_error"] == "timeout"


def test_sources_empty(client):
    assert client.get("/sources").json() == []



def test_digest_json(client, session, source):
    save_news(session, source.id, [make_item(1, recent(1)), make_item(2, recent(30))])

    response = client.get("/digest", params={"period": "day"})

    assert response.status_code == 200
    body = response.json()
    assert body["period"] == "day"
    assert body["total"] == 1
    [section] = body["sections"]
    assert section["source"] == "Example"
    assert section["items"][0]["title"] == "News 1"
    assert set(section["items"][0]) == {"title", "url", "published_at", "summary"}


def test_digest_defaults_to_day(client, session, source):
    save_news(session, source.id, [make_item(1, recent(30))])

    assert client.get("/digest").json()["total"] == 0


def test_digest_week_and_per_source(client, session, source):
    save_news(session, source.id, [make_item(i, recent(24 * i)) for i in range(1, 5)])

    body = client.get("/digest", params={"period": "week", "per_source": 2}).json()

    assert body["total"] == 4
    assert body["sections"][0]["total"] == 4
    assert len(body["sections"][0]["items"]) == 2


def test_digest_text(client, session, source):
    save_news(session, source.id, [make_item(1, recent(1))])

    response = client.get("/digest", params={"format": "text"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'filename="digest-day-' in response.headers["content-disposition"]
    assert response.text.startswith("Сводка новостей за день")
    assert "News 1" in response.text


@pytest.mark.parametrize(
    "params",
    [{"period": "month"}, {"per_source": 0}, {"per_source": 101}, {"format": "xml"}],
)
def test_digest_invalid_params(client, params):
    assert client.get("/digest", params=params).status_code == 422


def test_collect(monkeypatch, client):
    results = [CollectResult("A", fetched=5, saved=3), CollectResult("B", error="ConnectTimeout")]
    monkeypatch.setattr(routes, "run_collection", lambda: results)

    response = client.post("/collect")

    assert response.status_code == 200
    assert response.json() == {
        "saved": 3,
        "results": [
            {"source": "A", "ok": True, "fetched": 5, "saved": 3, "error": None},
            {"source": "B", "ok": False, "fetched": 0, "saved": 0, "error": "ConnectTimeout"},
        ],
    }


def test_collect_conflict_when_running(monkeypatch, client):
    def busy():
        raise CollectionInProgressError("Сбор новостей уже выполняется")

    monkeypatch.setattr(routes, "run_collection", busy)

    response = client.post("/collect")

    assert response.status_code == 409
    assert response.json()["detail"] == "Сбор новостей уже выполняется"


def test_collect_is_post_only(client):
    assert client.get("/collect").status_code == 405


@pytest.fixture
def lifespan_env(monkeypatch):
    seeded = []
    monkeypatch.setattr(api_main, "seed_default_sources", lambda: seeded.append(True))
    # Без немедленного сбора, чтобы тест не ходил в сеть.
    monkeypatch.setattr(
        api_main, "create_scheduler", lambda minutes: create_scheduler(minutes, run_immediately=False)
    )
    return seeded


def test_lifespan_starts_and_stops_scheduler(monkeypatch, app, lifespan_env):
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "collect_interval_minutes", 45)

    with TestClient(app) as client:
        scheduler = app.state.scheduler
        assert scheduler.running
        assert client.get("/health").json()["scheduler"]["interval_minutes"] == 45

    assert not scheduler.running
    assert lifespan_env == [True]  # источники по умолчанию добавлены при старте


def test_lifespan_without_scheduler(monkeypatch, app, lifespan_env):
    monkeypatch.setattr(settings, "scheduler_enabled", False)

    with TestClient(app) as client:
        assert app.state.scheduler is None
        assert client.get("/health").json()["scheduler"]["enabled"] is False

    assert lifespan_env == [True]


def test_openapi_docs_available(client):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/health", "/sources", "/digest", "/collect"} <= set(paths)
