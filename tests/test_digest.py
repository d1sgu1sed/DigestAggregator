"""Сервис сводки: выборка за период, группировка, текстовый вид."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.db.models import DigestPeriod, NewsItem
from app.db.repository import save_news
from app.digest import build_digest, render_text
from app.digest.service import TEXT_SUMMARY_LENGTH, DigestItem, DigestReport, DigestSection
from tests.factories import make_item

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def hours_ago(h: float) -> datetime:
    return NOW - timedelta(hours=h)


def test_groups_by_source_biggest_first(session, source, other_source):
    save_news(session, source.id, [make_item(1, hours_ago(1))])
    save_news(session, other_source.id, [make_item(2, hours_ago(2)), make_item(3, hours_ago(3))])

    report = build_digest(session, DigestPeriod.DAY, now=NOW)

    assert report.total == 3
    assert [(s.source, s.total) for s in report.sections] == [("Other", 2), ("Example", 1)]


def test_items_newest_first(session, source):
    save_news(session, source.id, [make_item(1, hours_ago(5)), make_item(2, hours_ago(1))])

    [section] = build_digest(session, DigestPeriod.DAY, now=NOW).sections

    assert [i.title for i in section.items] == ["News 2", "News 1"]


def test_per_source_limits_items_but_not_total(session, source):
    save_news(session, source.id, [make_item(i, hours_ago(i)) for i in range(1, 6)])

    report = build_digest(session, DigestPeriod.DAY, per_source=2, now=NOW)

    [section] = report.sections
    assert section.total == 5
    assert [i.title for i in section.items] == ["News 1", "News 2"]
    assert report.total == 5


def test_period_filters_news(session, source):
    save_news(session, source.id, [make_item(1, hours_ago(2)), make_item(2, hours_ago(24 * 3))])

    assert build_digest(session, DigestPeriod.DAY, now=NOW).total == 1
    assert build_digest(session, DigestPeriod.WEEK, now=NOW).total == 2


def test_empty_period(session, source):
    report = build_digest(session, DigestPeriod.DAY, now=NOW)

    assert report.total == 0
    assert report.sections == []
    assert (report.start, report.end) == (NOW - timedelta(days=1), NOW)


def test_llm_summary_preferred(session, source):
    save_news(session, source.id, [make_item(1, hours_ago(1), summary="plain")])
    session.execute(update(NewsItem).values(llm_summary="beautiful"))

    [section] = build_digest(session, DigestPeriod.DAY, now=NOW).sections
    assert section.items[0].summary == "beautiful"


def test_report_detached_from_session(session, source):
    save_news(session, source.id, [make_item(1, hours_ago(1))])
    report = build_digest(session, DigestPeriod.DAY, now=NOW)
    session.expunge_all()

    assert report.sections[0].items[0].source == "Example"  # обычные данные, не ORM


# --- render_text ------------------------------------------------------------


def make_report(sections, period=DigestPeriod.DAY) -> DigestReport:
    return DigestReport(
        period=period,
        start=NOW - timedelta(days=1),
        end=NOW,
        total=sum(s.total for s in sections),
        sections=sections,
    )


def make_digest_item(n: int, summary: str | None = None) -> DigestItem:
    return DigestItem(
        title=f"News {n}",
        url=f"https://example.com/{n}",
        source="Example",
        published_at=datetime(2026, 10, 15, 9, 30, tzinfo=UTC),
        summary=summary,
    )


def test_render_text_full():
    report = make_report([DigestSection("Example", 1, [make_digest_item(1, "Short summary")])])

    assert render_text(report) == (
        "Сводка новостей за день\n"
        "Период: 14.10.2026 12:00 - 15.10.2026 12:00 UTC\n"
        "Всего новостей: 1\n"
        "\n"
        "== Example (1) ==\n"
        "• [15.10 09:30] News 1\n"
        "  Short summary\n"
        "  https://example.com/1\n"
    )


def test_render_text_mentions_truncated_section():
    report = make_report([DigestSection("Example", 7, [make_digest_item(1)])], DigestPeriod.WEEK)
    text = render_text(report)

    assert text.startswith("Сводка новостей за неделю")
    assert "== Example (7, показаны 1 последних) ==" in text


def test_render_text_shortens_long_summary():
    text = render_text(make_report([DigestSection("Example", 1, [make_digest_item(1, "x" * 1000)])]))

    summary_line = text.splitlines()[-2].strip()
    assert len(summary_line) == TEXT_SUMMARY_LENGTH
    assert summary_line.endswith("…")


def test_render_text_empty():
    assert "За этот период новостей нет." in render_text(make_report([]))
