from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

from app.db.models import DigestPeriod, SourceType


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


class FeedType(StrEnum):
    RSS = SourceType.RSS.value
    GOOGLE_ALERTS = SourceType.GOOGLE_ALERTS.value


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
