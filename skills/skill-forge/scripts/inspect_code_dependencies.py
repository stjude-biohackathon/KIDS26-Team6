#!/usr/bin/env python3
"""Statically discover candidate code and command dependencies within approved roots."""

from __future__ import annotations

import argparse
import ast
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import shlex
import sys
from typing import Iterable


LANGUAGE_BY_SUFFIX = {
    ".bash": "shell",
    ".groovy": "nextflow",
    ".nf": "nextflow",
    ".py": "python",
    ".r": "r",
    ".sh": "shell",
}

SHELL_BUILTINS = {
    "alias",
    "break",
    "case",
    "cd",
    "continue",
    "do",
    "done",
    "echo",
    "elif",
    "else",
    "esac",
    "eval",
    "exec",
    "exit",
    "export",
    "fi",
    "for",
    "function",
    "if",
    "in",
    "local",
    "printf",
    "read",
    "readonly",
    "return",
    "set",
    "shift",
    "source",
    "then",
    "time",
    "trap",
    "typeset",
    "ulimit",
    "umask",
    "unset",
    "until",
    "wait",
    "while",
}

FILE_SUFFIXES = {
    ".bash",
    ".bed",
    ".cfg",
    ".conf",
    ".csv",
    ".fa",
    ".fasta",
    ".fq",
    ".groovy",
    ".json",
    ".nf",
    ".py",
    ".r",
    ".sh",
    ".toml",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class QueueItem:
    """A file queued for bounded recursive inspection."""

    path: Path
    depth: int


def utcNow() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def constantString(node: ast.AST) -> str | None:
    """Return a literal string represented by an AST node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def commandFromAst(node: ast.AST) -> str | None:
    """Extract a command candidate from a literal subprocess argument."""
    literal = constantString(node)
    if literal:
        try:
            tokens = shlex.split(literal)
        except ValueError:
            tokens = literal.split()
        return tokens[0] if tokens else None
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return constantString(node.elts[0])
    return None


def attributeName(node: ast.AST) -> str:
    """Return a dotted name for Name/Attribute AST nodes."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = attributeName(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def inspectPython(text: str) -> dict[str, object]:
    """Inspect Python source for imports, commands, and file references."""
    imports: set[str] = set()
    commands: set[str] = set()
    references: set[str] = set()
    warnings: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return {
            "imports": [],
            "commands": [],
            "references": [],
            "warnings": [f"Python parse error at line {exc.lineno}: {exc.msg}"],
        }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            imports.add(prefix + (node.module or ""))
        elif isinstance(node, ast.Call):
            functionName = attributeName(node.func)
            if functionName in {
                "subprocess.call",
                "subprocess.check_call",
                "subprocess.check_output",
                "subprocess.Popen",
                "subprocess.run",
                "os.popen",
                "os.system",
            } and node.args:
                command = commandFromAst(node.args[0])
                if command:
                    commands.add(command)
            if functionName in {
                "open",
                "Path",
                "pathlib.Path",
                "importlib.resources.files",
            } and node.args:
                reference = constantString(node.args[0])
                if reference and Path(reference).suffix.lower() in FILE_SUFFIXES:
                    references.add(reference)
            if functionName in {"exec", "eval"}:
                warnings.append(f"Dynamic execution candidate: {functionName}")
            if functionName in {
                "__import__",
                "importlib.import_module",
                "pkg_resources.iter_entry_points",
            }:
                warnings.append(f"Dynamic import/plugin candidate: {functionName}")

    return {
        "imports": sorted(value for value in imports if value),
        "commands": sorted(commands),
        "references": sorted(references),
        "warnings": sorted(set(warnings)),
    }


def shellTokens(line: str) -> list[str]:
    """Tokenize one shell line conservatively."""
    try:
        return shlex.split(line, comments=True, posix=True)
    except ValueError:
        return line.split()


def commandFromShellTokens(tokens: list[str]) -> str | None:
    """Find a likely executable token after assignments and wrappers."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            index += 1
            continue
        if token in {"command", "env", "nohup", "sudo"}:
            index += 1
            continue
        if token in SHELL_BUILTINS or token.startswith(("#", "-", "$")):
            return None
        return token
    return None


def inspectShell(text: str) -> dict[str, object]:
    """Inspect shell source for sourced files, script calls, and commands."""
    commands: set[str] = set()
    references: set[str] = set()
    warnings: list[str] = []

    sourcePattern = re.compile(r"^\s*(?:source|\.)\s+([^\s;&|]+)")
    scriptPattern = re.compile(
        r"(?:^|[;&|]\s*)(?:python(?:3)?|Rscript|bash|sh)\s+([^\s;&|]+)"
    )
    nextflowPattern = re.compile(r"\bnextflow\s+run\s+([^\s;&|]+)")

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        sourceMatch = sourcePattern.search(line)
        if sourceMatch:
            references.add(sourceMatch.group(1).strip("\"'"))
        for match in scriptPattern.finditer(line):
            references.add(match.group(1).strip("\"'"))
        for match in nextflowPattern.finditer(line):
            value = match.group(1).strip("\"'")
            if "/" in value or value.endswith(".nf"):
                references.add(value)
            else:
                commands.add(f"nextflow:{value}")

        segments = re.split(r"(?:&&|\|\||[;|])", line)
        for segment in segments:
            command = commandFromShellTokens(shellTokens(segment))
            if command:
                commands.add(command)

        if "eval " in line:
            warnings.append("Dynamic shell evaluation candidate: eval")

    return {
        "imports": [],
        "commands": sorted(commands),
        "references": sorted(references),
        "warnings": sorted(set(warnings)),
    }


def inspectR(text: str) -> dict[str, object]:
    """Inspect R source for packages, sourced files, and system commands."""
    imports: set[str] = set()
    commands: set[str] = set()
    references: set[str] = set()
    warnings: list[str] = []

    for match in re.finditer(
        r"\b(?:library|require)\s*\(\s*[\"']?([A-Za-z0-9_.]+)", text
    ):
        imports.add(match.group(1))
    imports.update(
        match.group(1)
        for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_.]+)::[A-Za-z]", text)
    )
    for match in re.finditer(r"\bsource\s*\(\s*[\"']([^\"']+)[\"']", text):
        references.add(match.group(1))
    for match in re.finditer(
        r"\b(?:system|system2)\s*\(\s*[\"']([^\"']+)[\"']", text
    ):
        literal = match.group(1)
        tokens = shellTokens(literal)
        if tokens:
            commands.add(tokens[0])
    if re.search(r"\b(?:eval|parse)\s*\(", text):
        warnings.append("Dynamic R evaluation candidate: eval/parse")

    return {
        "imports": sorted(imports),
        "commands": sorted(commands),
        "references": sorted(references),
        "warnings": warnings,
    }


def inspectNextflow(text: str) -> dict[str, object]:
    """Inspect Nextflow/Groovy source for modules, containers, and commands."""
    result = inspectShell(text)
    references = set(result["references"])
    commands = set(result["commands"])
    imports = set(result["imports"])
    warnings = list(result["warnings"])

    includePattern = re.compile(
        r"\binclude\s*\{[^}]*\}\s*from\s*[\"']([^\"']+)[\"']", re.DOTALL
    )
    for match in includePattern.finditer(text):
        references.add(match.group(1))
    for match in re.finditer(r"\bcontainer\s+[\"']([^\"']+)[\"']", text):
        commands.add(f"container:{match.group(1)}")
    for match in re.finditer(r"\bconda\s+[\"']([^\"']+)[\"']", text):
        commands.add(f"conda:{match.group(1)}")
    for match in re.finditer(r"\bplugins?\s*\{([^}]+)\}", text, re.DOTALL):
        pluginText = re.sub(r"\s+", " ", match.group(1)).strip()
        imports.add(f"nextflow-plugin:{pluginText}")

    return {
        "imports": sorted(imports),
        "commands": sorted(commands),
        "references": sorted(references),
        "warnings": sorted(set(warnings)),
    }


def detectLanguage(path: Path) -> str:
    """Detect supported source language from filename and suffix."""
    if path.name in {"Snakefile", "nextflow.config"}:
        return "nextflow"
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "text")


def inspectText(path: Path, maxBytes: int) -> dict[str, object]:
    """Inspect one source file without executing it."""
    size = path.stat().st_size
    if size > maxBytes:
        return {
            "imports": [],
            "commands": [],
            "references": [],
            "warnings": [
                f"Inspection skipped: {size} bytes exceeds --maxBytesPerFile={maxBytes}."
            ],
        }
    text = path.read_text(encoding="utf-8", errors="replace")
    language = detectLanguage(path)
    if language == "python":
        return inspectPython(text)
    if language == "shell":
        return inspectShell(text)
    if language == "r":
        return inspectR(text)
    if language == "nextflow":
        return inspectNextflow(text)
    return {"imports": [], "commands": [], "references": [], "warnings": []}


def isWithin(path: Path, roots: Iterable[Path]) -> bool:
    """Return whether a resolved path lies inside an approved root."""
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def resolveReference(
    reference: str, source: Path, roots: list[Path]
) -> tuple[Path | None, str | None]:
    """Resolve a literal local reference only within approved roots."""
    if any(character in reference for character in "$*?[]{}"):
        return None, "dynamic_reference"
    candidatePath = Path(reference).expanduser()
    candidates = (
        [candidatePath]
        if candidatePath.is_absolute()
        else [source.parent / candidatePath] + [root / candidatePath for root in roots]
    )
    for candidate in candidates:
        if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
            if isWithin(candidate, roots + [source.parent]):
                return candidate.resolve(), None
    if candidatePath.is_absolute() and not isWithin(candidatePath, roots):
        return None, "outside_approved_roots"
    return None, "not_found"


def resolvePythonImport(
    module: str, source: Path, roots: list[Path]
) -> Path | None:
    """Resolve a Python import to a local module when possible."""
    if not module:
        return None
    relativeLevel = len(module) - len(module.lstrip("."))
    moduleName = module.lstrip(".")
    bases: list[Path] = []
    if relativeLevel:
        base = source.parent
        for _ in range(max(relativeLevel - 1, 0)):
            base = base.parent
        bases.append(base)
    else:
        bases.extend([source.parent, *roots])
    parts = moduleName.split(".") if moduleName else []
    for base in bases:
        stem = base.joinpath(*parts)
        for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
            if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
                if isWithin(candidate, roots + [source.parent]):
                    return candidate.resolve()
    return None


def displayPath(path: Path, roots: list[Path]) -> str:
    """Display a path relative to the first matching approved root."""
    for index, root in enumerate(roots, start=1):
        try:
            relative = path.resolve().relative_to(root.resolve())
            return f"<ROOT_{index}>/{relative}"
        except ValueError:
            continue
    return f"<ENTRYPOINT>/{path.name}"


def collectEntrypoints(values: Iterable[str], maxFiles: int) -> list[Path]:
    """Collect explicit source entrypoints without following symlinks."""
    files: list[Path] = []
    for value in values:
        path = Path(value).expanduser()
        if not path.exists():
            raise ValueError(f"Entrypoint does not exist: {value}")
        if path.is_symlink():
            raise ValueError(f"Symlink entrypoints are not followed: {value}")
        if path.is_file():
            files.append(path.resolve())
        elif path.is_dir():
            for candidate in sorted(path.rglob("*"), key=lambda item: str(item)):
                if candidate.is_file() and not candidate.is_symlink():
                    if detectLanguage(candidate) != "text":
                        files.append(candidate.resolve())
                        if len(files) > maxFiles:
                            raise ValueError(
                                f"Entrypoint scan exceeds --maxFiles={maxFiles}."
                            )
    return sorted(set(files), key=str)


def inspectGraph(args: argparse.Namespace) -> dict[str, object]:
    """Inspect entrypoints and recursively resolved local references."""
    entrypoints = collectEntrypoints(args.entrypoints, args.maxFiles)
    roots = [Path(value).expanduser().resolve() for value in args.root]
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"Approved root is not a directory: {root}")
    if not roots:
        roots = sorted({path.parent for path in entrypoints}, key=str)

    queue: deque[QueueItem] = deque(QueueItem(path, 0) for path in entrypoints)
    seen: set[Path] = set()
    records: list[dict[str, object]] = []
    allCommands: set[str] = set()
    allImports: set[str] = set()
    unresolved: list[dict[str, str]] = []

    while queue:
        item = queue.popleft()
        path = item.path.resolve()
        if path in seen:
            continue
        if len(seen) >= args.maxFiles:
            raise ValueError(f"Recursive inspection exceeds --maxFiles={args.maxFiles}.")
        if not isWithin(path, roots + [entrypoint.parent for entrypoint in entrypoints]):
            unresolved.append(
                {
                    "source": displayPath(path, roots),
                    "reference": str(path),
                    "reason": "outside_approved_roots",
                }
            )
            continue
        seen.add(path)
        try:
            result = inspectText(path, args.maxBytesPerFile)
        except OSError as exc:
            records.append(
                {
                    "path": displayPath(path, roots),
                    "depth": item.depth,
                    "language": detectLanguage(path),
                    "error": str(exc),
                }
            )
            continue

        resolvedReferences: list[str] = []
        for reference in result["references"]:
            resolved, reason = resolveReference(str(reference), path, roots)
            if resolved:
                resolvedReferences.append(displayPath(resolved, roots))
                if item.depth < args.maxDepth:
                    queue.append(QueueItem(resolved, item.depth + 1))
            else:
                unresolved.append(
                    {
                        "source": displayPath(path, roots),
                        "reference": str(reference),
                        "reason": reason or "not_found",
                    }
                )

        localImports: list[str] = []
        for module in result["imports"]:
            moduleString = str(module)
            if moduleString.startswith(("nextflow-plugin:",)):
                continue
            localModule = resolvePythonImport(moduleString, path, roots)
            if localModule:
                localImports.append(displayPath(localModule, roots))
                if item.depth < args.maxDepth:
                    queue.append(QueueItem(localModule, item.depth + 1))

        allCommands.update(str(command) for command in result["commands"])
        allImports.update(str(module) for module in result["imports"])
        records.append(
            {
                "path": displayPath(path, roots),
                "depth": item.depth,
                "language": detectLanguage(path),
                "imports": result["imports"],
                "localImports": sorted(localImports),
                "commands": result["commands"],
                "references": result["references"],
                "resolvedReferences": sorted(resolvedReferences),
                "warnings": result["warnings"],
            }
        )

    return {
        "schemaVersion": "1.0",
        "generatedAtUtc": utcNow(),
        "limits": {
            "maxDepth": args.maxDepth,
            "maxFiles": args.maxFiles,
            "maxBytesPerFile": args.maxBytesPerFile,
        },
        "roots": [f"<ROOT_{index}>" for index, _ in enumerate(roots, start=1)],
        "files": records,
        "summary": {
            "filesInspected": len(records),
            "commands": sorted(allCommands),
            "importsOrPackages": sorted(allImports),
            "unresolvedCount": len(unresolved),
        },
        "unresolvedReferences": sorted(
            unresolved,
            key=lambda record: (
                record["source"],
                record["reference"],
                record["reason"],
            ),
        ),
        "limitations": [
            "Static discovery cannot prove complete dynamic dependency closure.",
            "Command candidates require public-tool versus custom-wrapper verification.",
            "Paths containing variables or glob syntax remain unresolved by design.",
        ],
    }


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Statically inspect Python, shell, R, and Nextflow entrypoints without running them."
        )
    )
    parser.add_argument(
        "entrypoints", nargs="+", help="Explicit source files or directories."
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Approved resolution root; may be repeated.",
    )
    parser.add_argument("--output", required=True, help="Output dependency-report JSON.")
    parser.add_argument(
        "--maxDepth",
        type=int,
        default=5,
        help="Maximum transitive reference depth (default: 5).",
    )
    parser.add_argument(
        "--maxFiles",
        type=int,
        default=500,
        help="Maximum files inspected (default: 500).",
    )
    parser.add_argument(
        "--maxBytesPerFile",
        type=int,
        default=2 * 1024 * 1024,
        help="Maximum source bytes parsed per file (default: 2097152).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the static dependency inspection CLI."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parseArgs(argv)
    if args.maxDepth < 0 or args.maxFiles < 1 or args.maxBytesPerFile < 1:
        logging.error("Depth must be non-negative; file and byte limits must be positive.")
        return 2
    try:
        report = inspectGraph(args)
    except (OSError, ValueError) as exc:
        logging.error("%s", exc)
        return 2
    outputPath = Path(args.output).expanduser()
    outputPath.parent.mkdir(parents=True, exist_ok=True)
    outputPath.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logging.info("Wrote dependency report: %s", outputPath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
