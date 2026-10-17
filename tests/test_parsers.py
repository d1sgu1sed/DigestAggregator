"""Парсеры RSS/Atom/Google Alerts и Hacker News API — без сети и БД."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.collector.parsers import (
    MAX_SUMMARY_LENGTH,
    ParseError,
    clean_text,
    get_api_parser,
    parse_feed,
    parse_hn_algolia,
    unwrap_google_redirect,
)
from tests.helpers import fixture_bytes

# --- clean_text / unwrap_google_redirect ------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<p>Hello <b>world</b></p>", "Hello world"),
        ("Tom &amp; Jerry", "Tom & Jerry"),
        ("  many\n\n  spaces\t", "many spaces"),
        ("non&nbsp;breaking", "non breaking"),
        (None, ""),
        ("", ""),
    ],
)
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


def test_unwrap_google_redirect():
    url = "https://www.google.com/url?rct=j&sa=t&url=https://python.org/news/315&ct=ga"
    assert unwrap_google_redirect(url) == "https://python.org/news/315"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/url?url=https://other.com",  # не Google
        "https://www.google.com/search?q=x",  # не редирект
        "https://www.google.com/url?q=x",  # нет параметра url
    ],
)
def test_unwrap_google_redirect_leaves_other_urls(url):
    assert unwrap_google_redirect(url) == url


# --- RSS --------------------------------------------------------------------


@pytest.fixture
def rss_items():
    return parse_feed(fixture_bytes("rss.xml"))


def test_rss_skips_entries_without_link_or_title(rss_items):
    assert [i.url for i in rss_items] == [
        "https://example.com/news/1?utm_source=rss",
        "https://example.com/news/2",
    ]


def test_rss_full_entry(rss_items):
    item = rss_items[0]

    assert item.external_id == "https://example.com/news/1"
    assert item.title == "First & foremost"
    assert item.summary == "Первая новость дня."
    assert item.author == "Иван Петров"
    assert item.published_at == datetime(2026, 10, 15, 6, 27, 41, tzinfo=UTC)


def test_rss_fallbacks_for_missing_fields(rss_items):
    item = rss_items[1]

    assert item.external_id == item.url  # нет guid — берём ссылку
    assert item.summary is None
    assert item.author is None
    assert datetime.now(UTC) - item.published_at < timedelta(minutes=1)  # нет даты — время сбора


def test_rss_long_summary_truncated():
    long_text = "слово " * 1000
    feed = f"""<rss version="2.0"><channel><item>
        <link>https://example.com/long</link><title>Long</title>
        <description>{long_text}</description></item></channel></rss>"""

    [item] = parse_feed(feed.encode())
    assert len(item.summary) == MAX_SUMMARY_LENGTH
    assert item.summary.endswith("…")


def test_empty_feed_returns_no_items():
    assert parse_feed(b'<rss version="2.0"><channel><title>x</title></channel></rss>') == []


@pytest.mark.parametrize("content", [b"", b"<html><body>Not found</body></html", b"\x00\x01garbage"])
def test_invalid_feed_raises(content):
    with pytest.raises(ParseError):
        parse_feed(content)


# --- Google Alerts (Atom) ---------------------------------------------------


def test_google_alerts_entry():
    [item] = parse_feed(fixture_bytes("google_alerts.xml"))

    assert item.url == "https://python.org/news/315"  # редирект Google развёрнут
    assert item.external_id == "tag:google.com,2013:googlealerts/feed:123456789"
    assert item.title == "Вышел новый релиз Python 3.15"  # без <b>
    assert item.published_at == datetime(2026, 10, 15, 7, 30, tzinfo=UTC)


# --- Hacker News API ----------------------------------------------------------


@pytest.fixture
def hn_items():
    return parse_hn_algolia(fixture_bytes("hn.json"))


def test_hn_skips_hits_without_title(hn_items):
    assert [i.external_id for i in hn_items] == ["50100015", "50100016"]


def test_hn_story(hn_items):
    item = hn_items[0]

    assert item.url == "https://example.com/rss-reader"
    assert item.title == "Show HN: A tiny RSS reader"
    assert item.author == "alice"
    assert item.summary is None
    assert item.published_at == datetime.fromtimestamp(1792046050, UTC)


def test_hn_story_without_url_links_to_discussion(hn_items):
    item = hn_items[1]

    assert item.url == "https://news.ycombinator.com/item?id=50100016"
    assert item.summary == 'Curious about your "setup".'


@pytest.mark.parametrize(
    "content",
    [b"not json", b"{}", json.dumps({"hits": None}).encode(), b"[]"],
)
def test_hn_invalid_payload_raises(content):
    with pytest.raises(ParseError):
        parse_hn_algolia(content)


def test_get_api_parser():
    assert get_api_parser("https://hn.algolia.com/api/v1/search?x=1") is parse_hn_algolia

    with pytest.raises(ParseError, match="unknown.example"):
        get_api_parser("https://unknown.example/api")
