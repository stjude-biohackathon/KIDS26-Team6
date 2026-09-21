"""Shared, restrained Rich presentation for human-facing CLI output."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import json
import sys
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)
error_console = Console(stderr=True, highlight=False)


def emit_json(payload: Any) -> None:
    """Write machine-readable JSON without Rich styling or status text."""

    sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")


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

    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold")
    for label, value in rows:
        rendered = value if isinstance(value, Text) else plain_text(value)
        grid.add_row(f"{label}:", rendered)
    console.print(Group(Text(title, style=style), grid))


def mapping_summary(title: str, payload: Mapping[str, object]) -> None:
    """Render a flat mapping with readable labels."""

    summary(
        title,
        ((key.replace("_", " ").title(), value) for key, value in payload.items()),
    )


def command_list(title: str, commands: Sequence[str]) -> None:
    """Print shell commands without interpreting their contents as markup."""

    console.print(Text(title, style="bold"))
    for command in commands:
        console.print(Text.assemble("  ", plain_text(command, style="cyan")))


def notice(label: str, message: object, *, style: str, stderr: bool = False) -> None:
    """Print one compact labelled message."""

    destination = error_console if stderr else console
    destination.print(
        Text.assemble((label, style), " ", plain_text(message)),
        soft_wrap=True,
    )


def info(message: object, *, stderr: bool = False) -> None:
    notice("Info", message, style="bold cyan", stderr=stderr)


def success(message: object, *, stderr: bool = False) -> None:
    notice("OK", message, style="bold green", stderr=stderr)


def warning(message: object) -> None:
    """Print a warning to stderr without interpreting the message as markup."""

    notice("Warning", message, style="bold yellow", stderr=True)


def error(message: object) -> None:
    """Print an error to stderr without interpreting the message as markup."""

    notice("Error", message, style="bold red", stderr=True)
