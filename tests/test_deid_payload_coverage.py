"""Every event type, every string field.

The inverted schema rule is the seal's central safety claim: adding an event
type can only ever *over*-scrub, never under-scrub. This file is what makes that
a claim rather than a hope -- it plants a **distinct sentinel in every string
field of one event of every type declared in events.py** and asserts that not
one of them survives.

If somebody adds an event type with a free-text payload key and forgets the
seal, this test fails. If somebody adds a key to `SKIP_KEYS` that actually
carries typed text, this test fails.
"""

from __future__ import annotations

import json

import pytest

from wfrec import events as events_mod
from wfrec.events import Event
from wfrec.seal import PATH_KEYS, SKIP_KEYS, seal_session


def all_event_types() -> list[str]:
    """Every event-type constant ``events.py`` declares.

    Read from the module rather than listed here, so a new constant is picked up
    automatically instead of needing somebody to remember this file exists.
    """

    out: list[str] = []
    for name in dir(events_mod):
        if name.startswith("_") or not name.isupper():
            continue
        value = getattr(events_mod, name)
        if isinstance(value, str) and "." in value and not value.startswith("~"):
            out.append(value)
    return sorted(set(out))


#: Payload keys the recorder actually writes, harvested from across the
#: collectors and exporters. Every one gets a sentinel.
TEXT_KEYS = (
    "text",
    "label",
    "command",
    "cwd",
    "path",
    "root",
    "workdir",
    "note",
    "ocr_text",
    "window_title",
    "title",
    "detail",
    "message",
    "content",
    "branch",
    "diff",
    "patch",
    "summary",
    "reason",
    "job_name",
    "name",
    "stdout",
    "stderr",
    "output",
    "error",
    "shell",
    "tool",
    "role",
    "ref",
    "adapter",
    "family",
    "workflow_family",
    "tags",
    "dest",
    "src",
    "filename",
)


@pytest.fixture()
def planted(store):
    """One event of every declared type, each field a distinct sentinel.

    The sentinels are shaped like real identifiers on purpose: a sentinel the
    regex tier cannot detect would make this test pass for the wrong reason, so
    each one is a labelled MRN in the documented test band.
    """

    session, _ = store.start(title="A", analyst="analyst-a")
    sentinels: dict[str, str] = {}
    batch: list[Event] = []

    for index, event_type in enumerate(all_event_types()):
        payload: dict[str, object] = {}
        for offset, key in enumerate(TEXT_KEYS):
            sentinel = f"MRN 44{index:02d}{offset:03d}"
            sentinels[f"{event_type}.{key}"] = sentinel
            payload[key] = sentinel
        # Nested, listed, and path-shaped placements too: the walker has to
        # reach all of them.
        nested_sentinel = f"MRN 4490{index:03d}"
        sentinels[f"{event_type}.nested"] = nested_sentinel
        payload["nested"] = {"deeper": {"deepest": nested_sentinel}}
        list_sentinel = f"MRN 4491{index:03d}"
        sentinels[f"{event_type}.list"] = list_sentinel
        payload["items"] = [{"value": list_sentinel}, list_sentinel]
        # A *subject id*, not `MRN_4492000`: the latter is indistinguishable
        # from a surrogate (`MRN_` + hex) and SurrogateGuard correctly protects
        # it, which would make this test fail for a reason that is a documented
        # design limit rather than a bug. See
        # `test_a_value_shaped_like_a_surrogate_is_protected` below.
        path_sentinel = f"SJ4492{index:03d}"
        sentinels[f"{event_type}.pathfield"] = path_sentinel
        payload["path"] = f"/data/{path_sentinel}/x.bam"
        # Structural keys carry values that must survive untouched.
        payload["exit_code"] = 0
        payload["duration_ms"] = 12
        payload["seq"] = 999

        batch.append(Event(source="test", type=event_type, payload=payload))

    session.writer.extend(batch)
    store.stop(session.session_id)
    return store.resolve(session.session_id), sentinels


