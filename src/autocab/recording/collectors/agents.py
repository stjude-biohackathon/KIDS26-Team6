"""Agent/chat transcript collector: Codex, Claude Code, Copilot Chat, Cursor.

Codex uses DevSQL's normalized tables. The direct adapters read undocumented,
version-drifting on-disk formats. The module's organizing principle is failure
isolation: a schema change emits ``agent.adapter.failed`` while the other
adapters keep working.

There is also always a manual path -- ``wfrec attach-transcript`` -- which works
regardless of format drift and is what guarantees a demo can proceed.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from ..devsql import DevSQLClient
from ..events import AGENT_ADAPTER_FAILED, AGENT_MESSAGE, AGENT_TOOL_COMPLETED, Event
from ..redaction import shared as shared_redactor
from .base import Collector, CollectorStatus, Degraded
from .shell import (
    active_interval_start,
    datetime_to_iso,
    parse_devsql_timestamp,
)

if TYPE_CHECKING:
    from ..session import Session

MAX_TEXT_CHARS = 8000
DEVSQL_CODEX_COLUMNS = (
    "thread_id",
    "record_index",
    "timestamp",
    "role",
    "text",
    "parent_thread_id",
    "parent_record_index",
    "source_kind",
    "agent_path",
    "agent_role",
    "originator",
    "cwd",
    "git_branch",
)
DEVSQL_CODEX_TOOL_COLUMNS = (
    "thread_id",
    "call_id",
    "call_record_index",
    "output_record_index",
    "called_at",
    "completed_at",
    "tool_name",
    "arguments_json",
    "cmd",
    "output_text",
    "exit_code",
    "cwd",
    "source_path",
    "parent_thread_id",
    "source_kind",
    "agent_path",
    "agent_role",
    "originator",
    "git_branch",
)
DevSQLDiscoverer = Callable[[], DevSQLClient]


def _iso(value: Any) -> str | None:
    """Normalize the several timestamp shapes these tools use."""

    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        # Copilot uses epoch milliseconds; guard against seconds too.
        seconds = value / 1000 if value > 1e11 else value
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    text = str(value)
    if text.endswith("Z") and "T" in text:
        return text
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        moment = moment.astimezone(timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    except ValueError:
        return None


def _flatten_content(content: Any) -> str:
    """Reduce the nested content shapes of these formats to plain text."""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "tool_use":
                    name = item.get("name", "tool")
                    parts.append(
                        f"[tool_use {name}] {json.dumps(item.get('input'), default=str)[:2000]}"
                    )
                elif item.get("type") == "tool_result":
                    parts.append(f"[tool_result] {_flatten_content(item.get('content'))[:2000]}")
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return _flatten_content(content.get("content") or content.get("text"))
    return str(content)


def app_support_dirs(app: str) -> list[Path]:
    """Candidate per-user config roots for a VS Code-family app."""

    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / app]
    if sys.platform == "win32":  # pragma: no cover - Windows
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / app] if appdata else []
    base = os.environ.get("XDG_CONFIG_HOME")
    roots = [Path(base) / app] if base else []
    roots.append(home / ".config" / app)
    return roots


@dataclass(slots=True)
class Turn:
    """One normalized conversation turn."""

    tool: str
    role: str
    text: str
    ts: str | None = None
    cwd: str | None = None
    key: str = ""
    meta: dict[str, Any] | None = None


@dataclass(slots=True)
class ToolExecution:
    """One completed tool call from an agent transcript."""

    agent: str
    tool_name: str
    arguments: str = ""
    output: str = ""
    command: str = ""
    exit_code: int | None = None
    called_at: str | None = None
    completed_at: str | None = None
    cwd: str | None = None
    key: str = ""
    meta: dict[str, Any] | None = None


class BaseAdapter:
    """One agent tool's on-disk transcript format."""

    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def seek_to_end(self) -> None:
        """Skip anything already on disk, so only new turns are recorded."""

    def turns(self, since: float) -> Iterator[Turn]:
        raise NotImplementedError

    def activities(self, since: float) -> Iterator[Turn | ToolExecution]:
        """Yield normalized records from this adapter."""

        yield from self.turns(since)


