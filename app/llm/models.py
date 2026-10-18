from enum import StrEnum


class LLMModel(StrEnum):
    """Модели Ollama, которые можно выбрать для описаний новостей"""

    GEMMA3_4B = "gemma3:4b"  # медленнее, лучше перевод
    QWEN3_4B = "qwen3:4b-instruct"  # быстрее, но ошибается в переводе
