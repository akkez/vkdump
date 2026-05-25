import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any
import typer
from rich.console import Console
from rich.json import JSON
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table

from ..core.db import apply_migrations, connection
from ..core.orchestrator import execute
from ..core.progress import Cancelled
from ..core.runs import list_runs
from ..tasks.registry import get_task
from .progress import CliProgress


def _short_error(err: str | None) -> str:
    """Take the last non-empty line of a traceback (the exception summary).

    `runs.error` typically stores `traceback.format_exc()`, whose last line
    is the actual `TypeError: ...` / `ValueError: ...` summary. The first
    line is always the boilerplate "Traceback (most recent call last):",
    which carries no signal in a table column.
    """
    if not err:
        return ""
    lines = [ln for ln in err.splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:80]


app = typer.Typer(help="vkdump — parse and enrich VK chat dumps.", no_args_is_help=True)
db_app = typer.Typer(help="Database management.", no_args_is_help=True)
runs_app = typer.Typer(help="Inspect past task runs.", no_args_is_help=True)
logs_app = typer.Typer(help="Inspect parse error logs.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(runs_app, name="runs")
app.add_typer(logs_app, name="logs")

console = Console()


def _run(task_name: str, params: dict) -> None:
    task = get_task(task_name)
    apply_migrations()
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as rp:
        tid = rp.add_task(task.title, total=1)
        reporter = CliProgress(rp, tid)
        try:
            outcome = execute(task, params, reporter)
        except KeyboardInterrupt:
            console.print("[yellow]cancelled[/yellow]")
            raise typer.Exit(130)

    if outcome.status == "success":
        console.print(f"[green]ok[/green] run #{outcome.run_id}")
        if outcome.result is not None:
            console.print(_render_result(outcome.result))
    elif outcome.status == "cancelled":
        console.print(f"[yellow]cancelled[/yellow] run #{outcome.run_id}")
        raise typer.Exit(130)
    else:
        console.print(f"[red]failed[/red] run #{outcome.run_id}: {outcome.error}")
        raise typer.Exit(1)


def _json_default(value: Any) -> Any:
    """Coerce non-JSON-serialisable values to something printable."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _render_result(result: Any) -> JSON | str:
    """Render a task result as pretty, colourised JSON. Falls back to repr
    when the value can't be coerced (e.g. contains a circular reference)."""
    try:
        text = json.dumps(
            result, indent=2, ensure_ascii=False, default=_json_default, sort_keys=False
        )
        return JSON(text)
    except (TypeError, ValueError):
        return repr(result)


@app.command("parse-dump")
def parse_dump_cmd(
    source: Annotated[
        Path,
        typer.Argument(
            help="A VK dump: ZIP archive, archive root folder, messages/ folder, single chat folder, or one HTML file.",
        ),
    ],
    source_tz: Annotated[
        str,
        typer.Option(
            "--source-tz",
            help="IANA timezone the dump's timestamps are in. Default 'UTC'.",
        ),
    ] = "UTC",
) -> None:
    """Parse a VK dump into SQLite (chats, users, messages, attachments)."""
    _run("parse-dump", {"source": source, "source_timezone": source_tz})


@app.command("enrich")
def enrich_cmd() -> None:
    """Enrich stored messages (download media, expand forwards, resolve users)."""
    _run("enrich", {})


@app.command("stats")
def stats_cmd() -> None:
    """Compute statistics over stored messages."""
    _run("stats", {})


@db_app.command("migrate")
def db_migrate_cmd() -> None:
    """Apply pending SQL migrations."""
    applied = apply_migrations()
    if applied:
        for name in applied:
            console.print(f"applied {name}")
    else:
        console.print("nothing to apply")


@logs_app.command("list")
def errors_list_cmd(
    limit: Annotated[int, typer.Option(help="Max rows to show.")] = 20,
    chat: Annotated[str | None, typer.Option(help="Filter by chat source_folder.")] = None,
) -> None:
    """List recent parse errors (one row per failed message item)."""
    sql = (
        "SELECT pe.id, pe.created_at, pe.source_folder, pe.source_file, pe.error "
        "FROM parse_errors pe "
    )
    args: list = []
    if chat:
        sql += "WHERE pe.source_folder = ? "
        args.append(chat)
    sql += "ORDER BY pe.id DESC LIMIT ?"
    args.append(limit)
    with connection() as conn:
        rows = conn.execute(sql, args).fetchall()
    table = Table(show_header=True, header_style="bold")
    for col in ("id", "created_at", "chat", "file", "error"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["id"]),
            str(r["created_at"]),
            r["source_folder"] or "",
            r["source_file"] or "",
            (r["error"] or "")[:80],
        )
    console.print(table)


@logs_app.command("show")
def errors_show_cmd(error_id: Annotated[int, typer.Argument(help="parse_errors.id")]) -> None:
    """Show the raw HTML and traceback for one parse error."""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM parse_errors WHERE id = ?", (error_id,)
        ).fetchone()
    if not row:
        console.print(f"[red]no parse_errors row with id={error_id}[/red]")
        raise typer.Exit(1)
    console.print(f"[bold]id:[/bold] {row['id']}")
    console.print(f"[bold]chat:[/bold] {row['source_folder']}  [bold]file:[/bold] {row['source_file']}")
    console.print(f"[bold]created_at:[/bold] {row['created_at']}")
    console.print(f"[bold]error:[/bold] {row['error']}")
    console.print()
    console.print("[bold]traceback:[/bold]")
    console.print(row["traceback"] or "")
    console.print()
    console.print("[bold]raw_html:[/bold]")
    console.print(row["raw_html"] or "")


@runs_app.command("list")
def runs_list_cmd(limit: Annotated[int, typer.Option(help="Max rows to show.")] = 20) -> None:
    """Show recent task runs."""
    rows = list_runs(limit=limit)
    table = Table(show_header=True, header_style="bold")
    for col in ("id", "task", "status", "started_at", "finished_at", "error"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r.id),
            r.task,
            r.status,
            r.started_at.isoformat(timespec="seconds"),
            r.finished_at.isoformat(timespec="seconds") if r.finished_at else "",
            _short_error(r.error),
        )
    console.print(table)