class DevSQLCodexAdapter(BaseAdapter):
    """Codex messages exposed through DevSQL's normalized tables."""

    name = "devsql-codex"

    def __init__(
        self,
        client: DevSQLClient,
        *,
        since: str,
        existing_events: Iterable[Event] = (),
    ) -> None:
        interval_start = parse_devsql_timestamp(since)
        if interval_start is None:
            raise ValueError("DevSQL interval start must be a timezone-aware timestamp.")
        self.client = client
        self.interval_start = interval_start
        self._query_start = interval_start
        self._restore_query_start(existing_events)

    def available(self) -> bool:
        """Validate the joined schema without reading transcript content."""

        self.client.query(f"{self._select()} LIMIT 0")
        return True

    def turns(self, since: float) -> Iterator[Turn]:
        """Return canonical Codex turns from the active recording interval."""

        query_second = self._query_start.strftime("%Y-%m-%dT%H:%M:%S")
        sql = (
            f"{self._select()} "
            "WHERE message.is_canonical = 1 "
            "AND message.role IN ('user', 'assistant') "
            f"AND substr(message.timestamp, 1, 19) >= '{query_second}' "
            "ORDER BY message.timestamp, message.thread_id, "
            "message.record_index"
        )
        rows = self.client.query(sql, required_columns=DEVSQL_CODEX_COLUMNS)

        latest = self._query_start
        for row in rows:
            occurred_at = parse_devsql_timestamp(row["timestamp"])
            if occurred_at is None or occurred_at < self._query_start:
                continue
            latest = max(latest, occurred_at)
            turn = self._convert(row, occurred_at)
            if turn is not None:
                yield turn
        self._query_start = latest

    @staticmethod
    def _select() -> str:
        """Build the shared content-free and interval query projection."""

        return (
            "SELECT "
            "message.thread_id AS thread_id, "
            "message.record_index AS record_index, "
            "message.timestamp AS timestamp, "
            "message.role AS role, "
            "message.text AS text, "
            "thread.parent_thread_id AS parent_thread_id, "
            "thread.parent_record_index AS parent_record_index, "
            "thread.source_kind AS source_kind, "
            "thread.agent_path AS agent_path, "
            "thread.agent_role AS agent_role, "
            "thread.originator AS originator, "
            "thread.cwd AS cwd, "
            "thread.git_branch AS git_branch "
            "FROM codex_messages AS message "
            "JOIN codex_threads AS thread "
            "ON thread.thread_id = message.thread_id"
        )

    def _restore_query_start(self, events: Iterable[Event]) -> None:
        """Resume near the last captured turn after a daemon restart."""

        for event in events:
            if event.type != AGENT_MESSAGE:
                continue
            if event.payload.get("provider") != "devsql":
                continue
            capture_id = event.payload.get("capture_id")
            if not isinstance(capture_id, str) or not capture_id.startswith("codex:"):
                continue
            occurred_at = parse_devsql_timestamp(event.ts)
            if occurred_at is not None and occurred_at >= self.interval_start:
                self._query_start = max(self._query_start, occurred_at)

    @staticmethod
    def _convert(row: dict[str, Any], occurred_at: datetime) -> Turn | None:
        """Normalize one joined row and reject incomplete identities."""

        thread_id = row["thread_id"]
        record_index = row["record_index"]
        role = row["role"]
        text = row["text"]
        if not isinstance(thread_id, str) or not thread_id:
            return None
        if isinstance(record_index, bool):
            return None
        try:
            normalized_index = int(record_index)
        except (TypeError, ValueError):
            return None
        if role not in {"user", "assistant"}:
            return None
        if not isinstance(text, str) or not text.strip():
            return None

        capture_id = f"codex:{thread_id}:{normalized_index}"
        return Turn(
            tool="codex",
            role=role,
            text=text,
            ts=datetime_to_iso(occurred_at),
            cwd=row["cwd"] if isinstance(row["cwd"], str) else None,
            key=capture_id,
            meta={
                "provider": "devsql",
                "session_id": thread_id,
                "source_id": str(normalized_index),
                "parent_session_id": row["parent_thread_id"],
                "parent_record_index": row["parent_record_index"],
                "source_kind": row["source_kind"],
                "agent_id": row["agent_path"],
                "agent_role": row["agent_role"],
                "originator": row["originator"],
                "git_branch": row["git_branch"],
            },
        )


