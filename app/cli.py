import logging
import threading
import uuid
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from app.collector import (
    CollectionInProgressError,
    FeedType,
    InvalidFeedError,
    SourceExistsError,
    SourceNotFoundError,
    add_feed_source,
    list_sources_with_counts,
    remove_source,
    run_collection,
    seed_sources,
)
from app.collector.http import create_client
from app.config import settings
from app.db.models import DigestPeriod, DigestStatus, SourceType
from app.db.session import session_scope
from app.digest import build_digest, render_text
from app.llm import (
    DigestInProgressError,
    LLMError,
    LLMModel,
    NewsNotFoundError,
    OllamaClient,
    abandon_llm_digest,
    describe_news,
    get_digest,
    run_llm_digest,
    start_llm_digest,
)
from app.scheduler import COLLECT_JOB_ID, create_scheduler

app = typer.Typer(
    help="Агрегатор новостей: сбор из RSS/API и сводка за день или неделю",
    no_args_is_help=True,
)
sources_app = typer.Typer(help="Источники новостей", no_args_is_help=True)
llm_app = typer.Typer(help="Описания новостей и дайджесты", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(llm_app, name="llm")

console = Console()


def fail(message: str, code: int = 1) -> typer.Exit:
    """Печатает ошибку в stderr"""
    typer.secho(f"Ошибка: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def write_or_print(text: str, output: Path | None) -> None:
    if output is None:
        typer.echo(text, nl=False)
    else:
        output.write_text(text, encoding="utf-8")
        typer.secho(f"Сохранено в {output}", fg=typer.colors.GREEN)


def ollama_client() -> OllamaClient:
    return OllamaClient(settings.ollama_url, timeout=settings.llm_timeout_seconds)


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Подробный лог")] = False,
) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command()
def collect() -> None:
    """Собрать новости со всех активных источников"""
    with session_scope() as session:
        seed_sources(session)
    try:
        results = run_collection()
    except CollectionInProgressError as exc:
        raise fail(str(exc))

    for r in results:
        if r.ok:
            typer.echo(f"{r.source}: получено {r.fetched}, новых {r.saved}")
        else:
            typer.secho(f"{r.source}: ОШИБКА - {r.error}", fg=typer.colors.RED)
    typer.secho(f"Всего новых новостей: {sum(r.saved for r in results)}", bold=True)
    if results and not any(r.ok for r in results):
        raise typer.Exit(1)


@app.command()
def scheduler(
    interval: Annotated[
        int | None,
        typer.Option(min=1, help="Интервал сбора в минутах"),
    ] = None,
) -> None:
    """Собирать новости по расписанию"""
    minutes = interval or settings.collect_interval_minutes
    with session_scope() as session:
        seed_sources(session)

    logging.getLogger("app").setLevel(logging.INFO)
    sched = create_scheduler(minutes)
    sched.start()
    typer.echo(f"Сбор каждые {minutes} мин, остановка: Ctrl+C")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        job = sched.get_job(COLLECT_JOB_ID)
        sched.shutdown(wait=False)
        typer.echo(
            "\nПланировщик остановлен"
            + (f" Следующий сбор был бы в {job.next_run_time:%H:%M} UTC." if job else "")
        )


@app.command()
def digest(
    period: Annotated[DigestPeriod, typer.Option("--period", "-p", help="Период")] = DigestPeriod.DAY,
    per_source: Annotated[int, typer.Option(min=1, max=100, help="Новостей на источник")] = 10,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Сохранить в текстовый файл", dir_okay=False)
    ] = None,
) -> None:
    """Сводка новостей за день или неделю без нейросети"""
    with session_scope() as session:
        report = build_digest(session, period, per_source=per_source)
    write_or_print(render_text(report), output)


@sources_app.command("list")
def sources_list() -> None:
    """Источники, их статус и число сохранённых новостей"""
    with session_scope() as session:
        rows = list_sources_with_counts(session)
    if not rows:
        typer.echo("Источников нет. Добавьте: python -m app sources add NAME URL")
        return

    table = Table("id", "Название", "Тип", "Новостей", "Последний сбор (UTC)", "Ошибка")
    for source, count in rows:
        table.add_row(
            str(source.id),
            source.name if source.is_active else f"[dim]{source.name} (отключён)[/dim]",
            str(source.type),
            str(count),
            f"{source.last_fetched_at:%d.%m %H:%M}" if source.last_fetched_at else "—",
            f"[red]{source.last_error}[/red]" if source.last_error else "",
        )
    console.print(table)


