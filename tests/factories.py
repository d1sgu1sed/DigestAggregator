from datetime import UTC, datetime

from app.db.repository import NewsItemData


def make_item(n: int, published_at: datetime | None = None, **overrides) -> NewsItemData:
    data = {
        "external_id": f"guid-{n}",
        "url": f"https://example.com/news/{n}",
        "title": f"News {n}",
        "published_at": published_at or datetime.now(UTC),
    }
    data.update(overrides)
    return NewsItemData(**data)
