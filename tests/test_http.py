"""Загрузка с повторами при сбоях сети (tenacity) — на подменённом транспорте."""

import httpx
import pytest
from tenacity import wait_none
from tenacity.wait import wait_base

from app.collector.http import USER_AGENT, FetchError, create_client, fetch
from tests.helpers import mock_client, respond_with

URL = "https://example.com/rss"


def do_fetch(*steps, attempts=3):
    handler, calls = respond_with(*steps)
    try:
        return fetch(mock_client(handler), URL, attempts=attempts, wait=wait_none()), calls
    except FetchError as exc:
        exc.calls = calls
        raise


def test_success_first_try():
    body, calls = do_fetch(b"<rss/>")

    assert body == b"<rss/>"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, 500, 502, 503, 429],
)
def test_retries_transient_failure_then_succeeds(failure):
    body, calls = do_fetch(failure, failure, b"ok")

    assert body == b"ok"
    assert len(calls) == 3


def test_gives_up_after_all_attempts():
    with pytest.raises(FetchError, match=r"ConnectTimeout.*попыток: 3") as exc_info:
        do_fetch(httpx.ConnectTimeout)

    assert len(exc_info.value.calls) == 3


def test_gives_up_on_persistent_server_error():
    with pytest.raises(FetchError, match=r"HTTP 503.*попыток: 2"):
        do_fetch(503, attempts=2)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_client_errors_are_not_retried(status):
    with pytest.raises(FetchError, match=f"HTTP {status}") as exc_info:
        do_fetch(status)

    assert len(exc_info.value.calls) == 1


def test_fetch_error_keeps_original_cause():
    with pytest.raises(FetchError) as exc_info:
        do_fetch(httpx.ConnectError)

    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


def test_waits_between_attempts():
    """По умолчанию между попытками есть пауза (экспоненциальная)."""
    waits = []
    handler, _ = respond_with(503, b"ok")

    class RecordingWait(wait_base):
        def __call__(self, retry_state):
            waits.append(retry_state.attempt_number)
            return 0

    fetch(mock_client(handler), URL, wait=RecordingWait())
    assert waits == [1]


def test_client_defaults():
    with create_client() as client:
        assert client.headers["User-Agent"] == USER_AGENT
        assert client.follow_redirects is True
        assert client.timeout.connect == 5.0
        assert client.timeout.read == 10.0
