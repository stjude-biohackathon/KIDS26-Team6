"""Spool record format: the shell-to-daemon wire protocol."""

from __future__ import annotations

from pathlib import Path


from wfrec import spool


def test_hostile_command_round_trips_exactly():
    """Quotes, backslashes and tabs must survive verbatim.

    This is the whole reason the format is RS/US delimited rather than JSON:
    escaping these out of a shell hook would cost a fork per prompt, and
    getting the escaping subtly wrong would corrupt the command silently.
    """

    nasty = 'samtools view -c "in.bam" | awk \'{print $1"\\t"$2}\' # back\\slash'
    line = spool.encode(
        spool.KIND_COMMAND, "1789578668017", "/data", "0", "8421", nasty, "zsh", "411", "7"
    )
    record = spool.decode_line(line, Path("h-411.rec"))

    assert record is not None
    assert record.named("command") == nasty
    assert record.named_int("exit_code") == 0
    assert record.named_int("duration_ms") == 8421
    assert record.named("shell") == "zsh"


def test_short_record_degrades_instead_of_raising():
    """A POSIX sh remote hook emits fewer fields; that must not be fatal."""

    line = spool.encode(spool.KIND_COMMAND, "1789578668017", "/scratch", "1")
    record = spool.decode_line(line, Path("login1-99.rec"))

    assert record is not None
    assert record.named_int("exit_code") == 1
    assert record.named("command") == ""
    assert record.named_int("duration_ms", -1) == -1


def test_delimiters_are_stripped_from_field_values():
    """A field may not contain the delimiters, so encode must neutralize them."""

    line = spool.encode(spool.KIND_COMMAND, "1", "/tmp", "0", "1", f"echo{spool.US}x{spool.RS}y")
    record = spool.decode_line(line, Path("h-1.rec"))
    assert record.named("command") == "echo x y"


def test_reader_must_not_use_splitlines(tmp_path):
    """Regression guard for a subtle bug worth naming.

    ``str.splitlines()`` treats \\x1c, \\x1d and \\x1e as line boundaries -- and
    RS *is* \\x1e -- so a reader built on splitlines() cuts every record in half
    at its own leading delimiter and silently yields nothing.
    """

    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    target = spool_dir / "host-1.rec"
    for index in range(3):
        spool.append(target, spool.encode(spool.KIND_COMMAND, "1", "/tmp", "0", "1", f"cmd{index}"))

    raw = target.read_text()
    assert len(raw.splitlines()) != 3, "if this fails the hazard no longer exists"

    records = spool.SpoolReader(spool_dir).drain()
    assert [r.named("command") for r in records] == ["cmd0", "cmd1", "cmd2"]


def test_reader_is_incremental_and_ignores_partial_lines(tmp_path):
    """Draining twice must not re-deliver, and a half-written record waits."""

    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    target = spool_dir / "host-1.rec"
    spool.append(target, spool.encode(spool.KIND_COMMAND, "1", "/tmp", "0", "1", "first"))

    reader = spool.SpoolReader(spool_dir)
    assert [r.named("command") for r in reader.drain()] == ["first"]
    assert reader.drain() == []

    # A hook caught mid-append: no trailing newline yet.
    with target.open("a", encoding="utf-8") as handle:
        handle.write(spool.RS + "cmd" + spool.US + "2" + spool.US + "/tmp")
    assert reader.drain() == []

    with target.open("a", encoding="utf-8") as handle:
        handle.write(spool.US + "0" + spool.US + "1" + spool.US + "second\n")
    assert [r.named("command") for r in reader.drain()] == ["second"]


def test_reader_rereads_truncated_file(tmp_path):
    """If a file shrinks, re-read from zero rather than seeking past the end."""

    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    target = spool_dir / "host-1.rec"
    spool.append(target, spool.encode(spool.KIND_COMMAND, "1", "/tmp", "0", "1", "before"))
    reader = spool.SpoolReader(spool_dir)
    reader.drain()

    target.write_text(spool.encode(spool.KIND_COMMAND, "1", "/tmp", "0", "1", "after"))
    assert [r.named("command") for r in reader.drain()] == ["after"]


def test_non_record_lines_are_ignored(tmp_path):
    assert spool.decode_line("not a record\n", Path("x.rec")) is None
    assert spool.decode_line("", Path("x.rec")) is None
