"""Seal a session: detect, rewrite destructively, and prove it happened.

Detection lives in ``autocab.deid``. This module is orchestration and I/O --
locking, staging, the three-phase commit, crash recovery, and the audit. Keeping
the split means a new detector tier cannot accidentally change the atomicity
story, and the atomicity story can be tested with a fake engine at zero model
cost.

The schema rule is **inverted**
-------------------------------
A fixed field map -- "scrub ``payload.command``, ``payload.ocr_text``,
``payload.note``" -- means every new event type and every new payload key
silently bypasses the seal. That is leak-by-default, and it fails in the most
expensive way: quietly, months later, in a session somebody already shared.

So the seal **recursively walks every payload and scrubs every ``str`` value**,
with an explicit :data:`SKIP_KEYS` set of structural keys. Adding an event type
can then only ever over-scrub, never under-scrub. Over-scrubbing is a bug report;
under-scrubbing is a disclosure.

The locking property everything rests on
----------------------------------------
``EventWriter.append`` opens ``events.jsonl`` with ``"a"`` **inside**
``file_lock(.events.lock)``, and the lock file is a **separate inode**. So
``os.replace(tmp, events.jsonl)`` while holding that lock is safe: a blocked
appender re-opens *by path* once the lock releases and lands on the new file, and
no appender holds a file descriptor across the swap.

``tests/test_deid_seal_concurrency.py`` asserts that directly, because a future
refactor to a long-lived append descriptor would break the seal **silently** --
the appends would land in the unlinked old inode and simply vanish.

Three phases
------------
======  ===================  ==========  ==================================
Phase   Lock                 Duration    Work
======  ===================  ==========  ==================================
1       ``.events.lock``     ms          snapshot the timeline, record size
2       ``.seal/lock`` only  s-min       detect, pseudonymize, stage
3       ``.events.lock``     ms          verify size, ``os.replace`` each file
======  ===================  ==========  ==================================

Phase 2 deliberately does **not** hold ``.events.lock``. The alternative is
holding it across minutes of inference, which stalls every collector thread in
the daemon -- and a safety feature that freezes the recorder gets switched off.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from autocab.deid import (
    Allowlist,
    Detector,
    Policy,
    Pseudonymizer,
    RegexRules,
    RenderMode,
    Span,
    SurrogateGuard,
    check_bounds,
    normalize_text,
    render,
    resolve,
)
from autocab.deid.allowlist import WORKFORCE_KEYS
from autocab.deid.compat import mask_token
from autocab.deid.config import CREDENTIAL_SHAPES
from autocab.deid.engines.base import table_digest
from autocab.deid.policy import Action, assurance_for
from autocab.deid.pseudonym import GENERALIZED_AGE

from .events import DEID_SEALED, Event, read_events, utc_now
from .locking import file_lock

SEAL_DIRNAME = ".seal"
SEAL_MARKER = "seal.json"
JOURNAL_NAME = "journal.json"
AUDIT_RELPATH = "deid/audit.jsonl"
SEAL_REPORT_RELPATH = "deid/seal-report.json"

MAX_TAIL_RETRIES = 3
MAX_TAIL_EVENTS = 200


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
class SealError(RuntimeError):
    """Base class for every refusal on the seal path."""


class NotSealed(SealError):
    """Something tried to consume an unsealed session.

    Raised by ``export_session`` and by ``session_bundle``. These gates are what
    give "fail closed" teeth: a failed seal blocks the leak instead of
    permitting it.
    """


class SealInProgress(SealError):
    """A journal is sitting in ``committing``.

    A mixed state can exist on disk for milliseconds; it must never be
    *consumed*. Readers refuse rather than read half a sealed session.
    """


class SessionActive(SealError):
    """The session is not idle. ``--force`` stops it first, then seals.

    **Refuse, don't race.** The alternative is holding ``.events.lock`` across
    minutes of inference.
    """


class SealAborted(SealError):
    """The seal gave up with nothing committed. The session is unchanged."""


# --------------------------------------------------------------------------
# the payload walker
# --------------------------------------------------------------------------
#: Structural keys: machine-generated, no free text, and several are the anchors
#: downstream analysis is built on. Scrubbing ``seq`` or ``ts`` would not leak
#: less, it would destroy the timeline.
#:
#: This set is the *only* thing standing between the walker and a payload key,
#: so it is deliberately short and every entry is a value whose shape is fixed
#: by the recorder rather than typed by a human.
SKIP_KEYS: frozenset[str] = frozenset(
    {
        "seq",
        "ts",
        "type",
        "source",
        "backend",
        "exit_code",
        "duration_ms",
        "chars",
        "lines",
        "bytes",
        "width",
        "height",
        "sha",
        "pid",
        "ppid",
        "prompt_seq",
        "job_id",
        "origin",
        "session",
        "kind",
        "score",
        "confidence",
        "count",
        "index",
        "frame",
        "coords",
        "quality",
        "method",
        "enabled",
        "reason_code",
        "status",
        "elapsed",
        "size",
        "mode",
        "v",
        "version",
        "schema",
        "state",
        "flags",
        "seconds",
    }
)

#: Keys whose value is a filesystem path. Scrubbed **component-wise**, keeping
#: the separators, so ``/data/proj/SJALL018/smith_jane_R1.fastq.gz`` becomes
#: ``/data/proj/SUBJ_9c2f1a.../NAME_4d81b2..._R1.fastq.gz`` and the directory
#: structure an analyst needs survives.
PATH_KEYS: frozenset[str] = frozenset(
    {
        "path",
        "cwd",
        "root",
        "workdir",
        "dir",
        "directory",
        "file",
        "filename",
        "relpath",
        "target",
        "dest",
        "destination",
        "src",
        "source_path",
        "output",
        "outdir",
        "log_path",
        "ref_path",
    }
)

_SEPARATORS = "/\\"


@dataclass(frozen=True, slots=True)
class Atom:
    """One string the detector will see.

    A plain field contributes one atom. A path field contributes one atom per
    component, which is what makes component-wise scrubbing possible without a
    second detection pass.
    """

    text: str
    path_field: bool = False


def _split_path(text: str) -> list[tuple[str, bool]]:
    """Split into ``(piece, is_component)``, preserving separators exactly."""

    pieces: list[tuple[str, bool]] = []
    buffer: list[str] = []
    for char in text:
        if char in _SEPARATORS:
            if buffer:
                pieces.append(("".join(buffer), True))
                buffer = []
            pieces.append((char, False))
        else:
            buffer.append(char)
    if buffer:
        pieces.append(("".join(buffer), True))
    return pieces


def transform_strings(
    node: Any,
    fn: Callable[[str, str, bool], str],
    *,
    prefix: str = "",
    skip: frozenset[str] = SKIP_KEYS,
    skip_workforce: bool = True,
) -> Any:
    """Rebuild ``node`` with every eligible string passed through ``fn``.

    ``fn(field_path, text, is_path_field) -> str``.

    Used twice per seal: once with a collecting ``fn`` that returns its input
    unchanged, to gather the atoms for one batched detection pass, and once with
    an applying ``fn`` that looks the rewrite up in the cache.

    ``skip_workforce`` drops ``analyst`` / ``host`` / ``user``: they identify
    the *workforce*, not the PHI subject, and ``WorkflowClusterer`` groups on
    ``analyst`` -- pseudonymizing it silently destroys the clustering the
    recorder exists to feed. ``--pseudonymize-analyst`` turns this off.
    """

    if isinstance(node, Mapping):
        out: dict[str, Any] = {}
        for key, value in node.items():
            name = str(key)
            lowered = name.casefold()
            child = f"{prefix}.{name}" if prefix else name
            if lowered in skip or (skip_workforce and lowered in WORKFORCE_KEYS):
                out[name] = value
                continue
            out[name] = transform_strings(
                value,
                fn,
                prefix=child,
                skip=skip,
                skip_workforce=skip_workforce,
            )
        return out
    if isinstance(node, list):
        return [
            transform_strings(
                item, fn, prefix=f"{prefix}[{index}]", skip=skip, skip_workforce=skip_workforce
            )
            for index, item in enumerate(node)
        ]
    if isinstance(node, tuple):  # pragma: no cover - payloads are JSON
        return tuple(
            transform_strings(item, fn, prefix=prefix, skip=skip, skip_workforce=skip_workforce)
            for item in node
        )
    if isinstance(node, str):
        leaf = prefix.rsplit(".", 1)[-1].split("[", 1)[0].casefold()
        return fn(prefix, node, leaf in PATH_KEYS)
    return node


def atoms_of(text: str, path_field: bool) -> list[Atom]:
    if not path_field:
        return [Atom(text=text)]
    return [
        Atom(text=piece, path_field=True)
        for piece, is_component in _split_path(text)
        if is_component and piece
    ]


# --------------------------------------------------------------------------
# the scrubber
# --------------------------------------------------------------------------
@dataclass
class Finding:
    """One audited detection."""

    target: str
    field: str
    label: str
    engine: str
    score: float
    start: int
    end: int
    action: str
    surrogate: str
    value_id: int
    seq: int | None = None
    absorbed: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "target": self.target,
            "field": self.field,
            "label": self.label,
            "engine": self.engine,
            "score": round(float(self.score), 4),
            "start": self.start,
            "end": self.end,
            "action": self.action,
            "surrogate": self.surrogate,
            "value_id": self.value_id,
        }
        if self.seq is not None:
            out["seq"] = self.seq
        if self.absorbed:
            out["absorbed"] = list(self.absorbed)
        # Deliberately absent: the matched text, any digest of it, and -- for
        # NAME -- even its length. A four-character surname is a meaningful
        # hint, and sha256("4419902") brute-forces in milliseconds, which is
        # why `value_id` carries the linkage instead.
        return out


class Scrubber:
    """Detect once per distinct string, rewrite many times.

    Two caches, and the important one is the **rewritten string**, not the
    spans: pseudonymization is a pure function of ``(label,
    normalized_surface)`` within a run, so a string that appears in four hundred
    consecutive OCR frames is detected once and rewritten once. Tens of
    thousands of events typically reduce to a few thousand distinct strings.
    """

    def __init__(
        self,
        *,
        pseudonymizer: Pseudonymizer,
        detectors: Sequence[Detector] = (),
        deny_terms: Sequence[str] = (),
        allowlist: Allowlist | None = None,
        policy: Policy | None = None,
    ) -> None:
        self._pseudo = pseudonymizer
        self._policy = policy or Policy(render=RenderMode.PSEUDONYMIZE)
        self._allowlist = allowlist or Allowlist.default()
        self._guard = SurrogateGuard()
        self._regex = RegexRules(deny_terms)
        self._model_tiers = tuple(detectors)
        self._stack: tuple[Detector, ...] = (self._guard, self._regex, *self._model_tiers)
        self._spans: dict[str, list[Span]] = {}
        self._rewritten: dict[str, str] = {}
        self.findings: list[Finding] = []
        self.masked_at_capture = 0

    # -- detection ---------------------------------------------------------
    def engines(self) -> list[dict[str, Any]]:
        return [detector.info().to_dict() for detector in self._stack]

    def prepare(self, atoms: Iterable[Atom]) -> None:
        """Run every tier over the distinct atoms, in one batch per tier.

        Batching here is the whole reason ``Detector.detect`` is batch-shaped by
        contract: a per-string loop over a model across 30k events is the
        difference between a 40-second seal and an hour.
        """

        pending = sorted(
            {atom.text for atom in atoms if atom.text and atom.text not in self._spans}
        )
        if not pending:
            return
        normalized = [normalize_text(text) for text in pending]
        per_detector = [detector.detect(normalized) for detector in self._stack]
        for index, text in enumerate(pending):
            candidates: list[Span] = []
            for results in per_detector:
                candidates.extend(results[index])
            check_bounds(normalized[index], candidates)
            self._spans[text] = resolve(normalized[index], candidates, keeps=self._allowlist.keeps)

    # -- rewriting ---------------------------------------------------------
    def rewrite_field(
        self, text: str, *, path_field: bool, target: str, field_path: str, seq: int | None
    ) -> str:
        if not path_field:
            return self._rewrite_atom(text, target=target, field_path=field_path, seq=seq, base=0)

        out: list[str] = []
        offset = 0
        for piece, is_component in _split_path(text):
            if is_component and piece:
                out.append(
                    self._rewrite_atom(
                        piece, target=target, field_path=field_path, seq=seq, base=offset
                    )
                )
            else:
                out.append(piece)
            offset += len(piece)
        return "".join(out)

    def _rewrite_atom(
        self, text: str, *, target: str, field_path: str, seq: int | None, base: int
    ) -> str:
        if not text:
            return text
        normalized = normalize_text(text)
        spans = self._spans.get(text)
        if spans is None:
            # A caller that skipped `prepare`. Detect now rather than silently
            # leaving the string unscrubbed.
            self.prepare([Atom(text=text)])
            spans = self._spans[text]

        # Invariant 3. Abort the whole seal rather than clip: a replacement in
        # the wrong region looks exactly like success.
        check_bounds(normalized, spans)
        cached = self._rewritten.get(text)
        if cached is not None:
            return cached

        # One replacement decision per span, recorded for the audit and reused
        # for the rewrite. Computing them twice would be harmless (both are pure
        # functions of `(label, normalized_surface)`) but it doubles the HMAC
        # work on the OCR path for no reason.
        replacements: dict[int, str] = {}
        for span in spans:
            if span.protect:
                self.masked_at_capture += 1
                continue
            action, replacement, value_id = self._decide(span, span.surface(normalized))
            replacements[span.start] = replacement
            self.findings.append(
                Finding(
                    target=target,
                    field=field_path,
                    label=span.label,
                    engine=span.detector,
                    score=span.score,
                    start=base + span.start,
                    end=base + span.end,
                    action=action,
                    surrogate=replacement,
                    value_id=value_id,
                    seq=seq,
                    absorbed=span.absorbed,
                )
            )

        rewritten = render(normalized, spans, lambda span, _surface: replacements[span.start])
        self._rewritten[text] = rewritten
        return rewritten

    def _decide(self, span: Span, surface: str) -> tuple[str, str, int]:
        """``(action, replacement, value_id)`` for one span, per the policy."""

        action = self._policy.action_for(span.label)
        if action is Action.MASK:
            # `value_id` 0 means "no linkage recorded", which is exactly right
            # for a masked span: there is nothing to link it to.
            return action.value, mask_token(span.label), 0
        surrogate = self._pseudo.surrogate_for(span.label, surface)
        resolved = Action.GENERALIZE.value if surrogate.generalized else action.value
        return resolved, surrogate.text, surrogate.value_id


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------
#: Text files scrubbed alongside ``events.jsonl``. Several of these are written
#: **verbatim** today, with no inline redaction at all, which makes the seal
#: their only control -- ``files/diffs/*.patch`` (a diff hunk is raw file
#: content), ``shell/remote/*`` (remote command output) and ``jobs/*.out``
#: (scheduler output, a high-risk channel).
TEXT_TARGET_GLOBS: tuple[str, ...] = (
    "context/**/*.md",
    "screen/ocr/**/*.txt",
    "files/diffs/**/*.patch",
    "files/diffs/**/*.diff",
    # `**/*`, not `*`: `pull_spool` writes to `shell/remote/<host>/<file>.rec`,
    # so a single-level glob matches only the *host directory*, which
    # `is_file()` then discards -- and every pulled remote file goes unsealed.
    # The pattern that looks obviously right here is the one that silently
    # covers nothing.
    "shell/remote/**/*",
    "jobs/**/*.out",
    "jobs/**/*.err",
)


def iter_text_targets(session_dir: Path) -> Iterator[Path]:
    """Every text file the seal rewrites, de-duplicated and ordered.

    Ordered so ``seal.json``'s per-target digests come out in a stable sequence
    and two seals of one session produce comparable records.
    """

    seen: set[Path] = set()
    for pattern in TEXT_TARGET_GLOBS:
        for path in sorted(session_dir.glob(pattern)):
            if path.is_file() and path not in seen:
                seen.add(path)
                yield path


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------
STATE_PLANNING = "planning"
STATE_STAGING = "staging"
STATE_COMMITTING = "committing"
STATE_COMMITTED = "committed"


def seal_dir(session_dir: Path) -> Path:
    return session_dir / SEAL_DIRNAME


def staged_dir(session_dir: Path) -> Path:
    return seal_dir(session_dir) / "staged"


def journal_path(session_dir: Path) -> Path:
    return seal_dir(session_dir) / JOURNAL_NAME


def marker_path(session_dir: Path) -> Path:
    return session_dir / SEAL_MARKER


def read_journal(session_dir: Path) -> dict[str, Any] | None:
    path = journal_path(session_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A torn journal is indistinguishable from `planning`: nothing has been
        # replaced yet, because the transition to `committing` is written and
        # fsynced before the first os.replace.
        return {"state": STATE_PLANNING, "torn": True}
    return payload if isinstance(payload, dict) else {"state": STATE_PLANNING, "torn": True}


def write_journal(session_dir: Path, payload: dict[str, Any]) -> None:
    """Write and **fsync** the journal.

    The fsync is what makes the state machine trustworthy: the transition to
    ``committing`` has to be durable *before* the first ``os.replace``, or a
    crash between them leaves a partly-replaced session that recovery reads as
    ``planning`` and discards -- losing the seal while keeping the rewrite.
    """

    path = journal_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with tmp.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    except OSError:  # pragma: no cover - not all filesystems allow dir fsync
        pass
    finally:
        os.close(directory)


_RECOVERING: set[str] = set()


def recover(session_dir: Path) -> dict[str, Any] | None:
    """Finish or discard an interrupted seal. Safe to call any number of times.

    Runs at the top of ``wfrec seal``, ``export_session``,
    ``session_bundle._trace_from_session_dir`` and ``EventWriter.__init__``.

    ==============  ==========================================================
    Journal state   Recovery
    ==============  ==========================================================
    ``planning``    delete ``.seal/``; the session is **unchanged**
    ``staging``     delete ``.seal/``; the session is **unchanged**
    ``committing``  **roll forward**: replay ``os.replace`` for every file
                    still under ``staged/``
    ``committed``   write ``seal.json``, delete ``staged/``, done
    ==============  ==========================================================

    The commit loop is idempotent because **the disappearance of a staged file
    *is* its per-file commit record**. Which is exactly why every file must be
    staged before any file is replaced -- see :func:`_commit`.

    Cross-file atomicity is unachievable on POSIX. This gives the *observable*
    equivalent: a mixed state can exist on disk for milliseconds, but while the
    journal says ``committing`` every reader refuses, so it is never consumed.
    """

    key = str(session_dir.resolve())
    if key in _RECOVERING:
        # Re-entrancy guard: recovery constructs readers, and a reader's own
        # __init__ calls recover().
        return None
    journal = read_journal(session_dir)
    if journal is None:
        return None

    _RECOVERING.add(key)
    try:
        state = str(journal.get("state", STATE_PLANNING))
        if state in (STATE_PLANNING, STATE_STAGING):
            shutil.rmtree(seal_dir(session_dir), ignore_errors=True)
            return {"recovered": state, "action": "discarded"}

        if state == STATE_COMMITTING:
            _commit_staged(session_dir, journal)
            journal = {**journal, "state": STATE_COMMITTED}
            write_journal(session_dir, journal)
            state = STATE_COMMITTED

        if state == STATE_COMMITTED:
            record = journal.get("seal_record")
            if isinstance(record, dict):
                marker_path(session_dir).write_text(
                    json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
            shutil.rmtree(seal_dir(session_dir), ignore_errors=True)
            return {"recovered": STATE_COMMITTING, "action": "rolled-forward"}
        return None
    finally:
        _RECOVERING.discard(key)


def guard_readers(session_dir: Path) -> None:
    """Refuse to read a session whose journal is mid-commit.

    Called after :func:`recover`, so reaching this means recovery itself could
    not finish -- which is precisely when reading would produce a half-sealed
    projection.
    """

    journal = read_journal(session_dir)
    if journal and str(journal.get("state")) == STATE_COMMITTING:
        raise SealInProgress(
            f"{session_dir} has a seal in progress; its timeline is mid-commit. "
            "Run `wfrec seal --status` and retry."
        )


def _commit_staged(session_dir: Path, journal: Mapping[str, Any]) -> list[str]:
    """Replay every staged replacement and every recorded deletion.

    Idempotent in both directions: a staged file that is gone has already been
    committed, and a deletion target that is gone has already been deleted.
    """

    committed: list[str] = []
    staged = staged_dir(session_dir)
    for relpath in journal.get("targets", []) or []:
        source = staged / str(relpath)
        if not source.exists():
            continue  # its disappearance IS its commit record
        destination = session_dir / str(relpath)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        committed.append(str(relpath))
    for relpath in journal.get("deletions", []) or []:
        target = session_dir / str(relpath)
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        committed.append(f"-{relpath}")
    return committed


# --------------------------------------------------------------------------
# preconditions
# --------------------------------------------------------------------------
def assert_idle(
    session, *, force: bool = False, stop_session: Callable[[], Any] | None = None
) -> None:
    """Refuse unless the session is genuinely idle, checked **three** ways.

    Any one of them can be stale: ``RecorderState`` is written by whichever
    process last transitioned, the sentinel is a derived projection that a
    crashed daemon leaves behind, and ``manifest.status`` is only as fresh as
    the last ``save()``. Together they are trustworthy enough to justify
    refusing rather than racing.
    """

    from .session import STATUS_ACTIVE, STATUS_PAUSED
    from .state import RecorderState, read_sentinel

    reasons: list[str] = []
    session_id = session.session_id

    try:
        state = RecorderState.load()
    except Exception:  # pragma: no cover - unreadable state is not a green light
        reasons.append("recorder state could not be read")
    else:
        if state.active_session == session_id:
            reasons.append("RecorderState.active_session still names this session")

    try:
        sentinel = read_sentinel()
    except Exception:  # pragma: no cover
        sentinel = None
    if sentinel and sentinel[0] == session_id:
        reasons.append("the shell-hook sentinel still names this session")

    if session.manifest.status in (STATUS_ACTIVE, STATUS_PAUSED):
        reasons.append(f"manifest.status is {session.manifest.status!r}")

    if not reasons:
        return
    if not force:
        raise SessionActive(
            f"session {session_id} is not idle: "
            + "; ".join(reasons)
            + ". Stop it first, or pass --force to stop and then seal. Refusing "
            "rather than racing: the alternative is holding .events.lock across "
            "minutes of inference and stalling every collector thread."
        )

    # `--force` **stops first, then seals**. It does not mean "seal anyway":
    # sealing a live session would race every collector thread, and phase 3's
    # size check would keep failing against a file that is still growing until
    # the tail budget ran out and the whole seal aborted. Stopping is the only
    # thing that actually makes the precondition true.
    if stop_session is None:
        from .client import Client
        from .session import SessionStore

        if Client.discover() is None:
            stop_session = lambda: SessionStore().stop(session_id)
        else:
            raise SessionActive(
                f"--force cannot stop session {session_id} safely from this process. "
                "Stop it through the live recorder first, then seal."
            )
    try:
        stop_session()
    except Exception as exc:
        raise SessionActive(
            f"--force could not stop session {session_id}: {exc}. Refusing to seal a live session."
        ) from exc
    session.manifest = type(session.manifest).from_dict(
        json.loads((session.root / "manifest.json").read_text(encoding="utf-8"))
    )


# --------------------------------------------------------------------------
# the seal
# --------------------------------------------------------------------------
@dataclass
class SealResult:
    record: dict[str, Any]
    findings: int
    targets: list[str]
    reseal: bool = False
    dry_run: bool = False

    @property
    def sealed(self) -> str:
        return str(self.record.get("assurance", "unknown"))


def seal_status(session_dir: Path) -> dict[str, Any]:
    """What ``wfrec seal --status`` prints. Never mutates anything."""

    marker = marker_path(session_dir)
    journal = read_journal(session_dir)
    out: dict[str, Any] = {
        "session_dir": str(session_dir),
        "sealed": marker.exists(),
        "journal": journal.get("state") if journal else None,
    }
    if marker.exists():
        try:
            out["seal"] = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover
            out["seal"] = {"error": "seal.json is unreadable"}
    return out


def is_sealed(session_dir: Path) -> bool:
    return marker_path(session_dir).exists()


def require_sealed(session_dir: Path, *, what: str) -> dict[str, Any]:
    """The hard gate. Used by both export paths.

    Guards the **timeline**, not just the projection: ``session_bundle``
    rebuilds from ``events.jsonl`` live, so gating ``exports/`` alone would be
    trivially bypassable.
    """

    recover(session_dir)
    guard_readers(session_dir)
    marker = marker_path(session_dir)
    if not marker.exists():
        raise NotSealed(
            f"{what} refuses to run on unsealed session {session_dir.name}: "
            "no seal.json. Run `wfrec seal` first. An unsealed session cannot be "
            "exported or ingested, which is what gives fail-closed its teeth."
        )
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NotSealed(f"{marker} is unreadable: {exc}") from exc
    if record.get("assurance") == "partial" and not record.get("accepted_partial"):
        raise NotSealed(
            f"{what} refuses session {session_dir.name}: assurance is `partial`, "
            "so the de-identification pass degraded. A degraded run is never "
            "`verified`; re-seal with a working engine, or accept the partial "
            "seal explicitly."
        )
    _verify_seal_targets(session_dir, record, what=what)
    return record


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_seal_targets(session_dir: Path, record: Mapping[str, Any], *, what: str) -> None:
    targets = record.get("targets")
    if not isinstance(targets, Mapping):
        raise NotSealed(
            f"{what} refuses session {session_dir.name}: seal.json has no target digests."
        )
    expected = {
        "events.jsonl",
        "manifest.json",
        AUDIT_RELPATH,
        *(str(path.relative_to(session_dir)) for path in iter_text_targets(session_dir)),
    }
    recorded = {str(name) for name in targets}
    missing = sorted(expected - recorded)
    unexpected = sorted(recorded - expected)
    if missing or unexpected:
        detail: list[str] = []
        if missing:
            detail.append(f"missing targets: {', '.join(missing)}")
        if unexpected:
            detail.append(f"unexpected targets: {', '.join(unexpected)}")
        raise NotSealed(
            f"{what} refuses session {session_dir.name}: seal target set does not match the session "
            f"({'; '.join(detail)})."
        )
    for name, digests in targets.items():
        path = session_dir / str(name)
        if not path.is_file():
            raise NotSealed(
                f"{what} refuses session {session_dir.name}: sealed target missing: {name}"
            )
        if not isinstance(digests, Mapping):
            raise NotSealed(
                f"{what} refuses session {session_dir.name}: invalid digest entry for {name}"
            )
        expected_digest = str(digests.get("after_sha256") or "")
        if not expected_digest:
            raise NotSealed(
                f"{what} refuses session {session_dir.name}: target {name} has no after_sha256"
            )
        actual_digest = _sha256_file(path)
        if actual_digest != expected_digest:
            raise NotSealed(
                f"{what} refuses session {session_dir.name}: {name} no longer matches its sealed digest."
            )


_CREDENTIAL_PATTERNS = tuple(pattern for _name, pattern in CREDENTIAL_SHAPES)


def _scrub_secrets(text: str) -> str:
    """Last-ditch guard on the audit writer.

    The audit should never see a credential -- it records labels, offsets and
    surrogates, not content. This exists because "should never" is not a
    control, and ``tests/test_deid_audit.py`` checks the whole session tree for
    planted keys rather than trusting this function.
    """

    out = text
    for pattern in _CREDENTIAL_PATTERNS:
        out = pattern.sub("[REDACTED_CREDENTIAL]", out)
    return out


def _scrub_manifest(session, scrubber: Scrubber, policy: Policy) -> dict[str, Any]:
    payload = json.loads((session.root / "manifest.json").read_text(encoding="utf-8"))
    for key in ("title", "workflow_family"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            payload[key] = scrubber.rewrite_field(
                value,
                path_field=False,
                target="manifest.json",
                field_path=key,
                seq=None,
            )
    if isinstance(payload.get("tags"), list):
        payload["tags"] = [
            scrubber.rewrite_field(
                tag,
                path_field=False,
                target="manifest.json",
                field_path=f"tags[{index}]",
                seq=None,
            )
            if isinstance(tag, str) and tag
            else tag
            for index, tag in enumerate(payload["tags"])
        ]
    if isinstance(payload.get("watch_roots"), list):
        payload["watch_roots"] = [
            scrubber.rewrite_field(
                root,
                path_field=True,
                target="manifest.json",
                field_path=f"watch_roots[{index}]",
                seq=None,
            )
            if isinstance(root, str) and root
            else root
            for index, root in enumerate(payload["watch_roots"])
        ]
    if policy.pseudonymize_analyst:
        for key in ("analyst", "host"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                payload[key] = _rewrite_workforce_value(scrubber, key, value)
    return payload


def _rewrite_workforce_value(scrubber: Scrubber, key: str, value: str) -> str:
    label = "NAME" if key == "analyst" else "DEVICE"
    return scrubber._pseudo.surrogate_for(label, value).text


def seal_session(
    session,
    *,
    detectors: Sequence[Detector] = (),
    deny_terms: Sequence[str] = ("patient", "diagnosis", "pathology"),
    policy: Policy | None = None,
    allowlist: Allowlist | None = None,
    engine_label: str = "regex",
    reseal: bool = False,
    force: bool = False,
    dry_run: bool = False,
    degraded: bool = False,
    degraded_reasons: Sequence[str] = (),
    key: bytes | None = None,
    stop_session: Callable[[], Any] | None = None,
) -> SealResult:
    """Seal ``session``. The three-phase commit, start to finish.

    ``detectors`` *adds* model tiers; ``surrogate_guard`` and ``regex_rules``
    always run and no flag substitutes for them.

    A ``dry_run`` performs every detection and stages nothing, so the numbers
    are real and the session is untouched.
    """

    session_dir: Path = session.root
    policy = policy or Policy(render=RenderMode.PSEUDONYMIZE)

    recover(session_dir)
    guard_readers(session_dir)

    existing = marker_path(session_dir)
    if existing.exists() and not reseal:
        return SealResult(
            record=json.loads(existing.read_text(encoding="utf-8")),
            findings=0,
            targets=[],
            reseal=False,
        )

    generation = 1
    if existing.exists():
        try:
            generation = (
                int(json.loads(existing.read_text(encoding="utf-8")).get("generation", 1)) + 1
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):  # pragma: no cover
            generation = 2

    assert_idle(session, force=force, stop_session=stop_session)

    started = time.perf_counter()
    events_path = session_dir / "events.jsonl"
    lock_path = session_dir / ".events.lock"

    # ---------------------------------------------------------------- phase 1
    # Snapshot under `.events.lock`, for milliseconds.
    with file_lock(lock_path):
        events = read_events(events_path)
        snapshot_bytes = events_path.stat().st_size if events_path.exists() else 0
        sealed_through_seq = max((event.seq or 0 for event in events), default=0)

    pseudonymizer = Pseudonymizer(key=key)
    try:
        scrubber = Scrubber(
            pseudonymizer=pseudonymizer,
            detectors=detectors,
            deny_terms=deny_terms,
            allowlist=allowlist,
            policy=policy,
        )

        journal: dict[str, Any] = {
            "state": STATE_PLANNING,
            "session": session.session_id,
            "started_at": utc_now(),
            "snapshot_bytes": snapshot_bytes,
            "sealed_through_seq": sealed_through_seq,
            "generation": generation,
            "targets": [],
            "deletions": [],
        }
        if not dry_run:
            write_journal(session_dir, journal)

        # ------------------------------------------------------------ phase 2
        # Scrub. `.seal/lock` only -- never `.events.lock`.
        seal_lock = seal_dir(session_dir) / "lock"
        text_targets = list(iter_text_targets(session_dir))

        with file_lock(seal_lock):
            atoms: list[Atom] = []
            for event in events:
                transform_strings(
                    event.payload,
                    lambda _path, text, is_path: atoms.extend(atoms_of(text, is_path)) or text,
                    skip_workforce=not policy.pseudonymize_analyst,
                )
            file_texts: dict[Path, str] = {}
            for path in text_targets:
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                file_texts[path] = content
                atoms.append(Atom(text=content))

            scrubber.prepare(atoms)

            staged_events, changed_events = _scrub_events(events, scrubber, policy)
            staged_manifest = _scrub_manifest(session, scrubber, policy)
            staged_files = {
                path: scrubber.rewrite_field(
                    content,
                    path_field=False,
                    target=str(path.relative_to(session_dir)),
                    field_path="<file>",
                    seq=None,
                )
                for path, content in file_texts.items()
            }

            if dry_run:
                dry_record = _build_record(
                    session=session,
                    scrubber=scrubber,
                    pseudonymizer=pseudonymizer,
                    policy=policy,
                    engine_label=engine_label,
                    generation=generation,
                    sealed_through_seq=sealed_through_seq,
                    targets={},
                    duration=time.perf_counter() - started,
                    degraded=degraded,
                    degraded_reasons=degraded_reasons,
                    deny_terms=deny_terms,
                )
                dry_targets = sorted(str(path.relative_to(session_dir)) for path in staged_files)
                # `file_lock` creates `.seal/lock` as a side effect, so even a
                # dry run leaves a directory behind unless it is cleaned up
                # explicitly. "Touches nothing" has to mean nothing, or the next
                # reader sees a `.seal/` and has to reason about whether a seal
                # is in flight.
                _dry_cleanup = True
            else:
                _dry_cleanup = False

            if not dry_run:
                journal["state"] = STATE_STAGING
                write_journal(session_dir, journal)

            if dry_run:
                # Detection has already run and every finding is real; staging
                # is the only thing skipped, which is what makes a dry run's
                # numbers trustworthy and its side effects nil.
                targets = {}
            else:
                targets = _stage_all(
                    session_dir=session_dir,
                    session=session,
                    scrubber=scrubber,
                    pseudonymizer=pseudonymizer,
                    policy=policy,
                    engine_label=engine_label,
                    generation=generation,
                    sealed_through_seq=sealed_through_seq,
                    staged_events=staged_events,
                    staged_manifest=staged_manifest,
                    staged_files=staged_files,
                    events_path=events_path,
                )
            journal["targets"] = sorted(targets)

        if dry_run:
            shutil.rmtree(seal_dir(session_dir), ignore_errors=True)
            return SealResult(
                record=dry_record,
                findings=len(scrubber.findings),
                targets=dry_targets,
                reseal=generation > 1,
                dry_run=True,
            )

        # ------------------------------------------------------------ phase 3
        # Commit under `.events.lock`, for milliseconds.
        record = _commit(
            session_dir,
            journal,
            targets=targets,
            scrubber=scrubber,
            session=session,
            pseudonymizer=pseudonymizer,
            policy=policy,
            engine_label=engine_label,
            generation=generation,
            events_path=events_path,
            snapshot_bytes=snapshot_bytes,
            started=started,
            degraded=degraded,
            degraded_reasons=degraded_reasons,
            deny_terms=deny_terms,
        )

        return SealResult(
            record=record,
            findings=len(scrubber.findings),
            targets=sorted(journal["targets"]),
            reseal=generation > 1,
        )
    finally:
        # Zeroed in place whatever happened, including on the abort paths.
        pseudonymizer.close()


def _scrub_events(
    events: Sequence[Event], scrubber: Scrubber, policy: Policy
) -> tuple[list[dict[str, Any]], int]:
    staged: list[dict[str, Any]] = []
    changed = 0
    for event in events:
        before = event.payload
        after = transform_strings(
            before,
            lambda field_path, text, is_path: scrubber.rewrite_field(
                text,
                path_field=is_path,
                target="events.jsonl",
                field_path=f"payload.{field_path}",
                seq=event.seq,
            ),
            skip_workforce=not policy.pseudonymize_analyst,
        )
        if after != before:
            changed += 1
        payload = event.to_dict()
        payload["payload"] = after
        if policy.pseudonymize_analyst:
            for key in ("host", "analyst"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    payload[key] = _rewrite_workforce_value(scrubber, key, value)
        staged.append(payload)
    return staged, changed


def _write_audit(session_dir: Path, staged_root: Path, scrubber: Scrubber) -> None:
    audit = staged_root / AUDIT_RELPATH
    audit.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        _scrub_secrets(json.dumps(finding.to_dict(), sort_keys=True, ensure_ascii=False))
        for finding in scrubber.findings
    ]
    audit.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _commit(
    session_dir: Path,
    journal: dict[str, Any],
    *,
    targets: dict[str, dict[str, str]],
    scrubber: Scrubber,
    session,
    pseudonymizer: Pseudonymizer,
    policy: Policy,
    engine_label: str,
    generation: int,
    events_path: Path,
    snapshot_bytes: int,
    started: float,
    degraded: bool,
    degraded_reasons: Sequence[str],
    deny_terms: Sequence[str],
) -> dict[str, Any]:
    """Phase 3. Verify the size, then replace. Bounded tail catch-up.

    A size mismatch means a straggler appended after the snapshot -- a shell
    hook whose spool record the daemon ingested a second late, say. Rather than
    abandoning the whole seal, read the tail beyond ``snapshot_bytes``, scrub it
    synchronously and append it to the staged file, then retry. Bounded at
    :data:`MAX_TAIL_RETRIES` retries or :data:`MAX_TAIL_EVENTS` events, then
    abort with **nothing committed** -- an unbounded catch-up loop against a
    session that is still recording would never terminate.
    """

    staged_timeline = staged_dir(session_dir) / "events.jsonl"
    tail_events_seen = 0

    for attempt in range(MAX_TAIL_RETRIES + 1):
        with file_lock(session_dir / ".events.lock"):
            current = events_path.stat().st_size if events_path.exists() else 0
            if current == snapshot_bytes:
                digest = _finalize_staged_timeline(
                    staged_timeline,
                    sealed_through_seq=int(journal["sealed_through_seq"]),
                    findings=len(scrubber.findings),
                    distinct_values=pseudonymizer.distinct_values,
                )
                targets["events.jsonl"] = {
                    "before_sha256": _sha256_file(events_path) if events_path.exists() else "",
                    "after_sha256": digest,
                }
                record = _build_record(
                    session=session,
                    scrubber=scrubber,
                    pseudonymizer=pseudonymizer,
                    policy=policy,
                    engine_label=engine_label,
                    generation=generation,
                    sealed_through_seq=int(journal["sealed_through_seq"]),
                    targets=targets,
                    duration=time.perf_counter() - started,
                    degraded=degraded,
                    degraded_reasons=degraded_reasons,
                    deny_terms=deny_terms,
                )
                journal["seal_record"] = record
                journal["state"] = STATE_COMMITTING
                journal["targets"] = sorted(journal.get("targets") or [])
                write_journal(session_dir, journal)
                _commit_staged(session_dir, journal)
                journal["state"] = STATE_COMMITTED
                write_journal(session_dir, journal)
                marker_path(session_dir).write_text(
                    json.dumps(journal["seal_record"], indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                _bump_seq_locked(session_dir, journal)
                shutil.rmtree(seal_dir(session_dir), ignore_errors=True)
                return record

            tail = _read_tail(events_path, snapshot_bytes)

        if attempt == MAX_TAIL_RETRIES:
            break
        tail_events_seen += len(tail)
        if tail_events_seen > MAX_TAIL_EVENTS:
            break

        scrubbed_tail, _changed = _scrub_events(tail, scrubber, policy)
        with staged_timeline.open("a", encoding="utf-8") as handle:
            for payload in scrubbed_tail:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        snapshot_bytes = current
        journal["snapshot_bytes"] = snapshot_bytes
        journal["sealed_through_seq"] = max(
            int(journal["sealed_through_seq"]),
            max((event.seq or 0 for event in tail), default=0),
        )
        _write_audit(session_dir, staged_dir(session_dir), scrubber)

    shutil.rmtree(seal_dir(session_dir), ignore_errors=True)
    raise SealAborted(
        f"{events_path.name} kept growing during the seal "
        f"({tail_events_seen} tail events over {MAX_TAIL_RETRIES} retries). "
        "Nothing was committed and the session is unchanged. Stop the session "
        "and seal again."
    )


def _finalize_staged_timeline(
    staged_timeline: Path, *, sealed_through_seq: int, findings: int, distinct_values: int
) -> str:
    payloads: list[dict[str, Any]] = []
    sealed: dict[str, Any] | None = None
    for line in staged_timeline.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("type") == DEID_SEALED:
            sealed = payload
            continue
        payloads.append(payload)
    if sealed is None:
        raise SealAborted(f"{staged_timeline} is missing its staged {DEID_SEALED} event.")
    sealed["seq"] = sealed_through_seq + 1
    if isinstance(sealed.get("payload"), dict):
        sealed["payload"]["sealed_through_seq"] = sealed_through_seq
        sealed["payload"]["findings"] = findings
        sealed["payload"]["distinct_values"] = distinct_values
    staged_timeline.write_text(
        "".join(
            json.dumps(payload, ensure_ascii=False, default=str) + "\n"
            for payload in [*payloads, sealed]
        ),
        encoding="utf-8",
    )
    return _sha256_file(staged_timeline)


def _read_tail(events_path: Path, offset: int) -> list[Event]:
    """Events appended past ``offset``. Caller holds ``.events.lock``."""

    if not events_path.exists():
        return []
    with events_path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        payloads = handle.read()
    out: list[Event] = []
    for line in payloads.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Event.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def _bump_seq_locked(session_dir: Path, journal: Mapping[str, Any]) -> None:
    """Reserve the sequence number the ``deid.sealed`` event consumed.

    Without this a reseal -- the only writer a sealed session permits -- would
    mint a duplicate ``seq``, and ``seq`` is the stable anchor a downstream
    skill cites.
    """

    seq_path = session_dir / ".seq"
    consumed = int(journal.get("sealed_through_seq", 0)) + 1
    try:
        current = int(seq_path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        current = 0
    if consumed > current:
        seq_path.write_text(str(consumed), encoding="utf-8")


def _build_record(
    *,
    session,
    scrubber: Scrubber,
    pseudonymizer: Pseudonymizer,
    policy: Policy,
    engine_label: str,
    generation: int,
    sealed_through_seq: int,
    targets: Mapping[str, Mapping[str, str]],
    duration: float,
    degraded: bool,
    degraded_reasons: Sequence[str],
    deny_terms: Sequence[str],
) -> dict[str, Any]:
    """``seal.json``: the marker **and** the integrity chain."""

    counts: dict[str, int] = {}
    for finding in scrubber.findings:
        counts[finding.label] = counts.get(finding.label, 0) + 1

    model_tier = any(info.get("kind") == "model" for info in scrubber.engines())
    return {
        "schema": 1,
        "session": session.session_id,
        "sealed_at": utc_now(),
        "profile": policy.profile,
        "assurance": assurance_for(policy.profile, model_tier=model_tier, degraded=degraded),
        "degraded_reasons": list(degraded_reasons),
        "generation": generation,
        "engine": engine_label,
        "engines": scrubber.engines(),
        "deny_terms": len(tuple(deny_terms)),
        "sealed_through_seq": sealed_through_seq,
        "findings": len(scrubber.findings),
        "counts_by_label": dict(sorted(counts.items())),
        "masked_at_capture": scrubber.masked_at_capture,
        "distinct_values": pseudonymizer.distinct_values,
        "collisions": pseudonymizer.collisions,
        "targets": {name: dict(value) for name, value in sorted(targets.items())},
        "render_mode": policy.render.value,
        "box_precision": policy.box_precision,
        "pseudonymize_analyst": policy.pseudonymize_analyst,
        # Stated, and tested: tests/test_deid_audit.py walks the whole session
        # tree asserting no planted value and no pseudonyms.json anywhere.
        "pseudonym_key": "discarded",
        "reverse_map": "not-written",
        "duration_seconds": round(duration, 3),
        "generalized_age_token": GENERALIZED_AGE,
        "policy_digest": table_digest(
            [
                (policy.profile, policy.render.value, str(policy.pseudonymize_analyst)),
                tuple(sorted(str(term) for term in deny_terms)) or ("",),
            ]
        ),
    }


def _stage_all(
    *,
    session_dir: Path,
    session,
    scrubber: Scrubber,
    pseudonymizer: Pseudonymizer,
    policy: Policy,
    engine_label: str,
    generation: int,
    sealed_through_seq: int,
    staged_events: Sequence[dict[str, Any]],
    staged_manifest: Mapping[str, Any],
    staged_files: Mapping[Path, str],
    events_path: Path,
) -> dict[str, dict[str, str]]:
    """Write every rewritten file under ``.seal/staged/``, and nothing else.

    **Every file must be staged before any file is replaced.** The commit loop
    is idempotent precisely because the disappearance of a staged file *is* its
    per-file commit record -- so a file that has not been staged yet cannot be
    distinguished from one already committed, and a crash mid-staging would make
    roll-forward ambiguous.
    """

    staged_root = staged_dir(session_dir)
    staged_root.mkdir(parents=True, exist_ok=True)
    targets: dict[str, dict[str, str]] = {}

    for path, content in staged_files.items():
        relpath = str(path.relative_to(session_dir))
        destination = staged_root / relpath
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        targets[relpath] = {
            "before_sha256": _sha256_file(path),
            "after_sha256": _sha256_file(destination),
        }

    staged_manifest_path = staged_root / "manifest.json"
    staged_manifest_path.write_text(json.dumps(staged_manifest, indent=2) + "\n", encoding="utf-8")
    targets["manifest.json"] = {
        "before_sha256": _sha256_file(session_dir / "manifest.json"),
        "after_sha256": _sha256_file(staged_manifest_path),
    }

    # The `deid.sealed` event is the **last line of the staged events.jsonl,
    # before hashing**, so the timeline self-documents and `after_sha256` covers
    # the statement rather than stopping just short of it.
    sealed_event = Event(
        source="deid",
        type=DEID_SEALED,
        payload={
            "generation": generation,
            "engine": engine_label,
            "profile": policy.profile,
            "findings": len(scrubber.findings),
            "distinct_values": pseudonymizer.distinct_values,
            "sealed_through_seq": sealed_through_seq,
        },
        session=session.session_id,
        host=(
            _rewrite_workforce_value(scrubber, "host", session.manifest.host)
            if policy.pseudonymize_analyst and session.manifest.host
            else session.manifest.host
        ),
        analyst=(
            _rewrite_workforce_value(scrubber, "analyst", session.manifest.analyst)
            if policy.pseudonymize_analyst and session.manifest.analyst
            else session.manifest.analyst
        ),
        seq=sealed_through_seq + 1,
    )
    staged_timeline = staged_root / "events.jsonl"
    staged_timeline.parent.mkdir(parents=True, exist_ok=True)
    staged_timeline.write_text(
        "".join(
            json.dumps(payload, ensure_ascii=False, default=str) + "\n"
            for payload in [*staged_events, sealed_event.to_dict()]
        ),
        encoding="utf-8",
    )
    targets["events.jsonl"] = {
        "before_sha256": _sha256_file(events_path) if events_path.exists() else "",
        "after_sha256": _sha256_file(staged_timeline),
    }

    _write_audit(session_dir, staged_root, scrubber)
    # The audit is a commit target like any other: staged first, then replaced
    # under the same journal. Leaving it out of `targets` is how it ends up
    # written into `.seal/staged/` and then deleted along with the rest of the
    # scratch directory -- a seal with no audit trail.
    targets[AUDIT_RELPATH] = {
        "before_sha256": (
            _sha256_file(session_dir / AUDIT_RELPATH)
            if (session_dir / AUDIT_RELPATH).exists()
            else ""
        ),
        "after_sha256": _sha256_file(staged_root / AUDIT_RELPATH),
    }
    return targets
