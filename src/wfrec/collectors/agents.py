"""Agent/chat transcript collector: Codex, Claude Code, Copilot Chat, Cursor.

Codex uses DevSQL's normalized tables. The direct adapters read undocumented,
version-drifting on-disk formats. The module's organizing principle is failure
isolation: a schema change emits ``agent.adapter.failed`` while the other
adapters keep working.

There is also always a manual path -- ``wfrec attach-transcript`` -- which works
regardless of format drift and is what guarantees a demo can proceed.
"""

from __future__ import annotations

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
from ..events import AGENT_ADAPTER_FAILED, AGENT_MESSAGE, Event
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
                    parts.append(f"[tool_use {name}] {json.dumps(item.get('input'), default=str)[:2000]}")
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


class BaseAdapter:
    """One agent tool's on-disk transcript format."""

    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def seek_to_end(self) -> None:
        """Skip anything already on disk, so only new turns are recorded."""

    def turns(self, since: float) -> Iterator[Turn]:
        raise NotImplementedError


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
            raise ValueError(
                "DevSQL interval start must be a timezone-aware timestamp."
            )
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
            if not isinstance(capture_id, str) or not capture_id.startswith(
                "codex:"
            ):
                continue
            occurred_at = parse_devsql_timestamp(event.ts)
            if occurred_at is not None and occurred_at >= self.interval_start:
                self._query_start = max(self._query_start, occurred_at)

    @staticmethod
    def _convert(
        row: dict[str, Any], occurred_at: datetime
    ) -> Turn | None:
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
            root / "User" / "globalStorage" / "state.vscdb"
            for root in app_support_dirs("Cursor")
        ]
        self._seen: set[str] = set()

    def available(self) -> bool:
        return any(path.exists() for path in self.databases)

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=2.0
        )
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
            if event.type == AGENT_MESSAGE
            and isinstance(
                capture_id := event.payload.get("capture_id"), str
            )
            and capture_id
        }

    def probe(self) -> CollectorStatus:
        candidates: list[BaseAdapter] = [
            ClaudeCodeAdapter(),
            CopilotChatAdapter(),
            CursorAdapter(),
        ]
        detected: dict[str, bool] = {DevSQLCodexAdapter.name: False}
        try:
            client = self._devsql_client or self._devsql_discover()
            candidates.insert(
                0,
                DevSQLCodexAdapter(
                    client,
                    since=active_interval_start(self.session),
                    existing_events=self.session.writer.read(),
                ),
            )
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
                try:
                    adapter.seek_to_end()
                except Exception:
                    pass
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
            extra={"detected": detected},
        )

    def _run_once(self) -> None:
        events: list[Event] = []
        for adapter in self._adapters:
            if adapter.name in self._failed:
                continue
            try:
                for turn in adapter.turns(self._since):
                    event = self._to_event(turn)
                    if event is not None:
                        events.append(event)
            except Exception as exc:
                # Isolate the failure: one adapter's schema drift must not stop
                # the others, and the recorder must say what broke.
                self._failed.add(adapter.name)
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

    def _to_event(self, turn: Turn) -> Event | None:
        if turn.key:
            if turn.key in self._seen_keys:
                return None
            self._seen_keys.add(turn.key)

        text = turn.text
        truncated = len(text) > MAX_TEXT_CHARS
        if truncated:
            head = text[: MAX_TEXT_CHARS // 2]
            tail = text[-MAX_TEXT_CHARS // 2 :]
            text = f"{head}\n... [{len(turn.text) - MAX_TEXT_CHARS} chars elided by wfrec] ...\n{tail}"

        redacted = shared_redactor().apply(text)
        event = Event(
            source=self.source,
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
