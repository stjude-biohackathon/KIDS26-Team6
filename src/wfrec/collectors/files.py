"""File-change collector: watchdog as the trigger, git as the source of truth.

The ordering here is the whole design. ``watchdog`` tells us *when* something
changed; ``git`` tells us *what* changed. Relying on watchdog for correctness
would be a mistake on every platform -- Windows silently drops events when its
64 KB completion buffer overflows, macOS reports atomic saves as
create-plus-delete, and Linux inotify does not fire at all on NFS or Lustre,
which is exactly where HPC scratch lives.

Genomics safety
---------------
A single BAM or FASTQ can be 100 GB. Every path runs a gate chain before
anything opens it: extension denylist, then a size gate, then a binary sniff,
then a diff-size cap. Critically, ``git status`` is always invoked with
``--no-optional-locks``: without it git refreshes the index, which rehashes any
modified tracked file, so one touched 100 GB BAM would make git read all
100 GB and hang the machine for twenty minutes.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections import deque
from pathlib import Path
from threading import Lock

from .base import Collector, CollectorStatus, Degraded
from ..events import FILE_CHANGED, FILE_DIFF, FILE_FLOOD, GIT_SNAPSHOT, Event
from ..redaction import shared as shared_redactor


def _require_redactor():
    redactor = shared_redactor()
    if not redactor.available:
        raise Degraded(
            "deid-unavailable",
            redactor.error or "shared redactor is unavailable",
        )
    return redactor


def _redact_path_entries(
    entries: list[dict[str, str]], redactor=None
) -> tuple[list[dict[str, str]], list[str]]:
    """Redact the ``path`` field of each git-status / numstat entry.

    Only ``path`` -- the other fields are counts and status codes, and rewriting
    them would break the exporters that read them.
    """

    redactor = redactor or _require_redactor()
    out: list[dict[str, str]] = []
    findings: set[str] = set()
    for entry in entries:
        item = dict(entry)
        if isinstance(item.get("path"), str):
            redacted = redactor.apply(item["path"])
            item["path"] = redacted.text
            findings.update(redacted.findings)
        out.append(item)
    return out, sorted(findings)

#: Extensions never read, only recorded as metadata. Seeded from the repo's own
#: .gitignore and extended with the rest of the common genomics binary formats.
BINARY_EXTENSIONS = frozenset(
    {
        ".bam", ".bai", ".cram", ".crai", ".sam", ".bcf", ".vcf", ".gvcf",
        ".tbi", ".csi", ".fastq", ".fq", ".fasta", ".fa", ".fai", ".2bit",
        ".bw", ".bigwig", ".bedgraph", ".loom", ".mtx", ".h5", ".h5ad",
        ".hdf5", ".npy", ".npz", ".parquet", ".pt", ".ckpt", ".sra",
        ".tar", ".gz", ".bz2", ".xz", ".zip", ".zst", ".pyc", ".so", ".dylib",
        ".dll", ".o", ".a", ".png", ".jpg", ".jpeg", ".pdf", ".mp4", ".webp",
    }
)

#: Directory names pruned entirely. Walking these is how you exhaust inotify
#: watches and how a Nextflow run produces a million events.
PRUNE_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
        "envs", ".conda", ".snakemake", ".nextflow", "work", ".terraform",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".ipynb_checkpoints",
        ".zarr", "site-packages", ".tox", ".eggs", "build", "dist",
    }
)

METADATA_ONLY_BYTES = 10 * 1024 * 1024   # above this we never open the file
DIFF_MAX_BYTES = 256 * 1024              # above this we do not attempt a diff
DIFF_OUTPUT_MAX = 64 * 1024              # truncate a diff larger than this
TRACKED_BLOB_REFUSE = 100 * 1024 * 1024  # skip git entirely if tracked blobs exceed
FLOOD_QUEUE_MAX = 10000
DEBOUNCE_SECONDS = 1.0
GIT_MIN_INTERVAL = 5.0
SAFE_DIFF_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: Pathspec exclusions handed to git. Only the CLI supports this magic syntax,
#: which is the main reason this module shells out instead of using pygit2.
GIT_EXCLUDES = tuple(
    f":(exclude,glob)**/*{ext}"
    for ext in (".bam", ".cram", ".sam", ".fastq", ".fq", ".fastq.gz", ".vcf.gz", ".h5", ".parquet")
)


def is_binary_path(path: Path) -> bool:
    suffixes = [s.lower() for s in path.suffixes[-2:]]
    return any(s in BINARY_EXTENSIONS for s in suffixes)


def looks_binary(path: Path) -> bool:
    """Sniff for a NUL byte in the first 8 KB, git's own heuristic."""

    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(8192)
    except OSError:
        return True


