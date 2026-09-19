#!/usr/bin/env python3
"""Create a bounded, non-executing inventory of AutoCAB evidence sources."""

from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import logging
import mimetypes
from pathlib import Path
import re
import sys
from typing import Iterable
import xml.etree.ElementTree as ET


TEXT_EXTENSIONS = {
    ".bash",
    ".cfg",
    ".conf",
    ".csv",
    ".groovy",
    ".html",
    ".htm",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".nf",
    ".py",
    ".r",
    ".rst",
    ".sh",
    ".toml",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

RICH_READER_HINTS = {
    ".docx": "Use the host agent's DOCX reader or an approved extractor.",
    ".enex": "The inventory helper extracts note text with the standard library.",
    ".pdf": "Use the host agent's layout-aware PDF reader; inspect embedded figures when material.",
    ".pptx": "Use the host agent's PPTX reader or an approved extractor.",
    ".xlsx": "Use the host agent's spreadsheet reader; preserve formulas and sheet names.",
}


class VisibleTextParser(HTMLParser):
    """Collect visible text from HTML without executing or fetching resources."""

    def __init__(self) -> None:
        """Initialize the parser."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressedDepth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track script/style suppression and structural line breaks."""
        del attrs
        lowered = tag.lower()
        if lowered in {"script", "style"}:
            self.suppressedDepth += 1
        elif lowered in {"br", "p", "div", "li", "pre", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Close suppression regions and add structural line breaks."""
        lowered = tag.lower()
        if lowered in {"script", "style"} and self.suppressedDepth:
            self.suppressedDepth -= 1
        elif lowered in {"p", "div", "li", "pre", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        """Collect visible text outside script and style tags."""
        if not self.suppressedDepth:
            self.parts.append(data)

    def text(self) -> str:
        """Return whitespace-normalized visible text."""
        joined = html.unescape("".join(self.parts))
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line) + "\n"


def utcNow() -> str:
    """Return an ISO-8601 UTC timestamp."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256File(path: Path) -> str:
    """Compute a SHA-256 hash without loading the whole file.

    Args:
        path (Path): Regular file to hash.

    Returns:
        str: Lowercase hexadecimal SHA-256 digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def isHidden(path: Path, root: Path) -> bool:
    """Return whether any path component below root is hidden."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return any(part.startswith(".") for part in relative.parts)


def collectPaths(
    requested: Iterable[Path], includeHidden: bool, maxFiles: int
) -> tuple[list[Path], list[dict[str, str]]]:
    """Collect regular files without following symlinks.

    Args:
        requested (Iterable[Path]): Explicit files or directories.
        includeHidden (bool): Whether hidden entries may be inventoried.
        maxFiles (int): Hard upper bound.

    Returns:
        tuple[list[Path], list[dict[str, str]]]: Files and skipped/error records.

    Raises:
        ValueError: If the hard file limit is exceeded.
    """
    files: list[Path] = []
    records: list[dict[str, str]] = []
    seen: set[str] = set()

    for source in requested:
        expanded = source.expanduser()
        if not expanded.exists() and not expanded.is_symlink():
            records.append({"path": str(source), "status": "missing"})
            continue
        if expanded.is_symlink():
            records.append({"path": str(source), "status": "skipped_symlink"})
            continue
        if expanded.is_file():
            candidates = [expanded]
            root = expanded.parent
        elif expanded.is_dir():
            root = expanded
            candidates = sorted(expanded.rglob("*"), key=lambda item: str(item))
        else:
            records.append({"path": str(source), "status": "unsupported_node"})
            continue

        for candidate in candidates:
            if candidate.is_symlink():
                records.append({"path": str(candidate), "status": "skipped_symlink"})
                continue
            if not candidate.is_file():
                continue
            if not includeHidden and isHidden(candidate, root):
                continue
            canonical = str(candidate.resolve())
            if canonical in seen:
                continue
            seen.add(canonical)
            files.append(candidate)
            if len(files) > maxFiles:
                raise ValueError(
                    f"Source inventory exceeds --maxFiles={maxFiles}; narrow the input roots."
                )

    return sorted(files, key=lambda item: str(item)), records


def displayPath(path: Path, baseDir: Path, pathMode: str) -> str:
    """Render a source path according to the requested privacy mode."""
    if pathMode == "absolute":
        return str(path.resolve())
    if pathMode == "basename":
        return path.name
    if pathMode == "redacted":
        return f"<SOURCE>/{path.name}"
    try:
        return str(path.resolve().relative_to(baseDir.resolve()))
    except ValueError:
        return f"<EXTERNAL>/{path.name}"


def normalizeJson(text: str) -> tuple[str, list[str]]:
    """Normalize JSON text and report parse warnings."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return text, [f"JSON parse error at line {exc.lineno}: {exc.msg}"]
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", []


def normalizeJsonl(text: str) -> tuple[str, list[str]]:
    """Normalize JSONL while preserving record order."""
    records: list[str] = []
    warnings: list[str] = []
    for lineNumber, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"JSONL line {lineNumber}: {exc.msg}")
            records.append(line)
            continue
        records.append(json.dumps(value, sort_keys=True, ensure_ascii=False))
    return "\n".join(records) + ("\n" if records else ""), warnings


def normalizeHtml(text: str) -> tuple[str, list[str]]:
    """Extract visible text from HTML."""
    parser = VisibleTextParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # HTMLParser may surface malformed entity errors.
        return text, [f"HTML extraction failed: {exc}"]
    return parser.text(), []


def normalizeEnex(text: str) -> tuple[str, list[str]]:
    """Extract titles and note content from an Evernote ENEX export."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return text, [f"ENEX XML parse error: {exc}"]

    rendered: list[str] = []
    for index, note in enumerate(root.findall(".//note"), start=1):
        title = note.findtext("title") or f"Note {index}"
        content = note.findtext("content") or ""
        visible, warnings = normalizeHtml(content)
        rendered.extend([f"# {title}", "", visible.rstrip(), ""])
        if warnings:
            rendered.append(f"[Content extraction warning: {'; '.join(warnings)}]")
    return "\n".join(rendered).rstrip() + "\n", []


def normalizeText(path: Path, maxBytes: int) -> tuple[str | None, list[str]]:
    """Normalize a supported text-like file.

    Args:
        path (Path): Source file.
        maxBytes (int): Maximum bytes allowed for extraction.

    Returns:
        tuple[str | None, list[str]]: Normalized text, or None, plus warnings.
    """
    size = path.stat().st_size
    if size > maxBytes:
        return None, [f"Extraction skipped: {size} bytes exceeds --maxBytesPerFile={maxBytes}."]

    extension = path.suffix.lower()
    if extension not in TEXT_EXTENSIONS and extension != ".enex":
        return None, []

    raw = path.read_text(encoding="utf-8", errors="replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    if extension == ".json":
        return normalizeJson(raw)
    if extension == ".jsonl":
        return normalizeJsonl(raw)
    if extension in {".html", ".htm"}:
        return normalizeHtml(raw)
    if extension == ".enex":
        return normalizeEnex(raw)
    return raw, []


def safeExtractName(index: int, source: Path) -> str:
    """Build a deterministic normalized-text filename."""
    suffix = source.suffix.lower().lstrip(".") or "txt"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-") or "source"
    return f"{index:04d}-{stem}.{suffix}.txt"


def buildManifest(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    """Build the source manifest and optional normalized text copies."""
    requested = [Path(value) for value in args.sources]
    baseDir = Path(args.baseDir).expanduser() if args.baseDir else Path.cwd()
    files, skipped = collectPaths(requested, args.includeHidden, args.maxFiles)
    extractDir = Path(args.extractDir).expanduser() if args.extractDir else None
    if extractDir:
        extractDir.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, object]] = []
    contentIds: dict[str, str] = {}
    anyError = any(record["status"] == "missing" for record in skipped)

    for index, path in enumerate(files, start=1):
        try:
            digest = sha256File(path)
            stat = path.stat()
            normalized, warnings = normalizeText(path, args.maxBytesPerFile)
        except OSError as exc:
            entries.append(
                {
                    "path": displayPath(path, baseDir, args.pathMode),
                    "status": "read_error",
                    "error": str(exc),
                }
            )
            anyError = True
            continue

        sourceId = f"src-{digest[:12]}-{index:04d}"
        duplicateOf = contentIds.get(digest)
        contentIds.setdefault(digest, sourceId)
        entry: dict[str, object] = {
            "id": sourceId,
            "path": displayPath(path, baseDir, args.pathMode),
            "status": "ok",
            "sha256": digest,
            "bytes": stat.st_size,
            "extension": path.suffix.lower(),
            "mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "readerHint": RICH_READER_HINTS.get(path.suffix.lower()),
            "duplicateOf": duplicateOf,
            "warnings": warnings,
        }
        if normalized is not None and extractDir is not None:
            extractName = safeExtractName(index, path)
            outputPath = extractDir / extractName
            outputPath.write_text(normalized, encoding="utf-8")
            entry["normalizedText"] = extractName
            entry["normalizedLines"] = normalized.count("\n")
        entries.append(entry)

    manifest: dict[str, object] = {
        "schemaVersion": "1.0",
        "generatedAtUtc": utcNow(),
        "pathMode": args.pathMode,
        "sources": entries,
        "skipped": skipped,
        "summary": {
            "files": len(entries),
            "uniqueContentHashes": len(contentIds),
            "skipped": len(skipped),
            "bytes": sum(
                int(entry.get("bytes", 0)) for entry in entries if isinstance(entry, dict)
            ),
        },
    }
    return manifest, anyError


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Hash and inventory explicit evidence sources without executing captured content."
        )
    )
    parser.add_argument("sources", nargs="+", help="Files or directories to inventory.")
    parser.add_argument("--output", required=True, help="Output JSON manifest path.")
    parser.add_argument(
        "--extractDir",
        help="Optional directory for normalized copies of supported text-like files.",
    )
    parser.add_argument(
        "--baseDir",
        help="Base for relative path display (default: current working directory).",
    )
    parser.add_argument(
        "--pathMode",
        choices=["relative", "basename", "redacted", "absolute"],
        default="relative",
        help="How source paths appear in the manifest (default: relative).",
    )
    parser.add_argument(
        "--maxFiles",
        type=int,
        default=5000,
        help="Maximum number of files to inventory (default: 5000).",
    )
    parser.add_argument(
        "--maxBytesPerFile",
        type=int,
        default=10 * 1024 * 1024,
        help="Maximum bytes per text extraction (default: 10485760).",
    )
    parser.add_argument(
        "--includeHidden",
        action="store_true",
        help="Include hidden files below requested directories.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the source inventory CLI."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parseArgs(argv)
    if args.maxFiles < 1 or args.maxBytesPerFile < 1:
        logging.error("--maxFiles and --maxBytesPerFile must be positive.")
        return 2
    try:
        manifest, anyError = buildManifest(args)
    except (OSError, ValueError) as exc:
        logging.error("%s", exc)
        return 2

    outputPath = Path(args.output).expanduser()
    outputPath.parent.mkdir(parents=True, exist_ok=True)
    outputPath.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logging.info("Wrote source manifest: %s", outputPath)
    return 2 if anyError else 0


if __name__ == "__main__":
    sys.exit(main())
