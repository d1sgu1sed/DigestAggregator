from collections.abc import Iterator
from typing import Annotated

import httpx
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.collector.http import create_client
from app.config import settings
from app.llm import OllamaClient
from app.db.session import SessionLocal


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_http_client() -> Iterator[httpx.Client]:
    with create_client() as client:
        yield client


def get_llm_client() -> Iterator[OllamaClient]:
    with OllamaClient(settings.ollama_url, timeout=settings.llm_timeout_seconds) as client:
        yield client


def get_scheduler(request: Request):
    return getattr(request.app.state, "scheduler", None)


SessionDep = Annotated[Session, Depends(get_session)]
HttpClientDep = Annotated[httpx.Client, Depends(get_http_client)]
LLMClientDep = Annotated[OllamaClient, Depends(get_llm_client)]