def test_every_declared_event_type_is_covered(planted):
    session, _ = planted
    types_in_timeline = {
        json.loads(line)["type"]
        for line in (session.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    }

    for event_type in all_event_types():
        assert event_type in types_in_timeline, event_type
    assert len(all_event_types()) > 15, "the harvest found suspiciously few types"


def test_no_planted_sentinel_survives_the_seal(planted):
    session, sentinels = planted

    seal_session(session, force=True, key=b"\x01" * 32)

    blob = (session.root / "events.jsonl").read_text(encoding="utf-8")
    survivors = sorted(where for where, sentinel in sentinels.items() if sentinel in blob)

    assert survivors == [], (
        f"{len(survivors)} planted sentinel(s) survived the seal. This is the "
        "inverted schema rule failing: every string value in every payload must "
        f"be scrubbed. First few: {survivors[:8]}"
    )


def test_structural_keys_keep_their_values(planted):
    session, _ = planted

    seal_session(session, force=True, key=b"\x01" * 32)

    for line in (session.root / "events.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["source"] != "test":
            continue
        payload = record["payload"]
        assert payload["exit_code"] == 0
        assert payload["duration_ms"] == 12
        assert payload["seq"] == 999


def test_nested_list_and_path_placements_are_all_reached(planted):
    session, sentinels = planted

    seal_session(session, force=True, key=b"\x01" * 32)
    blob = (session.root / "events.jsonl").read_text(encoding="utf-8")

    for where, sentinel in sentinels.items():
        if where.endswith((".nested", ".list", ".pathfield")):
            assert sentinel not in blob, where


def test_path_fields_keep_their_structure(planted):
    session, _ = planted

    seal_session(session, force=True, key=b"\x01" * 32)

    for line in (session.root / "events.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["source"] != "test":
            continue
        path = record["payload"]["path"]
        # `/data/<scrubbed>/x.bam` -- separators and the surrounding components
        # survive so the directory structure an analyst reads is still there.
        assert path.startswith("/data/")
        assert path.endswith("/x.bam")
        assert path.count("/") == 3


def test_skip_keys_and_path_keys_do_not_overlap():
    """A key in both sets would be skipped, silently losing the component-wise
    scrubbing that `PATH_KEYS` exists to provide."""

    assert SKIP_KEYS & PATH_KEYS == set()


def test_adding_an_unknown_payload_key_is_scrubbed_not_skipped(store):
    """The whole point: a key nobody has thought about yet is still scrubbed."""

    session, _ = store.start(title="A", analyst="a")
    session.writer.append(
        Event(
            source="future",
            type="future.event.type",
            payload={"a_key_invented_next_year": "MRN 4419902"},
        )
    )
    store.stop(session.session_id)
    session = store.resolve(session.session_id)

    seal_session(session, force=True, key=b"\x01" * 32)

    assert "4419902" not in (session.root / "events.jsonl").read_text(encoding="utf-8")


def test_a_value_shaped_like_a_surrogate_is_protected(store):
    """A documented limit of idempotency-by-grammar, asserted so it stays known.

    ``SurrogateGuard`` recognizes a surrogate by its shape -- a known prefix, an
    underscore, and six or more lowercase hex characters -- because the seal key
    is ephemeral and a key-based check is impossible. The consequence is that a
    *real* value with exactly that shape is protected and survives.

    The alternatives are all worse: narrowing the pattern breaks every seal
    written with a different surrogate width, and dropping the guard makes a
    reseal pseudonymize its own pseudonyms. See the module docstring in
    ``engines/surrogate_guard.py``.
    """

    session, _ = store.start(title="A", analyst="a")
    session.writer.append(
        Event(
            source="test",
            type="context.note",
            payload={
                # Collides with the surrogate grammar: survives.
                "collides": "MRN_4492000",
                # The way a human writes it: scrubbed.
                "human": "MRN 4492000",
            },
        )
    )
    store.stop(session.session_id)
    session = store.resolve(session.session_id)

    result = seal_session(session, force=True, key=b"\x01" * 32)
    blob = (session.root / "events.jsonl").read_text(encoding="utf-8")

    assert "MRN_4492000" in blob, "the guard must keep a surrogate-shaped value"
    assert "MRN 4492000" not in blob
    # And it is *counted*, so the seal record shows the guard fired rather than
    # the value simply going unnoticed.
    assert result.record["masked_at_capture"] >= 1