class DevSQLCodexToolAdapter(BaseAdapter):
    """Completed Codex tool calls exposed through DevSQL."""

    name = "devsql-codex-tools"

    def __init__(
        self,
        client: DevSQLClient,
        *,
        since: str,
        existing_events: Iterable[Event] = (),
    ) -> None:
        interval_start = parse_devsql_timestamp(since)
        if interval_start is None:
            raise ValueError("DevSQL interval start must be a timezone-aware timestamp.")
        self.client = client
        self.interval_start = interval_start
        self._query_start = interval_start
        self._restore_query_start(existing_events)

    def available(self) -> bool:
        """Validate the joined tool schema without reading activity content."""

        self.client.query(f"{self._select()} LIMIT 0")
        return True

    def turns(self, since: float) -> Iterator[Turn]:
        """Tool adapters do not emit conversation turns."""

        return iter(())

    def activities(self, since: float) -> Iterator[Turn | ToolExecution]:
        """Return completed tool calls from the active recording interval."""

        query_second = self._query_start.strftime("%Y-%m-%dT%H:%M:%S")
        sql = (
            f"{self._select()} "
            "WHERE execution.completed_at IS NOT NULL "
            f"AND substr(execution.completed_at, 1, 19) >= '{query_second}' "
            "ORDER BY execution.completed_at, execution.thread_id, "
            "execution.call_record_index"
        )
        rows = self.client.query(sql, required_columns=DEVSQL_CODEX_TOOL_COLUMNS)

        latest = self._query_start
        for row in rows:
            completed_at = parse_devsql_timestamp(row["completed_at"])
            if completed_at is None or completed_at < self._query_start:
                continue
            latest = max(latest, completed_at)
            execution = self._convert(row, completed_at)
            if execution is not None:
                yield execution
        self._query_start = latest

    @staticmethod
    def _select() -> str:
        """Build the tool execution and thread metadata projection."""

        return (
            "SELECT "
            "execution.thread_id AS thread_id, "
            "execution.call_id AS call_id, "
            "execution.call_record_index AS call_record_index, "
            "execution.output_record_index AS output_record_index, "
            "execution.called_at AS called_at, "
            "execution.completed_at AS completed_at, "
            "execution.tool_name AS tool_name, "
            "execution.arguments_json AS arguments_json, "
            "execution.cmd AS cmd, "
            "execution.output_text AS output_text, "
            "execution.exit_code AS exit_code, "
            "execution.cwd AS cwd, "
            "execution.source_path AS source_path, "
            "thread.parent_thread_id AS parent_thread_id, "
            "thread.source_kind AS source_kind, "
            "thread.agent_path AS agent_path, "
            "thread.agent_role AS agent_role, "
            "thread.originator AS originator, "
            "thread.git_branch AS git_branch "
            "FROM codex_tool_executions AS execution "
            "JOIN codex_threads AS thread "
            "ON thread.thread_id = execution.thread_id"
        )

    def _restore_query_start(self, events: Iterable[Event]) -> None:
        """Resume near the last captured execution after a daemon restart."""

        for event in events:
            if event.type != AGENT_TOOL_COMPLETED:
                continue
            if event.payload.get("provider") != "devsql":
                continue
            capture_id = event.payload.get("capture_id")
            if not isinstance(capture_id, str) or not capture_id.startswith("codex-tool:"):
                continue
            occurred_at = parse_devsql_timestamp(event.ts)
            if occurred_at is not None and occurred_at >= self.interval_start:
                self._query_start = max(self._query_start, occurred_at)

    @staticmethod
    def _convert(row: dict[str, Any], completed_at: datetime) -> ToolExecution | None:
        """Normalize one joined tool execution row."""

        thread_id = row["thread_id"]
        call_id = row["call_id"]
        tool_name = row["tool_name"]
        if not all(isinstance(value, str) and value for value in (thread_id, call_id, tool_name)):
            return None

        exit_code: int | None = None
        raw_exit_code = row["exit_code"]
        if raw_exit_code is not None and not isinstance(raw_exit_code, bool):
            with contextlib.suppress(TypeError, ValueError):
                exit_code = int(raw_exit_code)

        arguments = row["arguments_json"]
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, default=str) if arguments is not None else ""
        output = row["output_text"] if isinstance(row["output_text"], str) else ""
        command = row["cmd"] if isinstance(row["cmd"], str) else ""
        return ToolExecution(
            agent="codex",
            tool_name=tool_name,
            arguments=arguments,
            output=output,
            command=command,
            exit_code=exit_code,
            called_at=_iso(row["called_at"]),
            completed_at=datetime_to_iso(completed_at),
            cwd=row["cwd"] if isinstance(row["cwd"], str) else None,
            key=f"codex-tool:{thread_id}:{call_id}",
            meta={
                "provider": "devsql",
                "session_id": thread_id,
                "call_id": call_id,
                "call_record_index": row["call_record_index"],
                "output_record_index": row["output_record_index"],
                "parent_session_id": row["parent_thread_id"],
                "source_kind": row["source_kind"],
                "agent_id": row["agent_path"],
                "agent_role": row["agent_role"],
                "originator": row["originator"],
                "git_branch": row["git_branch"],
            },
        )


