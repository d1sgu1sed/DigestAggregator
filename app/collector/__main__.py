"""Разовый сбор новостей: python -m app.collector"""

import logging

from app.collector import collect_all, seed_sources
from app.db.session import session_scope


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with session_scope() as session:
        seed_sources(session)
        session.commit()
        results = collect_all(session)

    for r in results:
        status = f"получено {r.fetched}, новых {r.saved}" if r.ok else f"ОШИБКА: {r.error}"
        print(f"{r.source}: {status}")


if __name__ == "__main__":
    main()
