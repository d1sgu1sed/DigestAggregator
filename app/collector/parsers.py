import html
import json
import re
from calendar import timegm
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import feedparser

from app.db.repository import NewsItemData

MAX_SUMMARY_LENGTH = 2000

# Блочные теги разделяют слова, поэтому заменяются пробелом
_BLOCK_TAG_RE = re.compile(r"</?(?:p|br|div|li|ul|ol|h[1-6]|tr|td|blockquote)\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


class ParseError(Exception):
    """Ответ источника не удалось разобрать."""


Parser = Callable[[bytes], list[NewsItemData]]


def clean_text(value: str | None) -> str:
    """Убирает HTML-теги и HTML-сущности, схлопывает пробелы"""
    if not value:
        return ""
    text = _TAG_RE.sub("", _BLOCK_TAG_RE.sub(" ", value))
    text = html.unescape(text)
    return " ".join(text.split())


def _truncate(text: str, limit: int = MAX_SUMMARY_LENGTH) -> str | None:
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def unwrap_google_redirect(url: str) -> str:
    """Google Alerts отдаёт ссылки вида google.com/url?url=<настоящий адрес>."""
    parts = urlsplit(url)
    if parts.netloc.endswith("google.com") and parts.path == "/url":
        target = parse_qs(parts.query).get("url")
        if target:
            return target[0]
    return url


def _is_http_url(value: str | None) -> bool:
    if not value:
        return False
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _entry_datetime(entry) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return datetime.now(UTC)
    return datetime.fromtimestamp(timegm(parsed), UTC)


def parse_feed(content: bytes) -> list[NewsItemData]:
    feed = feedparser.parse(content)
    if not feed.entries and (feed.bozo or not feed.get("version")):
        reason = feed.get("bozo_exception") or "формат не распознан"
        raise ParseError(f"Некорректная лента: {reason}")

    items = []
    for entry in feed.entries:
        link = entry.get("link")
        title = clean_text(entry.get("title"))
        if not _is_http_url(link) or not title:
            continue
        url = unwrap_google_redirect(link)
        items.append(
            NewsItemData(
                external_id=entry.get("id") or url,
                url=url,
                title=title,
                published_at=_entry_datetime(entry),
                summary=_truncate(clean_text(entry.get("summary"))),
                author=clean_text(entry.get("author")) or None,
            )
        )
    return items


HN_ITEM_URL = "https://news.ycombinator.com/item?id={}"


def parse_hn_algolia(content: bytes) -> list[NewsItemData]:
    try:
        hits = json.loads(content)["hits"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError(f"Некорректный ответ HN API: {exc!r}") from exc
    if not isinstance(hits, list):
        raise ParseError(f"Некорректный ответ HN API: hits={hits!r}")

    items = []
    for hit in hits:
        object_id = hit.get("objectID")
        title = clean_text(hit.get("title"))
        timestamp = hit.get("created_at_i")
        if not object_id or not title or timestamp is None:
            continue
        items.append(
            NewsItemData(
                external_id=str(object_id),
                url=hit.get("url") or HN_ITEM_URL.format(object_id),
                title=title,
                published_at=datetime.fromtimestamp(int(timestamp), UTC),
                summary=_truncate(clean_text(hit.get("story_text"))),
                author=hit.get("author") or None,
            )
        )
    return items


# Парсеры API выбираются по домену, т.к. у каждого API свой формат.
API_PARSERS: dict[str, Parser] = {
    "hn.algolia.com": parse_hn_algolia,
}


def get_api_parser(url: str) -> Parser:
    host = urlsplit(url).netloc.lower()
    try:
        return API_PARSERS[host]
    except KeyError:
        raise ParseError(f"Нет парсера для API {host!r}") from None
