import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from app.llm.models import LLMModel

# Модели подобраны так, чтобы помещаться в 4 ГБ видеопамяти
NUM_CTX = 2048


class LLMError(Exception):
    """Нейросеть не смогла ответить"""


class LLMUnavailableError(LLMError):
    """Ollama не запущена, не отвечает или модель не скачана"""


class OllamaClient:
    """Минимальный клиент HTTP API Ollama (/api/chat)"""

    def __init__(self, base_url: str, timeout: float = 120, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @retry(
        # Повторяем только сбой подключения 
        retry=retry_if_exception_type(httpx.ConnectError),
        stop=stop_after_attempt(2),
        wait=wait_fixed(1),
        reraise=True,
    )
    def _post_chat(self, body: dict) -> httpx.Response:
        return self._http.post("/api/chat", json=body)

    def ensure_model(self, model: LLMModel) -> None:
        """Проверяет, что Ollama запущена и модель скачана."""
        try:
            response = self._http.get("/api/tags", timeout=5)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                f"Ollama недоступна по адресу {self._http.base_url}: запустите `systemctl start ollama`"
            ) from exc
        names = {m["name"] for m in response.json().get("models", [])}
        if str(model) not in names and f"{model}:latest" not in names:
            raise LLMUnavailableError(f"Модель {model} не скачана: выполните `ollama pull {model}`")

    def chat(self, model: LLMModel, system: str, user: str, *, max_tokens: int = 300) -> str:
        body = {
            "model": str(model),
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"num_ctx": NUM_CTX, "num_predict": max_tokens, "temperature": 0},
        }
        try:
            response = self._post_chat(body)
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"Ollama недоступна по адресу {self._http.base_url}: запустите `systemctl start ollama`"
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError(f"Модель {model} не ответила вовремя") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Ошибка запроса к Ollama: {exc}") from exc

        if response.status_code == 404:
            raise LLMUnavailableError(f"Модель {model} не скачана: выполните `ollama pull {model}`")
        if response.is_error:
            raise LLMError(f"Ollama вернула HTTP {response.status_code}: {response.text[:200]}")

        text = response.json().get("message", {}).get("content", "")
        if not text.strip():
            raise LLMError(f"Модель {model} вернула пустой ответ")
        return text
