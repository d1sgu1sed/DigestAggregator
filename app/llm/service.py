import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session, joinedload

from app.db.models import NewsItem
from app.llm.client import OllamaClient
from app.llm.models import LLMModel
from app.llm.prompts import DESCRIBE_SYSTEM, DESCRIBE_USER, clean_answer

logger = logging.getLogger(__name__)


class NewsNotFoundError(Exception):
    """Новости с таким id нет"""


@dataclass(frozen=True, slots=True)
class DescribeResult:
    news: NewsItem
    cached: bool


def describe_news(
    session: Session,
    news_id: uuid.UUID,
    client: OllamaClient,
    model: LLMModel,
    *,
    regenerate: bool = False,
) -> DescribeResult:
    """Описание новости нейросетью"""
    news = session.get(NewsItem, news_id, options=[joinedload(NewsItem.source)])
    if news is None:
        raise NewsNotFoundError(f"Новость с id={news_id} не найдена")
    if news.llm_summary and not regenerate:
        return DescribeResult(news, cached=True)

    prompt = DESCRIBE_USER.format(
        source=news.source.name, title=news.title, summary=news.summary or "-"
    )
    news.llm_summary = clean_answer(client.chat(model, DESCRIBE_SYSTEM, prompt))
    news.llm_model = str(model)
    session.flush()
    logger.info("Описание новости %s сгенерировано моделью %s", news_id, model)
    return DescribeResult(news, cached=False)
