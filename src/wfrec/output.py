"""Shared Rich presentation helpers for human-facing ``wfrec`` output."""

from __future__ import annotations

from collections.abc import Iterable

from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)
error_console = Console(stderr=True, highlight=False)


def plain_text(value: object, style: str = "") -> Text:
    """Render dynamic values literally so paths cannot become Rich markup."""

    return Text(str(value), style=style)


def status_text(available: bool) -> Text:
    """Represent availability with both text and color."""

    if available:
        return Text("OK", style="bold green")
    return Text("--", style="yellow")


def data_table(*columns: str) -> Table:
    """Return the compact, border-light table used throughout the CLI."""

    table = Table(
        box=box.SIMPLE_HEAVY,
        expand=False,
        header_style="bold dim",
        pad_edge=False,
        show_edge=False,
    )
    for column in columns:
        table.add_column(column, overflow="fold")
    return table


def status_table(*columns: str) -> Table:
    """Return a compact table with the shared status column."""

    table = data_table("Status", *columns)
    table.columns[0].no_wrap = True
    return table


def summary(
    title: str,
    rows: Iterable[tuple[str, object]],
    *,
    style: str = "bold cyan",
) -> None:
    """Print a heading followed by a small key-value grid."""

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold")
    for label, value in rows:
        rendered = value if isinstance(value, Text) else plain_text(value)
        grid.add_row(label, rendered)
    console.print(Group(Text(title, style=style), grid))


def notice(label: str, message: object, *, style: str) -> None:
    """Print one compact labelled message to standard output."""

    console.print(Text.assemble((label, style), " ", plain_text(message)))


def success(message: object) -> None:
    notice("OK", message, style="bold green")


def warning(message: object) -> None:
    """Print a warning to stderr without interpreting the message as markup."""

    error_console.print(
        Text.assemble(("Warning", "bold yellow"), " ", plain_text(message))
    )


def error(message: object) -> None:
    """Print an error to stderr without interpreting the message as markup."""

    error_console.print(
        Text.assemble(("Error", "bold red"), " ", plain_text(message))
    )
