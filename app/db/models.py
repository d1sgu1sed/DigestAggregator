import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CHAR,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class SourceType(StrEnum):
    RSS = "rss"
    API = "api"
    GOOGLE_ALERTS = "google_alerts"


class DigestPeriod(StrEnum):
    DAY = "day"
    WEEK = "week"


def _pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    # Храним в БД значения ("rss"), а не имена членов ("RSS").
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class Source(Base):
    """Источник новостей: RSS-лента, API или Google Alerts"""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[SourceType] = mapped_column(_pg_enum(SourceType, "source_type"))
    url: Mapped[str] = mapped_column(Text, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # Состояние последнего сбора
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    news: Mapped[list["NewsItem"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<Source id={self.id} name={self.name!r}>"


class NewsItem(Base):
    """Отдельная новость

    Защита от дублей на уровне БД:
      * url_hash - SHA-256 нормализованного URL;
      * (source_id, external_id) - guid/id записи внутри источника.
    """

    __tablename__ = "news_items"
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(1024))
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)

    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(512))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Описание, сгенерированное LLM (заполняется отдельно).
    llm_summary: Mapped[str | None] = mapped_column(Text)

    source: Mapped[Source] = relationship(back_populates="news")

    def __repr__(self) -> str:
        return f"<NewsItem id={self.id} title={self.title[:40]!r}>"


class Digest(Base):
    """Сохранённая сводка за период (день/неделя)."""

    __tablename__ = "digests"
    __table_args__ = (Index("ix_digests_period_start", "period", "period_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[DigestPeriod] = mapped_column(_pg_enum(DigestPeriod, "digest_period"))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    news_count: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Digest id={self.id} period={self.period} start={self.period_start}>"
