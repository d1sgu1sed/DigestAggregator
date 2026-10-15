import pytest

from app.db.dedup import normalize_url, url_hash

CANONICAL = "https://example.com/news/1"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/news/1",
        "https://www.example.com/news/1",
        "https://EXAMPLE.com/news/1",
        "HTTPS://example.com/news/1",
        "https://example.com/news/1/",
        "https://example.com/news/1#comments",
        "https://example.com/news/1?utm_source=rss&utm_medium=feed",
        "https://example.com/news/1?fbclid=abc&gclid=def&yclid=1",
        "  https://example.com/news/1  ",
    ],
)
def test_url_variants_normalize_to_same_value(url):
    assert normalize_url(url) == CANONICAL
    assert url_hash(url) == url_hash(CANONICAL)


def test_meaningful_query_params_are_kept():
    assert normalize_url("https://example.com/news?id=42") == "https://example.com/news?id=42"
    assert url_hash("https://example.com/news?id=1") != url_hash("https://example.com/news?id=2")


def test_query_params_are_sorted():
    assert normalize_url("https://example.com/?b=2&a=1") == normalize_url(
        "https://example.com/?a=1&b=2"
    )


def test_tracking_params_removed_but_others_kept():
    assert (
        normalize_url("https://example.com/n?utm_campaign=x&page=2&ref=tw")
        == "https://example.com/n?page=2"
    )


def test_path_case_is_preserved():
    # Путь регистрозависим
    assert url_hash("https://example.com/News/A") != url_hash("https://example.com/news/a")


def test_empty_path_becomes_root():
    assert normalize_url("https://example.com") == "https://example.com/"


def test_different_articles_have_different_hashes():
    assert url_hash("https://example.com/news/1") != url_hash("https://example.com/news/2")


def test_hash_is_sha256_hex():
    h = url_hash(CANONICAL)
    assert len(h) == 64
    int(h, 16)  # только hex-символы
