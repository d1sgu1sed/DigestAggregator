import httpx
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from tenacity.wait import wait_base

USER_AGENT = "DigestAggregator/0.1"
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class FetchError(Exception):
    """Источник недоступен: сеть, таймаут или ошибочный HTTP-ответ."""


def create_client(timeout: float | httpx.Timeout = DEFAULT_TIMEOUT) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _is_retryable(exc: BaseException) -> bool:
    """Повторяем только временные сбои: сеть, таймауты, 5xx и 429."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 or status == 429
    return False


def _describe(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def fetch(
    client: httpx.Client,
    url: str,
    *,
    attempts: int = 3,
    wait: wait_base = wait_exponential(multiplier=1, max=10),
) -> bytes:
    """Скачивает URL с повторами при временных сбоях.

    Возвращает тело ответа или бросает FetchError с описанием причины.
    """
    retrying = Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait,
        retry=retry_if_exception(_is_retryable),
        reraise=False,
    )
    try:
        for attempt in retrying:
            with attempt:
                response = client.get(url)
                response.raise_for_status()
                return response.content
    except RetryError as exc:  # попытки исчерпаны
        last = exc.last_attempt.exception()
        raise FetchError(f"{_describe(last)} (попыток: {attempts})") from last
    except httpx.HTTPError as exc:  # перманентная ошибка, например 404
        raise FetchError(_describe(exc)) from exc
    raise AssertionError("unreachable")