@sources_app.command("add")
def sources_add(
    name: Annotated[str, typer.Argument(help="Название источника")],
    url: Annotated[str, typer.Argument(help="Адрес RSS-ленты")],
    type: Annotated[FeedType, typer.Option("--type", "-t", help="Тип ленты")] = FeedType.RSS,
    verify: Annotated[
        bool, typer.Option("--verify/--no-verify", help="Проверить ленту перед добавлением")
    ] = True,
) -> None:
    """Добавить свою RSS-ленту (или ленту Google Alerts)"""
    name = name.strip()
    if not name:
        raise fail("название не может быть пустым")
    if not url.startswith(("http://", "https://")):
        raise fail("адрес должен начинаться с http:// или https://")

    with session_scope() as session, create_client() as client:
        try:
            source = add_feed_source(
                session, name, url, SourceType(type), client=client if verify else None
            )
        except (SourceExistsError, InvalidFeedError) as exc:
            raise fail(str(exc))
        source_id = source.id
    typer.secho(f'Добавлен источник "{name}" (id={source_id})', fg=typer.colors.GREEN)
    typer.echo("Новости появятся при следующем сборе: python -m app collect")


@sources_app.command("remove")
def sources_remove(
    source_id: Annotated[int, typer.Argument(help="id источника")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Не спрашивать подтверждение")] = False,
) -> None:
    """Удалить источник вместе со всеми его новостями"""
    with session_scope() as session:
        rows = {s.id: (s, count) for s, count in list_sources_with_counts(session)}
        if source_id not in rows:
            raise fail(f"источник с id={source_id} не найден")
        source, count = rows[source_id]
        name = source.name
        if not yes:
            typer.confirm(
                f'Удалить "{source.name}" и {count} его новостей?', abort=True
            )
        try:
            remove_source(session, source_id)
        except SourceNotFoundError as exc:
            raise fail(str(exc))
    typer.secho(f'Источник "{name}" удалён', fg=typer.colors.GREEN)


ModelOption = Annotated[
    LLMModel | None,
    typer.Option("--model", "-m"),
]


@llm_app.command("describe")
def llm_describe(
    news_id: Annotated[uuid.UUID, typer.Argument(help="id новости")],
    model: ModelOption = None,
    regenerate: Annotated[
        bool, typer.Option("--regenerate", "-r", help="Сгенерировать заново")
    ] = False,
) -> None:
    """Описание новости нейросетью"""
    model = model or settings.llm_model
    with session_scope() as session, ollama_client() as client:
        try:
            with console.status(f"{model} пишет описание..."):
                result = describe_news(session, news_id, client, model, regenerate=regenerate)
        except NewsNotFoundError as exc:
            raise fail(str(exc))
        except LLMError as exc:
            raise fail(str(exc))
        news = result.news
        typer.secho(news.title, bold=True)
        typer.echo(news.url)
        typer.echo()
        typer.echo(news.llm_summary)
        note = "сохранённое описание" if result.cached else "сгенерировано"
        typer.secho(f"\n[{news.llm_model}, {note}]", dim=True)


@llm_app.command("digest")
def llm_digest(
    period: Annotated[DigestPeriod, typer.Option("--period", "-p", help="Период")] = DigestPeriod.DAY,
    model: ModelOption = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Сохранить в текстовый файл", dir_okay=False)
    ] = None,
) -> None:
    """Дайджест нейросетью за день или неделю"""
    model = model or settings.llm_model
    with ollama_client() as client:
        try:
            client.ensure_model(model)
        except LLMError as exc:
            raise fail(str(exc))

    with session_scope() as session:
        try:
            digest_id = start_llm_digest(session, period, model).id
        except DigestInProgressError as exc:
            raise fail(str(exc))
        try:
            session.commit()
        except Exception:
            abandon_llm_digest()
            raise

    with console.status(f"{model} готовит дайджест за {period}..."):
        run_llm_digest(digest_id, session_scope, ollama_client, settings.llm_digest_per_source_per_day)

    with session_scope() as session:
        digest_ = get_digest(session, digest_id)
        if digest_.status is not DigestStatus.DONE:
            raise fail(f"дайджест не сгенерирован: {digest_.error}")
        content = digest_.content
    write_or_print(content, output)


if __name__ == "__main__":
    app()
