from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import DigestPeriod
from app.db.repository import get_news_for_period, period_bounds

PERIOD_TITLES = {DigestPeriod.DAY: "день", DigestPeriod.WEEK: "неделю"}
TEXT_SUMMARY_LENGTH = 300


@dataclass(frozen=True, slots=True)
class DigestItem:
    title: str
    url: str
    source: str
    published_at: datetime
    summary: str | None


@dataclass(frozen=True, slots=True)
class DigestSection:
    source: str
    total: int  # всего новостей источника за период
    items: list[DigestItem]  # самые свежие, не больше per_source


@dataclass(frozen=True, slots=True)
class DigestReport:
    period: DigestPeriod
    start: datetime
    end: datetime
    total: int
    sections: list[DigestSection]


def build_digest(
    session: Session,
    period: DigestPeriod,
    *,
    per_source: int = 10,
    now: datetime | None = None,
) -> DigestReport:
    """Сводка за период: новости сгруппированы по источникам, свежие сверху.

    Возвращает простые dataclass-объекты, не привязанные к сессии БД.
    """
    start, end = period_bounds(period, now)
    news = get_news_for_period(session, start, end)  # уже отсортированы: новые сначала

    grouped: dict[str, list[DigestItem]] = {}
    for n in news:
        grouped.setdefault(n.source.name, []).append(
            DigestItem(
                title=n.title,
                url=n.url,
                source=n.source.name,
                published_at=n.published_at,
                summary=n.llm_summary or n.summary,
            )
        )

    # Источники с наибольшим числом новостей первыми
    sections = [
        DigestSection(source=name, total=len(items), items=items[:per_source])
        for name, items in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    return DigestReport(period=period, start=start, end=end, total=len(news), sections=sections)


def _shorten(text: str, limit: int = TEXT_SUMMARY_LENGTH) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_text(report: DigestReport) -> str:
    """Сводка в виде простого текста (для вывода или сохранения в .txt)."""
    fmt = "%d.%m.%Y %H:%M"
    lines = [
        f"Сводка новостей за {PERIOD_TITLES[report.period]}",
        f"Период: {report.start:{fmt}} - {report.end:{fmt}} UTC",
        f"Всего новостей: {report.total}",
    ]
    if not report.sections:
        lines += ["", "За этот период новостей нет."]

    for section in report.sections:
        shown = len(section.items)
        suffix = f", показаны {shown} последних" if shown < section.total else ""
        lines += ["", f"== {section.source} ({section.total}{suffix}) =="]
        for item in section.items:
            lines.append(f"• [{item.published_at:%d.%m %H:%M}] {item.title}")
            if item.summary:
                lines.append(f"  {_shorten(item.summary)}")
            lines.append(f"  {item.url}")
    return "\n".join(lines) + "\n"
