"""The audit records analytics, not content. Verified by sweeping the tree.

Every planted PHI value is checked against **every byte** of `seal.json`,
`deid/audit.jsonl` and every file the seal touched. And a planted credential is
checked against the whole session directory *and* `~/.wfrec/daemon.log`.

This is the test that makes "no reverse map, ever" an assertion rather than a
stated intention. It is deliberately a byte sweep rather than a schema check:
a schema check verifies the fields somebody remembered to think about.
"""

from __future__ import annotations

import json

import pytest

from wfrec.events import Event
from wfrec.seal import AUDIT_RELPATH, marker_path, seal_session

#: Planted values, one per label class, all inside the reserved ranges the eval
#: corpus documents so no fixture can collide with a live value.
PLANTED = {
    "MRN": "MRN 4419902",
    "MRN_DIGITS": "4419902",
    "EMAIL": "jane.smith@example.org",
    "SSN": "000-12-3456",
    "SUBJECT_ID": "SJ-4817",
    "PHONE": "+1-555-555-0142",
    "IP": "192.0.2.44",
    "NAME": "Zephyrine Quibblewick",
    "DOB": "2012-06-01",
    "PATH_NAME": "quibblewick_zephyrine",
}

CREDENTIALS = {
    "anthropic": "sk-ant-api03-" + "A" * 40,
    "openai": "sk-proj-" + "B" * 40,
    "openai_legacy": "sk-" + "C" * 40,
    "google": "AIza" + "D" * 35,
}


class NameEngine:
    """A fake model tier that finds the planted name, so NAME is exercised."""

    name = "fake_model"
    kind = "model"

    def info(self):
        from autocab.deid.spans import DetectorInfo

        return DetectorInfo(name=self.name, kind=self.kind, available=True)

    def detect(self, texts):
        from autocab.deid.spans import Span

        needle = PLANTED["NAME"]
        out = []
        for text in texts:
            spans = []
            start = text.find(needle)
            while start != -1:
                spans.append(
                    Span(
                        start=start,
                        end=start + len(needle),
                        label="NAME",
                        detector=f"model:{self.name}",
                        score=0.99,
                    )
                )
                start = text.find(needle, start + 1)
            out.append(spans)
        return out


@pytest.fixture()
def sealed(store, monkeypatch):
    session, _ = store.start(title="A", analyst="analyst-a")
    session.writer.append(
        Event(
            source="context",
            type="context.note",
            payload={
                "text": (
                    f"{PLANTED['NAME']}, {PLANTED['MRN']}, DOB {PLANTED['DOB']}, "
                    f"SSN {PLANTED['SSN']}, {PLANTED['EMAIL']}, "
                    f"{PLANTED['PHONE']}, {PLANTED['IP']}, {PLANTED['SUBJECT_ID']}"
                ),
                "path": f"/data/{PLANTED['PATH_NAME']}_R1.fastq.gz",
            },
        )
    )
    (session.root / "jobs").mkdir(parents=True, exist_ok=True)
    (session.root / "jobs" / "1.out").write_text(
        f"{PLANTED['MRN']} / {PLANTED['NAME']}\n", encoding="utf-8"
    )
    store.stop(session.session_id)
    session = store.resolve(session.session_id)

    for name, value in CREDENTIALS.items():
        monkeypatch.setenv(f"FAKE_{name.upper()}_KEY", value)

    result = seal_session(
        session,
        detectors=[NameEngine()],
        force=True,
        engine_label="gliner",
        key=b"\x01" * 32,
    )
    return session, result


def _all_bytes(root) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --------------------------------------------------------------------------
# the sweep
# --------------------------------------------------------------------------
def test_no_planted_value_appears_anywhere_under_the_session(sealed):
    session, _result = sealed
    files = _all_bytes(session.root)

    leaks: list[str] = []
    for label, value in PLANTED.items():
        needle = value.encode("utf-8")
        for name, blob in files.items():
            if needle in blob:
                leaks.append(f"{label} ({value!r}) in {name}")

    assert leaks == [], "\n".join(leaks)


def test_the_audit_carries_no_matched_text_and_no_digest_of_it(sealed):
    """A plain ``sha256`` of a six-digit MRN brute-forces in milliseconds.

    So the audit records ``value_id`` -- a monotonic integer per distinct
    ``(label, normalized_surface)`` -- which delivers every analytic the audit
    exists for (distinct-value counts, repeat rates, recurrence) with zero
    recoverable content, and dies with the run.
    """

    session, _result = sealed
    audit = (session.root / AUDIT_RELPATH).read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit.splitlines() if line.strip()]

    assert records
    allowed = {
        "target",
        "field",
        "label",
        "engine",
        "score",
        "start",
        "end",
        "action",
        "surrogate",
        "value_id",
        "seq",
        "absorbed",
    }
    for record in records:
        assert set(record) <= allowed, set(record) - allowed
        # No content, and no hash standing in for content.
        assert "text" not in record
        assert "surface" not in record
        assert "sha256" not in record
        assert "hash" not in record
        assert "digest" not in record


