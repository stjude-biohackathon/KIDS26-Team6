"""Normalization tests for evidence-linked recorded actions."""

from __future__ import annotations

from autocab.recording.events import Event
from autocab.workflow.observed_actions import build_observed_actions


def _event(
    seq: int,
    event_type: str,
    payload: dict[str, object],
    *,
    second: int,
) -> Event:
    return Event(
        source="agents" if event_type.startswith("agent.") else "shell",
        type=event_type,
        payload=payload,
        seq=seq,
        ts=f"2030-01-01T00:00:{second:02d}.000Z",
    )


def _evidence(*events: Event) -> dict[str, str]:
    return {f"events.jsonl#seq={event.seq}": f"evidence-{event.seq}" for event in events}


def test_duplicate_shell_and_agent_commands_become_one_action() -> None:
    shell = _event(
        1,
        "shell.command.completed",
        {"command": "python workflow.py", "cwd": "/project"},
        second=1,
    )
    agent = _event(
        2,
        "agent.tool.completed",
        {
            "category": "command",
            "tool_name": "exec_command",
            "command": "python  workflow.py",
            "cwd": "/project",
            "session_id": "thread-1",
        },
        second=2,
    )
    continuation = _event(
        3,
        "agent.tool.completed",
        {
            "category": "continuation",
            "tool_name": "write_stdin",
            "session_id": "thread-1",
        },
        second=3,
    )

    actions = build_observed_actions(
        [shell, agent, continuation],
        _evidence(shell, agent, continuation),
    )

    assert len(actions) == 1
    assert actions[0].command == "python workflow.py"
    assert actions[0].tool_name == "exec_command"
    assert actions[0].evidence_ids == ["evidence-1", "evidence-2", "evidence-3"]


def test_edit_actions_link_nearby_file_diff_evidence() -> None:
    edit = _event(
        1,
        "agent.tool.completed",
        {
            "category": "edit",
            "tool_name": "apply_patch",
            "session_id": "thread-1",
        },
        second=10,
    )
    diff = _event(
        2,
        "file.diff",
        {"path": "workflow.py", "added": 4, "deleted": 1},
        second=12,
    )

    actions = build_observed_actions([edit, diff], _evidence(edit, diff))

    assert len(actions) == 1
    assert actions[0].kind == "edit"
    assert actions[0].tool_name == "apply_patch"
    assert actions[0].evidence_ids == ["evidence-1", "evidence-2"]


def test_messages_and_coordination_calls_do_not_become_steps() -> None:
    message = _event(
        1,
        "agent.message",
        {"role": "assistant", "text": "I edited the workflow."},
        second=1,
    )
    wait = _event(
        2,
        "agent.tool.completed",
        {"category": "wait", "tool_name": "wait"},
        second=2,
    )
    prompt = _event(
        3,
        "agent.tool.completed",
        {"category": "interaction", "tool_name": "request_user_input"},
        second=3,
    )

    actions = build_observed_actions(
        [message, wait, prompt],
        _evidence(message, wait, prompt),
    )

    assert actions == []


def test_repeated_external_tool_calls_are_grouped() -> None:
    first = _event(
        1,
        "agent.tool.completed",
        {
            "category": "search",
            "tool_name": "web_search",
            "session_id": "thread-1",
        },
        second=1,
    )
    second = _event(
        2,
        "agent.tool.completed",
        {
            "category": "search",
            "tool_name": "web_search",
            "session_id": "thread-1",
        },
        second=20,
    )

    actions = build_observed_actions([first, second], _evidence(first, second))

    assert len(actions) == 1
    assert actions[0].kind == "manual"
    assert actions[0].evidence_ids == ["evidence-1", "evidence-2"]
