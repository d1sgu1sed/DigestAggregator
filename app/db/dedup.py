import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Параметры трекинга, которые не меняют саму новость.
TRACKING_PARAMS = {"fbclid", "gclid", "yclid", "ref", "from", "at_medium", "at_campaign"}


def normalize_url(url: str) -> str:
    """Приводит URL к стандартному виду, чтобы одна новость давала один ключ."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parts.scheme.lower() or "https",
            parts.netloc.lower().removeprefix("www."),
            path,
            urlencode(sorted(query)),
            "",  # фрагмент (#...) отбрасываем
        )
    )


def url_hash(url: str) -> str:
    """SHA-256 от нормализованного URL"""
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()