def test_the_audit_omits_length_for_name(sealed):
    """A four-character surname is a meaningful hint.

    `len` is omitted for every label, and NAME is the one where it would matter
    most -- so the assertion names it.
    """

    session, _result = sealed
    records = [
        json.loads(line)
        for line in (session.root / AUDIT_RELPATH).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    names = [record for record in records if record["label"] == "NAME"]

    assert names, "the fake model tier should have found the planted name"
    for record in names:
        assert "len" not in record
        assert "length" not in record


def test_value_id_delivers_linkage_without_content(sealed):
    """The same value in two files shares a ``value_id`` and a surrogate."""

    session, _result = sealed
    records = [
        json.loads(line)
        for line in (session.root / AUDIT_RELPATH).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    mrn_records = [record for record in records if record["label"] == "MRN"]

    assert len(mrn_records) >= 2, "the MRN was planted in the timeline and in jobs/"
    assert len({record["value_id"] for record in mrn_records}) == 1
    assert len({record["surrogate"] for record in mrn_records}) == 1
    # Different targets, same value: that is the whole point of the linkage.
    assert len({record["target"] for record in mrn_records}) >= 2


def test_no_reverse_map_file_exists_anywhere(sealed):
    """**No reverse map, ever.** Not a config option, not a debug flag."""

    session, _result = sealed
    names = {path.name for path in session.root.rglob("*")}

    for forbidden in ("pseudonyms.json", "pseudonym-map.json", "deid-map.json", "surrogates.json"):
        assert forbidden not in names

    blob = json.dumps(_readable(session.root))
    assert "pseudonyms" not in blob


def _readable(root) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            out[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return out


def test_the_seal_record_states_the_key_and_map_status(sealed):
    session, _result = sealed
    record = json.loads(marker_path(session.root).read_text(encoding="utf-8"))

    assert record["pseudonym_key"] == "discarded"
    assert record["reverse_map"] == "not-written"
    # And the record itself carries no planted value.
    blob = json.dumps(record)
    for value in PLANTED.values():
        assert value not in blob


@pytest.mark.parametrize("credential", sorted(CREDENTIALS))
def test_no_credential_shape_appears_under_the_session_or_the_daemon_log(
    sealed, credential, wfrec_home
):
    """Parameterized over all four credential shapes.

    A key is planted in the environment, a fully local seal runs, and then the
    entire session tree *and* ``~/.wfrec/daemon.log`` are walked asserting the
    string appears nowhere. Tested, not stated.
    """

    session, _result = sealed
    value = CREDENTIALS[credential].encode("utf-8")

    for name, blob in _all_bytes(session.root).items():
        assert value not in blob, f"{credential} leaked into {name}"

    daemon_log = wfrec_home / "daemon.log"
    if daemon_log.exists():
        assert value not in daemon_log.read_bytes()


def test_the_audit_writer_scrubs_a_credential_that_somehow_reaches_it():
    """The last-ditch guard, tested directly.

    The audit should never see a credential -- it records labels, offsets and
    surrogates. This exists because "should never" is not a control.
    """

    from wfrec.seal import _scrub_secrets

    for value in CREDENTIALS.values():
        out = _scrub_secrets(f'{{"field": "note", "detail": "{value}"}}')
        assert value not in out
        assert "[REDACTED_CREDENTIAL]" in out


def test_the_pseudonymizer_cannot_be_serialized():
    """A serializable key is a key that ends up in a pickle, a traceback frame,
    or a cache."""

    import pickle

    from autocab.deid.pseudonym import Pseudonymizer

    with Pseudonymizer(key=b"\x01" * 32) as pseudo:
        with pytest.raises(TypeError, match="never leave the process"):
            pickle.dumps(pseudo)
        assert "01010101" not in repr(pseudo)
        assert repr(pseudo) == "Pseudonymizer(key=<discarded>)"


def test_counts_by_label_is_analytics_not_content(sealed):
    session, result = sealed
    counts = result.record["counts_by_label"]

    assert counts["MRN"] >= 2
    assert counts["NAME"] >= 1
    assert result.record["distinct_values"] >= 5
    assert result.record["collisions"] == []
    # Counts only; no keys in this mapping are values.
    for key in counts:
        assert key.isupper() or "_" in key