class ClaudeCodeAdapter(BaseAdapter):
    """Claude Code's newline-delimited JSONL transcripts.

    The best-structured of the three by a wide margin: append-only JSONL with a
    ``version`` field on every record, so it can be tailed by byte offset and
    gated on version. Unknown record types are passed through opaquely rather
    than raising, because the observed set includes undocumented types.
    """

    name = "claude-code"
    KNOWN_ROLES = {"user", "assistant"}

    def __init__(self) -> None:
        self.root = Path.home() / ".claude" / "projects"
        self._offsets: dict[Path, int] = {}

    def available(self) -> bool:
        return self.root.is_dir()

    def seek_to_end(self) -> None:
        """Start tailing from the current end of every transcript file.

        Without this, starting a session ingests the whole of any transcript
        touched in the last hour -- hundreds of turns of unrelated history that
        happened before the analyst pressed record.
        """

        for path in self.root.rglob("*.jsonl"):
            try:
                self._offsets[path] = path.stat().st_size
            except OSError:
                continue

    def _files(self, since: float) -> list[Path]:
        files: list[Path] = []
        for path in self.root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime >= since - 3600:
                    files.append(path)
            except OSError:
                continue
        return sorted(files)

    def turns(self, since: float) -> Iterator[Turn]:
        for path in self._files(since):
            start = self._offsets.get(path, 0)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < start:
                start = 0
            if size == start:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(start)
                    chunk = handle.read()
            except OSError:
                continue
            cut = chunk.rfind("\n")
            if cut == -1:
                continue
            self._offsets[path] = start + len(chunk[: cut + 1].encode("utf-8"))

            for line in chunk[: cut + 1].split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("isMeta"):
                    continue
                kind = record.get("type")
                if kind not in self.KNOWN_ROLES:
                    continue
                message = record.get("message") or {}
                text = _flatten_content(message.get("content"))
                if not text.strip():
                    continue
                yield Turn(
                    tool=self.name,
                    role=str(kind),
                    text=text,
                    ts=_iso(record.get("timestamp")),
                    cwd=record.get("cwd"),
                    key=str(record.get("uuid") or f"{path.name}:{record.get('timestamp')}"),
                    meta={
                        "session_id": record.get("sessionId"),
                        "version": record.get("version"),
                        "git_branch": record.get("gitBranch"),
                        # Subagent turns are tagged so a downstream skill can
                        # tell a delegated sub-task from the main thread.
                        "sidechain": bool(record.get("isSidechain")),
                    },
                )


