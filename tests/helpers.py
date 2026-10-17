from collections.abc import Callable
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """httpx-клиент, который вместо сети вызывает handler (получаем примеры из fixtures)"""
    return httpx.Client(transport=httpx.MockTransport(handler))


def respond_with(*steps) -> tuple[Callable, list[httpx.Request]]:
    """Обработчик, отдающий ответы/исключения по очереди"""
    calls: list[httpx.Request] = []
    queue = list(steps)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("simulated", request=request)
        if isinstance(step, int):
            return httpx.Response(step)
        return httpx.Response(200, content=step)

    return handler, calls