def run_git(root: Path, *args: str, timeout: float = 20.0) -> tuple[int, str]:
    """Run a git command in ``root``, returning ``(returncode, stdout)``.

    ``--no-optional-locks`` is not optional here: see the module docstring.
    """

    cmd = ["git", "--no-optional-locks", "-C", str(root), *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        return proc.returncode, proc.stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def is_git_repo(root: Path) -> bool:
    code, out = run_git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def has_huge_tracked_blobs(root: Path) -> bool:
    """Whether the repo tracks blobs big enough to make git operations unsafe."""

    code, out = run_git(root, "ls-files", "-s", "--", ".")
    if code != 0:
        return False
    # Checking on-disk size of tracked paths is cheaper and good enough; a
    # tracked 100 GB BAM is the case we must refuse, and it will be present in
    # the worktree.
    for line in out.splitlines()[:5000]:
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        candidate = root / parts[1]
        try:
            if candidate.is_file() and candidate.stat().st_size > TRACKED_BLOB_REFUSE:
                return True
        except OSError:
            continue
    return False


def network_filesystem(path: Path) -> str | None:
    """Return the filesystem type if ``path`` is on a network mount.

    inotify delivers nothing on NFS, Lustre or GPFS -- silently -- so a
    declared root on HPC scratch must fall back to polling rather than appear
    to work.
    """

    if not Path("/proc/mounts").exists():
        return None
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8")
    except OSError:
        return None
    resolved = str(path.resolve())
    best: tuple[int, str] | None = None
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point, fstype = parts[1], parts[2]
        if resolved == mount_point or resolved.startswith(mount_point.rstrip("/") + "/"):
            if best is None or len(mount_point) > best[0]:
                best = (len(mount_point), fstype)
    if best and best[1] in {"nfs", "nfs4", "lustre", "gpfs", "cifs", "smb3", "fuse.sshfs"}:
        return best[1]
    return None


class FileCollector(Collector):
    """Watches declared roots and records git-verified changes."""

    source = "files"
    interval = 1.0

    def __init__(
        self,
        session,
        roots: list[Path] | None = None,
        *,
        initial_trigger: str = "session-start",
    ) -> None:
        super().__init__(session)
        #: Label for the snapshot taken when this collector starts. Resume
        #: passes "resume" so the timeline shows one snapshot for the resume
        #: rather than a "session-start" plus a "resume" for the same instant.
        self._initial_trigger = initial_trigger
        self._roots = [Path(r).expanduser().resolve() for r in (roots or [])]
        self._observer = None
        self._pending: dict[Path, float] = {}
        self._lock = Lock()
        self._dropped = 0
        self._queue: deque[tuple[Path, str]] = deque(maxlen=FLOOD_QUEUE_MAX)
        self._last_git: dict[Path, float] = {}
        self._poll_roots: list[Path] = []
        self._git_roots: list[Path] = []
        self._refused: dict[str, str] = {}
        self._network_roots: dict[str, str] = {}
        self._head: dict[Path, str] = {}
        #: Last (op, size, mtime) emitted per path, so a single editor save --
        #: which arrives as create + truncate + write + close -- yields one
        #: event rather than four identical ones.
        self._last_emitted: dict[Path, tuple[str, int | None, int | None]] = {}

    # ------------------------------------------------------------------ probe
    def probe(self) -> CollectorStatus:
        if not self._roots:
            raise Degraded(
                "no-watch-roots",
                "No project roots declared. Use `wfrec watch <dir>` to add one.",
            )
        _require_redactor()

        backend = "polling"
        try:
            from watchdog.observers import Observer  # noqa: F401

            backend = "watchdog"
        except ImportError:
            backend = "polling"

        for root in self._roots:
            # Network mounts get polled regardless of backend: inotify reports
            # nothing at all on NFS/Lustre, and does so silently.
            fstype = network_filesystem(root)
            if fstype or backend == "polling":
                self._poll_roots.append(root)
            if fstype:
                self._network_roots[str(root)] = fstype
                continue
            if is_git_repo(root):
                if has_huge_tracked_blobs(root):
                    # Refusing is the safe answer: a git operation here would
                    # rehash a multi-gigabyte tracked blob.
                    self._refused[str(root)] = "tracked-blob-over-100mb"
                else:
                    self._git_roots.append(root)

        return CollectorStatus(
            source=self.source,
            available=True,
            backend=backend,
            extra={
                "roots": [str(r) for r in self._roots],
                "git_roots": [str(r) for r in self._git_roots],
                "poll_roots": [str(r) for r in self._poll_roots],
                "network_roots": self._network_roots,
                "refused": self._refused,
            },
        )

    # -------------------------------------------------------------- lifecycle
    def start(self) -> CollectorStatus:
        status = super().start()
        if status.available and status.backend == "watchdog":
            self._start_observer()
        if status.available:
            for root in self._git_roots:
                self._git_snapshot(root, trigger=self._initial_trigger, force=True)
        return status

    def _start_observer(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:  # pragma: no cover
            return

        collector = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:  # type: ignore[no-untyped-def]
                if event.is_directory:
                    return
                collector._enqueue(Path(event.src_path), event.event_type)

        self._observer = Observer()
        handler = Handler()
        for root in self._roots:
            if str(root) in self._refused:
                continue
            try:
                self._observer.schedule(handler, str(root), recursive=True)
            except OSError as exc:
                # inotify watch exhaustion is the common case here.
                self.session.record(
                    "collector.error",
                    source=self.source,
                    payload={"root": str(root), "error": str(exc)},
                )
        try:
            self._observer.start()
        except Exception as exc:  # pragma: no cover - defensive
            self.session.record(
                "collector.error", source=self.source, payload={"error": str(exc)}
            )
            self._observer = None

    def teardown(self) -> None:
        # Flush a final snapshot: whatever the analyst changed just before
        # stopping is the most interesting part of the session. Skipped when
        # the Recorder already snapshotted moments ago for pause/resume, which
        # would otherwise emit the same diff two or three times.
        try:
            recent = time.time() - max(self._last_git.values(), default=0.0)
            if recent > 2.0:
                self.snapshot_all("session-stop")
        except Exception:  # pragma: no cover - defensive
            pass
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:  # pragma: no cover
                pass
            self._observer = None

    # ----------------------------------------------------------------- queue
    def _enqueue(self, path: Path, kind: str) -> None:
        """Record an interesting path, dropping noise before it costs anything."""

        if self._should_prune(path):
            return
        with self._lock:
            if len(self._queue) == self._queue.maxlen:
                self._dropped += 1
            self._queue.append((path, kind))
            self._pending[path] = time.time()

    def _should_prune(self, path: Path) -> bool:
        parts = set(path.parts)
        if parts & PRUNE_DIRS:
            return True
        name = path.name
        if name.endswith("~") or name.startswith(".#"):
            return True
        # Editor and tool scratch files: recording them adds noise and their
        # content is already captured via the real file's diff.
        if name.endswith(".swp") or name.endswith(".tmp"):
            return True
        return False

    # ------------------------------------------------------------------ loop
    def _run_once(self) -> None:
        now = time.time()
        with self._lock:
            dropped, self._dropped = self._dropped, 0
            ready = [p for p, seen in self._pending.items() if now - seen >= DEBOUNCE_SECONDS]
            for path in ready:
                self._pending.pop(path, None)
            self._queue.clear()

        events: list[Event] = []
        if dropped:
            # A flood means a tar extract, conda env or Nextflow work dir. Say
            # so explicitly and let the git snapshot summarize instead.
            events.append(
                Event(
                    source=self.source,
                    type=FILE_FLOOD,
                    payload={"dropped": dropped, "queue_max": FLOOD_QUEUE_MAX},
                )
            )

        for path in ready:
            event = self._describe(path)
            if event is not None:
                events.append(event)
        self.emit(events)

        if ready or dropped:
            for root in self._git_roots:
                self._git_snapshot(root, trigger="change")

    def _describe(self, path: Path) -> Event | None:
        """Build a ``file.changed`` event, gated by size and type."""

        try:
            exists = path.exists()
            size = path.stat().st_size if exists else None
        except OSError:
            exists, size = False, None

        try:
            mtime = int(path.stat().st_mtime_ns) if exists else None
        except OSError:
            mtime = None

        # The path is redacted inline. A filename is the dominant identifier
        # surface in this domain -- `/data/proj/SJALL018/smith_jane_R1.fastq.gz`
        # carries a subject id *and* a patient name -- and until now it was
        # written verbatim, which made the seal its only control. Defence in
        # depth: an unsealed session folder is still zippable and shareable even
        # though it cannot be exported.
        redactor = _require_redactor()
        redacted_path = redactor.apply(str(path))
        payload: dict[str, object] = {
            "path": redacted_path.text,
            "exists": exists,
            "size": size,
        }
        findings = list(redacted_path.findings)
        op = "deleted" if not exists else "modified"
        fingerprint = (op, size, mtime)
        if self._last_emitted.get(path) == fingerprint:
            return None
        self._last_emitted[path] = fingerprint

        if not exists:
            payload["op"] = "deleted"
            return Event(
                source=self.source,
                type=FILE_CHANGED,
                payload=payload,
                redactions=findings,
            )

        payload["op"] = "modified"
        if is_binary_path(path):
            payload["content"] = "skipped-binary-extension"
        elif size is not None and size > METADATA_ONLY_BYTES:
            payload["content"] = "skipped-too-large"
        elif looks_binary(path):
            payload["content"] = "skipped-binary-content"
        else:
            payload["content"] = "text"
        return Event(
            source=self.source, type=FILE_CHANGED, payload=payload, redactions=findings
        )

    # -------------------------------------------------------------------- git
    def _git_snapshot(self, root: Path, *, trigger: str, force: bool = False) -> None:
        """Record git's view of a root: HEAD, porcelain status, and diffs.

        ``force`` bypasses the rate limit. Rate limiting is right for
        change-triggered snapshots -- one `git status` per root per 5s -- but
        wrong at stop, pause and resume, where losing the final state would
        mean losing the diffs the session exists to capture.
        """

        if not force and time.time() - self._last_git.get(root, 0.0) < GIT_MIN_INTERVAL:
            return
        self._last_git[root] = time.time()
        code, head = run_git(root, "rev-parse", "HEAD")
        head = head.strip() if code == 0 else ""
        code, branch = run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        branch = branch.strip() if code == 0 else ""
        redactor = _require_redactor()
        redacted_root = redactor.apply(str(root))
        redacted_branch = redactor.apply(branch)

        code, status_out = run_git(
            root, "status", "--porcelain=v1", "-uall", "--", ".", *GIT_EXCLUDES
        )
        entries: list[dict[str, str]] = []
        if code == 0:
            for line in status_out.splitlines():
                if len(line) < 4:
                    continue
                entries.append({"status": line[:2].strip(), "path": line[3:]})

        code, numstat = run_git(
            root, "diff", "--no-ext-diff", "--no-color", "--numstat", "--", ".", *GIT_EXCLUDES
        )
        stats: list[dict[str, str]] = []
        if code == 0:
            for line in numstat.splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    stats.append(
                        {"added": parts[0], "deleted": parts[1], "path": parts[2]}
                    )
        redacted_changed, changed_findings = _redact_path_entries(entries[:200], redactor)
        redacted_numstat, numstat_findings = _redact_path_entries(stats[:200], redactor)
        findings = sorted(
            set(redacted_root.findings)
            | set(redacted_branch.findings)
            | set(changed_findings)
            | set(numstat_findings)
        )

        self.session.writer.append(
            Event(
                source=self.source,
                type=GIT_SNAPSHOT,
                payload={
                    "root": redacted_root.text,
                    "head": head,
                    "branch": redacted_branch.text,
                    "trigger": trigger,
                    # `changed` and `numstat` are lists of paths, and a path is
                    # the dominant identifier surface here. Redacted inline for
                    # the same reason as FILE_CHANGED above.
                    "changed": redacted_changed,
                    "numstat": redacted_numstat,
                    "changed_count": len(entries),
                },
                redactions=findings,
            )
        )
        self._head[root] = head
        self._capture_diffs(root, stats)

    def _capture_diffs(self, root: Path, stats: list[dict[str, str]]) -> None:
        """Store per-file patches for paths that pass every gate."""

        events: list[Event] = []
        for stat in stats[:40]:
            rel = stat["path"]
            target = root / rel
            if is_binary_path(target):
                continue
            try:
                if target.is_file() and target.stat().st_size > DIFF_MAX_BYTES:
                    continue
            except OSError:
                continue
            code, patch = run_git(
                root, "diff", "--no-ext-diff", "--no-color", "-U3", "--", rel
            )
            if code != 0 or not patch.strip():
                continue
            truncated = len(patch) > DIFF_OUTPUT_MAX
            if truncated:
                patch = patch[:DIFF_OUTPUT_MAX] + "\n... [diff truncated by wfrec]\n"
            # A diff hunk is **raw file content**, so this is the single
            # highest-risk thing the files collector writes -- and it went to
            # disk verbatim, leaving the seal as its only control. Redacting the
            # patch and its path inline costs one pass over a bounded string.
            redactor = _require_redactor()
            redacted_patch = redactor.apply(patch)
            redacted_rel = redactor.apply(rel)
            redacted_root = redactor.apply(str(root))
            findings = sorted(
                set(redacted_patch.findings)
                | set(redacted_rel.findings)
                | set(redacted_root.findings)
            )
            safe = rel.replace(os.sep, "__").replace("/", "__")
            # The *filename* is derived from the unredacted relative path, so it
            # would otherwise reintroduce the identifier the content just lost.
            safe = SAFE_DIFF_NAME.sub("_", redactor.apply(safe).text).strip("._") or "redacted"
            relative = f"files/diffs/{int(time.time() * 1000)}_{safe}.patch"
            destination = self.session.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(redacted_patch.text, encoding="utf-8")
            events.append(
                Event(
                    source=self.source,
                    type=FILE_DIFF,
                    payload={
                        "root": redacted_root.text,
                        "path": redacted_rel.text,
                        "added": stat["added"],
                        "deleted": stat["deleted"],
                        "truncated": truncated,
                    },
                    ref=relative,
                    redactions=findings,
                )
            )
        self.emit(events)

    def snapshot_all(self, trigger: str) -> None:
        """Snapshot every git root, bypassing the rate limit.

        Used for pause/resume gap reconciliation and at stop: taking a snapshot
        at both ends of a pause is how file changes made while capture was off
        are still recovered in aggregate.
        """

        for root in self._git_roots:
            self._git_snapshot(root, trigger=trigger, force=True)