class CopilotChatAdapter(BaseAdapter):
    """GitHub Copilot Chat sessions in VS Code's workspaceStorage.

    Rewritten wholesale on every turn rather than appended, so this cannot be
    tailed by offset -- the file is re-read and diffed by request index.
    """

    name = "copilot"

    def __init__(self) -> None:
        self.roots = [
            root / "User" / "workspaceStorage"
            for app in ("Code", "Code - Insiders", "VSCodium")
            for root in app_support_dirs(app)
        ]
        self._seen: set[str] = set()

    def available(self) -> bool:
        return any(root.is_dir() for root in self.roots)

    def seek_to_end(self) -> None:
        """Mark every currently-present turn as already seen."""

        for turn in list(self.turns(0.0)):
            self._seen.add(turn.key)

    def turns(self, since: float) -> Iterator[Turn]:
        for root in self.roots:
            if not root.is_dir():
                continue
            for workspace in root.iterdir():
                if not workspace.is_dir():
                    continue
                folder = self._workspace_folder(workspace)
                sessions = workspace / "chatSessions"
                if not sessions.is_dir():
                    continue
                for path in sessions.glob("*.json"):
                    try:
                        if path.stat().st_mtime < since - 3600:
                            continue
                        payload = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    for index, request in enumerate(payload.get("requests") or []):
                        yield from self._request_turns(path, index, request, folder)

    @staticmethod
    def _workspace_folder(workspace: Path) -> str | None:
        """Map the opaque workspace hash back to a real folder path."""

        meta = workspace / "workspace.json"
        if not meta.exists():
            return None
        try:
            return json.loads(meta.read_text(encoding="utf-8")).get("folder")
        except (OSError, json.JSONDecodeError):
            return None

    def _request_turns(
        self, path: Path, index: int, request: dict, folder: str | None
    ) -> Iterator[Turn]:
        ts = _iso(request.get("timestamp"))
        prompt = _flatten_content((request.get("message") or {}).get("text")) or _flatten_content(
            request.get("message")
        )
        if prompt.strip():
            key = f"{path.name}:{index}:user"
            if key not in self._seen:
                self._seen.add(key)
                yield Turn(self.name, "user", prompt, ts, folder, key)
        answer = _flatten_content(request.get("response"))
        if answer.strip():
            key = f"{path.name}:{index}:assistant"
            if key not in self._seen:
                self._seen.add(key)
                yield Turn(self.name, "assistant", answer, ts, folder, key)


class CursorAdapter(BaseAdapter):
    """Cursor's SQLite chat storage.

    The most fragile adapter: keys and schema change per release and the
    database is held open by Cursor in WAL mode. Opened strictly read-only with
    ``query_only`` and a short ``busy_timeout`` so the recorder can never lock
    the editor's database out from under the analyst.
    """

    name = "cursor"

    def __init__(self) -> None:
        self.databases = [
            root / "User" / "globalStorage" / "state.vscdb" for root in app_support_dirs("Cursor")
        ]
        self._seen: set[str] = set()

    def available(self) -> bool:
        return any(path.exists() for path in self.databases)

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        connection.execute("PRAGMA query_only=1")
        connection.execute("PRAGMA busy_timeout=2000")
        return connection

    def seek_to_end(self) -> None:
        """Mark every currently-present bubble as already seen."""

        for turn in list(self.turns(0.0)):
            self._seen.add(turn.key)

    def turns(self, since: float) -> Iterator[Turn]:
        for path in self.databases:
            if not path.exists():
                continue
            try:
                connection = self._connect(path)
            except sqlite3.Error:
                continue
            try:
                yield from self._read_bubbles(connection, path)
            except sqlite3.Error:
                # "database is locked" is expected while Cursor writes; skip
                # this poll rather than treating it as an error.
                continue
            finally:
                connection.close()

    def _read_bubbles(self, connection: sqlite3.Connection, path: Path) -> Iterator[Turn]:
        cursor = connection.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        )
        for key, value in cursor.fetchall():
            if key in self._seen:
                continue
            self._seen.add(key)
            try:
                bubble = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            text = _flatten_content(bubble.get("text") or bubble.get("richText"))
            if not text.strip():
                continue
            # Cursor encodes author as an int: 1 = user, 2 = assistant.
            role = "user" if bubble.get("type") == 1 else "assistant"
            yield Turn(
                tool=self.name,
                role=role,
                text=text,
                ts=_iso(bubble.get("createdAt") or bubble.get("timestamp")),
                cwd=None,
                key=str(key),
                meta={"source_db": str(path)},
            )


COMMAND_TOOLS = {"exec", "exec_command"}
EDIT_TOOLS = {"apply_patch", "set_value", "type_text", "_perform_editing_operations"}
CONTINUATION_TOOLS = {"write_stdin"}
WAIT_TOOLS = {"wait"}
INTERACTION_TOOLS = {"request_user_input"}


def tool_category(tool_name: str) -> str:
    """Assign a stable, small category without interpreting tool output."""

    normalized = tool_name.lower()
    if normalized in COMMAND_TOOLS:
        return "command"
    if normalized in EDIT_TOOLS:
        return "edit"
    if normalized in CONTINUATION_TOOLS:
        return "continuation"
    if normalized in WAIT_TOOLS:
        return "wait"
    if normalized in INTERACTION_TOOLS:
        return "interaction"
    if "search" in normalized or normalized in {"_fetch", "_fetch_file"}:
        return "search"
    if normalized in {"js", "click", "press_key", "get_app_state"}:
        return "interface"
    if normalized == "view_image":
        return "inspect"
    return "tool"


