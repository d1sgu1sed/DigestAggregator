import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

from app.collector.sources import FeedType
from app.db.models import DigestPeriod, DigestStatus, SourceType


class SchedulerStatus(BaseModel):
    enabled: bool
    running: bool
    interval_minutes: int | None = None
    next_run_at: datetime | None = None


class HealthOut(BaseModel):
    status: str
    database: str
    scheduler: SchedulerStatus


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: SourceType
    url: str
    is_active: bool
    last_fetched_at: datetime | None
    last_error: str | None
    news_count: int


class SourceCreate(BaseModel):
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
        Field(examples=["Хабр"]),
    ]
    url: Annotated[HttpUrl, Field(examples=["https://habr.com/ru/rss/articles/"])]
    type: FeedType = FeedType.RSS


class CollectResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source: str
    ok: bool
    fetched: int
    saved: int
    error: str | None


class CollectOut(BaseModel):
    saved: int
    results: list[CollectResultOut]


class DigestItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    url: str
    published_at: datetime
    summary: str | None


class DigestSectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source: str
    total: int
    items: list[DigestItemOut]


class DigestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period: DigestPeriod
    start: datetime
    end: datetime
    total: int
    sections: list[DigestSectionOut]


class NewsDescriptionOut(BaseModel):
    id: uuid.UUID
    source: str
    title: str
    url: str
    published_at: datetime
    summary: str | None
    llm_summary: str
    llm_model: str | None
    cached: bool


class LLMDigestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: DigestStatus
    period: DigestPeriod
    start: datetime = Field(validation_alias="period_start")
    end: datetime = Field(validation_alias="period_end")
    llm_model: str | None
    news_count: int
    content: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None
