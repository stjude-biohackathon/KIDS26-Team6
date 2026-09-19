"""Normalize recorded shell, agent, and file activity into workflow actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from autocab.recording.events import (
    AGENT_TOOL_COMPLETED,
    FILE_DIFF,
    SHELL_COMMAND,
    Event,
)

COMMAND_MATCH_SECONDS = 2.0
EDIT_MATCH_SECONDS = 10.0
TOOL_GROUP_SECONDS = 60.0
IGNORED_TOOL_CATEGORIES = {"continuation", "interaction", "wait"}


@dataclass(slots=True)
class ObservedAction:
    """One evidence-linked action proposed for the initial SkillSpec."""

    kind: str
    tool_name: str
    evidence_ids: list[str]
    timestamp: float
    command: str | None = None
    cwd: str = ""
    agent_session: str = ""
    category: str = ""


def build_observed_actions(
    events: list[Event],
    evidence_by_locator: dict[str, str],
) -> list[ObservedAction]:
    """Build ordered actions while joining duplicate and supporting evidence."""

    actions: list[ObservedAction] = []
    latest_agent_command: dict[str, ObservedAction] = {}
    file_diffs: list[tuple[Event, str]] = []

    for event in events:
        evidence_id = evidence_by_locator.get(_locator(event))
        if evidence_id is None:
            continue
        if event.type == FILE_DIFF:
            file_diffs.append((event, evidence_id))
            continue
        if event.type == SHELL_COMMAND:
            command = _text(event, "command")
            if command:
                _add_command(actions, event, evidence_id, command, tool_name="shell")
            continue
        if event.type != AGENT_TOOL_COMPLETED:
            continue

        category = _text(event, "category") or "tool"
        agent_session = _text(event, "session_id")
        if category == "continuation":
            previous = latest_agent_command.get(agent_session)
            if previous is not None:
                _append_evidence(previous, evidence_id)
            continue
        if category in IGNORED_TOOL_CATEGORIES:
            continue

        tool_name = _text(event, "tool_name") or "agent-tool"
        command = _text(event, "command")
        if category == "command" and command:
            action = _add_command(
                actions,
                event,
                evidence_id,
                command,
                tool_name=tool_name,
                agent_session=agent_session,
            )
            if agent_session:
                latest_agent_command[agent_session] = action
            continue

        kind = "edit" if category == "edit" else "manual"
        _add_tool_action(
            actions,
            event,
            evidence_id,
            kind=kind,
            tool_name=tool_name,
            category=category,
            agent_session=agent_session,
        )

    _attach_file_diffs(actions, file_diffs)
    return actions


def _add_command(
    actions: list[ObservedAction],
    event: Event,
    evidence_id: str,
    command: str,
    *,
    tool_name: str,
    agent_session: str = "",
) -> ObservedAction:
    timestamp = _timestamp(event)
    cwd = _text(event, "cwd")
    key = _command_key(command)
    for action in reversed(actions):
        if timestamp - action.timestamp > COMMAND_MATCH_SECONDS:
            break
        if (
            action.kind == "command"
            and action.command is not None
            and _command_key(action.command) == key
            and (not action.cwd or not cwd or action.cwd == cwd)
        ):
            _append_evidence(action, evidence_id)
            if action.tool_name == "shell" and tool_name != "shell":
                action.tool_name = tool_name
                action.agent_session = agent_session
            return action

    action = ObservedAction(
        kind="command",
        tool_name=tool_name,
        command=command,
        evidence_ids=[evidence_id],
        timestamp=timestamp,
        cwd=cwd,
        agent_session=agent_session,
        category="command",
    )
    actions.append(action)
    return action


def _add_tool_action(
    actions: list[ObservedAction],
    event: Event,
    evidence_id: str,
    *,
    kind: str,
    tool_name: str,
    category: str,
    agent_session: str,
) -> None:
    timestamp = _timestamp(event)
    for action in reversed(actions):
        if timestamp - action.timestamp > TOOL_GROUP_SECONDS:
            break
        if (
            action.kind == kind
            and action.tool_name == tool_name
            and action.category == category
            and action.agent_session == agent_session
        ):
            _append_evidence(action, evidence_id)
            return
    actions.append(
        ObservedAction(
            kind=kind,
            tool_name=tool_name,
            evidence_ids=[evidence_id],
            timestamp=timestamp,
            cwd=_text(event, "cwd"),
            agent_session=agent_session,
            category=category,
        )
    )


def _attach_file_diffs(
    actions: list[ObservedAction],
    file_diffs: list[tuple[Event, str]],
) -> None:
    edits = [action for action in actions if action.kind == "edit"]
    for event, evidence_id in file_diffs:
        event_time = _timestamp(event)
        nearby = [
            action
            for action in edits
            if abs(action.timestamp - event_time) <= EDIT_MATCH_SECONDS
        ]
        if nearby:
            nearest = min(nearby, key=lambda action: abs(action.timestamp - event_time))
            _append_evidence(nearest, evidence_id)


def _append_evidence(action: ObservedAction, evidence_id: str) -> None:
    if evidence_id not in action.evidence_ids:
        action.evidence_ids.append(evidence_id)


def _command_key(command: str) -> str:
    return " ".join(command.split())


def _locator(event: Event) -> str:
    return f"events.jsonl#seq={event.seq or 0}"


def _text(event: Event, field_name: str) -> str:
    value = event.payload.get(field_name)
    return value.strip() if isinstance(value, str) else ""


def _timestamp(event: Event) -> float:
    try:
        return datetime.fromisoformat(event.ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float(event.seq or 0)