def _bounded_redacted(value: str) -> tuple[str, int, bool, list[str]]:
    """Bound one captured field, then apply the shared storage redactor."""

    original_chars = len(value)
    truncated = original_chars > MAX_TEXT_CHARS
    if truncated:
        half = MAX_TEXT_CHARS // 2
        value = f"{value[:half]}\n... [{original_chars - MAX_TEXT_CHARS} chars elided] ...\n{value[-half:]}"
    redacted = shared_redactor().apply(value)
    return redacted.text, original_chars, truncated, redacted.findings


def _duration_ms(called_at: str | None, completed_at: str | None) -> int | None:
    """Return a non-negative tool duration when both timestamps are valid."""

    start = parse_devsql_timestamp(called_at) if called_at else None
    end = parse_devsql_timestamp(completed_at) if completed_at else None
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds() * 1000)


class AgentCollector(Collector):
    """Polls every available agent adapter and folds turns into the timeline."""

    source = "agents"
    interval = 5.0

    def __init__(
        self,
        session: "Session",
        since: float | None = None,
        *,
        devsql_client: DevSQLClient | None = None,
        devsql_discover: DevSQLDiscoverer = DevSQLClient.discover,
    ) -> None:
        super().__init__(session)
        self._since = since or time.time()
        self._devsql_client = devsql_client
        self._devsql_discover = devsql_discover
        self._adapters: list[BaseAdapter] = []
        self._failed: set[str] = set()
        self._seen_keys = {
            capture_id
            for event in session.writer.read()
            if event.type in {AGENT_MESSAGE, AGENT_TOOL_COMPLETED}
            and isinstance(capture_id := event.payload.get("capture_id"), str)
            and capture_id
        }

    def probe(self) -> CollectorStatus:
        candidates: list[BaseAdapter] = [
            ClaudeCodeAdapter(),
            CopilotChatAdapter(),
            CursorAdapter(),
        ]
        detected: dict[str, bool] = {
            DevSQLCodexAdapter.name: False,
            DevSQLCodexToolAdapter.name: False,
        }
        try:
            client = self._devsql_client or self._devsql_discover()
            existing_events = self.session.writer.read()
            interval_start = active_interval_start(self.session)
            candidates[0:0] = [
                DevSQLCodexAdapter(
                    client,
                    since=interval_start,
                    existing_events=existing_events,
                ),
                DevSQLCodexToolAdapter(
                    client,
                    since=interval_start,
                    existing_events=existing_events,
                ),
            ]
        except Exception:
            # DevSQL is optional for agent capture. Direct adapters must remain
            # available if discovery or schema validation fails.
            pass

        self._adapters = []
        for adapter in candidates:
            try:
                ok = adapter.available()
            except Exception:
                ok = False
            detected[adapter.name] = ok
            if ok:
                # Only capture turns from now on: the analyst pressed record
                # just now, not an hour ago.
                with contextlib.suppress(Exception):
                    adapter.seek_to_end()
                self._adapters.append(adapter)

        if not self._adapters:
            raise Degraded(
                "no-agent-tools-found",
                "No Codex, Claude Code, Copilot Chat or Cursor transcripts "
                "found. Use `wfrec attach-transcript <file> --tool <name>` "
                "to fold one in manually.",
                fallback="manual-attach",
            )
        return CollectorStatus(
            source=self.source,
            available=True,
            backend=",".join(a.name for a in self._adapters),
            extra={"detected": detected, "failed": []},
        )

    def _run_once(self) -> None:
        events: list[Event] = []
        for adapter in self._adapters:
            if adapter.name in self._failed:
                continue
            try:
                read_activities = getattr(adapter, "activities", adapter.turns)
                for activity in read_activities(self._since):
                    event = self._to_event(activity)
                    if event is not None:
                        events.append(event)
            except Exception as exc:
                # Isolate the failure: one adapter's schema drift must not stop
                # the others, and the recorder must say what broke.
                self._failed.add(adapter.name)
                self.status.extra["failed"] = sorted(self._failed)
                events.append(
                    Event(
                        source=self.source,
                        type=AGENT_ADAPTER_FAILED,
                        payload={
                            "tool": adapter.name,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                )
        self.emit(events)

    def _to_event(self, activity: Turn | ToolExecution) -> Event | None:
        if activity.key:
            if activity.key in self._seen_keys:
                return None
            self._seen_keys.add(activity.key)

        if isinstance(activity, ToolExecution):
            return self._tool_event(activity)
        return self._message_event(activity)

    @staticmethod
    def _message_event(turn: Turn) -> Event:
        """Convert one conversation turn into a redacted event."""

        text = turn.text
        truncated = len(text) > MAX_TEXT_CHARS
        if truncated:
            head = text[: MAX_TEXT_CHARS // 2]
            tail = text[-MAX_TEXT_CHARS // 2 :]
            text = (
                f"{head}\n... [{len(turn.text) - MAX_TEXT_CHARS} chars elided by wfrec] ...\n{tail}"
            )

        redacted = shared_redactor().apply(text)
        event = Event(
            source="agents",
            type=AGENT_MESSAGE,
            payload={
                "tool": turn.tool,
                "role": turn.role,
                "text": redacted.text,
                "cwd": turn.cwd,
                "chars": len(turn.text),
                "truncated": truncated,
                **({"capture_id": turn.key} if turn.key else {}),
                **(turn.meta or {}),
            },
            redactions=redacted.findings,
        )
        if turn.ts:
            event.ts = turn.ts
        return event

    @staticmethod
    def _tool_event(execution: ToolExecution) -> Event:
        """Convert one completed tool execution into a bounded, redacted event."""

        command, command_chars, command_truncated, command_findings = _bounded_redacted(
            execution.command
        )
        arguments, argument_chars, arguments_truncated, argument_findings = _bounded_redacted(
            execution.arguments
        )
        output, output_chars, output_truncated, output_findings = _bounded_redacted(
            execution.output
        )
        findings = list(
            dict.fromkeys([*command_findings, *argument_findings, *output_findings])
        )
        status = (
            "completed"
            if execution.exit_code is None
            else "succeeded"
            if execution.exit_code == 0
            else "failed"
        )
        event = Event(
            source="agents",
            type=AGENT_TOOL_COMPLETED,
            payload={
                "agent": execution.agent,
                "tool": execution.agent,
                "tool_name": execution.tool_name,
                "category": tool_category(execution.tool_name),
                "command": command,
                "arguments": arguments,
                "output": output,
                "exit_code": execution.exit_code,
                "status": status,
                "cwd": execution.cwd,
                "called_at": execution.called_at,
                "completed_at": execution.completed_at,
                "duration_ms": _duration_ms(execution.called_at, execution.completed_at),
                "command_chars": command_chars,
                "command_truncated": command_truncated,
                "argument_chars": argument_chars,
                "arguments_truncated": arguments_truncated,
                "output_chars": output_chars,
                "output_truncated": output_truncated,
                **({"capture_id": execution.key} if execution.key else {}),
                **(execution.meta or {}),
            },
            redactions=findings,
        )
        if execution.completed_at:
            event.ts = execution.completed_at
        return event


def attach_transcript(session, path: Path, tool: str = "manual") -> list[Event]:
    """Fold an arbitrary transcript file into the timeline.

    The universal escape hatch. Cursor and Copilot formats will drift; this
    path works regardless, so a demo is never blocked on an IDE release.
    """

    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    turns: list[Turn] = []

    # Try JSONL first, then a single JSON document, then plain text.
    parsed_any = False
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            parsed_any = False
            break
        parsed_any = True
        if isinstance(record, dict):
            message = record.get("message") or record
            text = _flatten_content(message.get("content") or message.get("text"))
            if text.strip():
                turns.append(
                    Turn(
                        tool=tool,
                        role=str(record.get("role") or record.get("type") or "unknown"),
                        text=text,
                        ts=_iso(record.get("timestamp")),
                    )
                )

    if not parsed_any:
        try:
            document = json.loads(raw)
            for record in document if isinstance(document, list) else [document]:
                text = _flatten_content(record)
                if text.strip():
                    turns.append(Turn(tool=tool, role="unknown", text=text))
        except json.JSONDecodeError:
            turns.append(Turn(tool=tool, role="unknown", text=raw))

    collector = AgentCollector(session)
    events = [event for turn in turns if (event := collector._to_event(turn)) is not None]
    session.writer.extend(events)
    return events
