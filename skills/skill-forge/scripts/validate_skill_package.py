#!/usr/bin/env python3
"""Validate structural and CBD/STD invariants of an Agent Skill package."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import unquote


NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MARKDOWN_LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
PRIVATE_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])/(?:home|Users|research_jude)/[^\s`\"')]+"),
    re.compile(r"(?<![A-Za-z0-9])~/(?:[^\s`\"')]+)"),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s`\"')]+"),
)


def parseFrontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse basic top-level YAML scalars from SKILL.md frontmatter.

    This intentionally supports only the fields needed for structural checks;
    it is not a general YAML parser.
    """
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter delimiter.")
    closing = text.find("\n---", 4)
    if closing < 0:
        raise ValueError("SKILL.md frontmatter has no closing delimiter.")
    frontmatter = text[4:closing]
    body = text[closing + 4 :].lstrip("\n")
    values: dict[str, str] = {}
    lines = frontmatter.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s*(.*))?$", line)
        if not match:
            index += 1
            continue
        key, raw = match.group(1), (match.group(2) or "").strip()
        if raw in {">", ">-", "|", "|-"}:
            folded: list[str] = []
            index += 1
            while index < len(lines) and (
                not lines[index].strip() or lines[index].startswith((" ", "\t"))
            ):
                if lines[index].strip():
                    folded.append(lines[index].strip())
                index += 1
            values[key] = " ".join(folded)
            continue
        if raw.startswith('"') and raw.endswith('"'):
            try:
                values[key] = json.loads(raw)
            except json.JSONDecodeError:
                values[key] = raw.strip('"')
        elif raw.startswith("'") and raw.endswith("'"):
            values[key] = raw[1:-1].replace("''", "'")
        else:
            values[key] = raw
        index += 1
    return values, body


def linkTarget(raw: str) -> str:
    """Extract the path component from a Markdown link target."""
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    if " " in value:
        value = value.split(" ", 1)[0]
    return unquote(value)


def isInside(path: Path, root: Path) -> bool:
    """Return whether a resolved path remains inside root."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def inspectLinks(root: Path, errors: list[str]) -> None:
    """Verify local Markdown links stay inside the package and exist."""
    for markdown in sorted(root.rglob("*.md"), key=lambda path: str(path)):
        if markdown.is_symlink() or markdown.name.endswith(".template.md"):
            continue
        try:
            text = markdown.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"Cannot read Markdown file {markdown.relative_to(root)}: {exc}")
            continue
        for raw in MARKDOWN_LINK_PATTERN.findall(text):
            targetValue = linkTarget(raw)
            if not targetValue or targetValue.startswith(
                ("#", "http://", "https://", "mailto:")
            ):
                continue
            targetValue = targetValue.split("#", 1)[0]
            target = markdown.parent / targetValue
            relativeSource = markdown.relative_to(root)
            if not isInside(target, root):
                errors.append(
                    f"{relativeSource}: link escapes skill package: {targetValue}"
                )
            elif not target.exists():
                errors.append(
                    f"{relativeSource}: linked resource does not exist: {targetValue}"
                )


def inspectSymlinks(root: Path, errors: list[str]) -> None:
    """Reject symlinks inside a portable generated skill package."""
    for path in sorted(root.rglob("*"), key=lambda value: str(value)):
        if path.is_symlink():
            errors.append(f"Skill package contains symlink: {path.relative_to(root)}")


def inspectTemplateTokens(root: Path, errors: list[str]) -> None:
    """Detect unrendered template syntax in text files."""
    textSuffixes = {".md", ".json", ".sh", ".txt", ".yaml", ".yml"}
    for path in sorted(root.rglob("*"), key=lambda value: str(value)):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in textSuffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if re.search(r"\{\{[^}]+\}\}", text):
            errors.append(f"Unrendered '{{{{...}}}}' token in {path.relative_to(root)}")
        if path.name.endswith(".template.md"):
            continue
        token = re.search(
            r"(?m)^\s*\$(?:name|title|purpose|workflow|dependencies)\s*$", text
        )
        if token:
            errors.append(f"Unrendered template token in {path.relative_to(root)}")


def inspectPrivatePaths(root: Path, errors: list[str]) -> None:
    """Detect common machine-specific absolute paths in an STD package."""
    textSuffixes = {
        ".bash",
        ".cfg",
        ".conf",
        ".json",
        ".md",
        ".nf",
        ".py",
        ".r",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
    for path in sorted(root.rglob("*"), key=lambda value: str(value)):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in textSuffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in PRIVATE_PATH_PATTERNS:
            match = pattern.search(text)
            if match:
                errors.append(
                    f"STD package contains machine-specific path in "
                    f"{path.relative_to(root)}: {match.group(0)}"
                )
                break


def validatePackage(
    root: Path,
    expectedPackaging: str | None,
    strict: bool,
    allowProposed: bool,
) -> dict[str, Any]:
    """Validate one Agent Skill package and return a structured report."""
    errors: list[str] = []
    warnings: list[str] = []
    if not root.exists() or not root.is_dir():
        return {
            "valid": False,
            "errors": [f"Skill directory does not exist: {root}"],
            "warnings": [],
        }
    if root.is_symlink():
        errors.append("Validate the source directory, not an installation symlink.")

    skillFiles = [
        path for path in root.rglob("SKILL.md") if path.is_file() and not path.is_symlink()
    ]
    if len(skillFiles) != 1:
        errors.append(f"Expected exactly one SKILL.md, found {len(skillFiles)}.")
    skillPath = root / "SKILL.md"
    if not skillPath.is_file():
        errors.append("Missing root SKILL.md.")
        return {"valid": False, "errors": errors, "warnings": warnings}

    text = skillPath.read_text(encoding="utf-8")
    lineCount = len(text.splitlines())
    if lineCount > 500:
        errors.append(f"SKILL.md has {lineCount} lines; maximum is 500.")
    try:
        frontmatter, body = parseFrontmatter(text)
    except ValueError as exc:
        errors.append(str(exc))
        frontmatter, body = {}, ""

    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")
    if not name:
        errors.append("Frontmatter name is required.")
    elif not NAME_PATTERN.fullmatch(name):
        errors.append("Frontmatter name must use lowercase letters/numbers and hyphens.")
    elif len(name) > 64:
        errors.append("Frontmatter name exceeds 64 characters.")
    if name and name != root.name:
        errors.append(f"Directory/name mismatch: {root.name!r} != {name!r}.")
    if not description:
        errors.append("Frontmatter description is required.")
    elif len(description) > 1024:
        errors.append("Frontmatter description exceeds 1024 characters.")
    elif "when" not in description.lower():
        warnings.append("Description may not explain when the skill should trigger.")

    if not (root / "CHANGELOG.md").is_file():
        errors.append("Missing CHANGELOG.md.")
    if not (root / "README.md").is_file():
        warnings.append("Complex skills should include README.md.")

    detectedPackaging = expectedPackaging
    if detectedPackaging is None:
        if name.endswith("-cbd"):
            detectedPackaging = "cbd"
        elif name.endswith("-std"):
            detectedPackaging = "std"
    if expectedPackaging and not name.endswith(f"-{expectedPackaging}"):
        errors.append(f"Expected name suffix '-{expectedPackaging}'.")

    if detectedPackaging == "cbd":
        if not (root / "scripts" / "cbd_config.py").is_file():
            errors.append("CBD package is missing scripts/cbd_config.py.")
        lowered = body.lower()
        if "codebase-dependent" not in lowered or "autocab" not in lowered:
            errors.append("CBD SKILL.md does not document its AutoCAB codebase contract.")
    elif detectedPackaging == "std":
        if (root / "scripts" / "cbd_config.py").exists():
            errors.append("STD package must not contain scripts/cbd_config.py.")
        if (root / ".cursor" / "autoCAB").exists() or (
            root / ".agents" / "autoCAB"
        ).exists():
            errors.append("STD package must not embed a CBD config.")
        inspectPrivatePaths(root, errors)
        for bundleValue in re.findall(r"bundled as `([^`]+)`", body):
            bundlePath = root / bundleValue
            if not isInside(bundlePath, root) or not bundlePath.is_file():
                errors.append(
                    f"STD declared bundled dependency is missing: {bundleValue}"
                )

    if detectedPackaging and not allowProposed and re.search(
        r"(?i)(pending approval|`proposed`|status:\s*proposed)", body
    ):
        errors.append(
            "Package still contains proposed behavior awaiting approval "
            "(use --allowProposed only for staged review)."
        )

    inspectSymlinks(root, errors)
    inspectLinks(root, errors)
    inspectTemplateTokens(root, errors)

    if strict:
        for path in sorted(root.rglob("*"), key=lambda value: str(value)):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.lower() in {".md", ".py", ".sh", ".json"}:
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if re.search(r"(?im)^\s*(?:TODO|TBA)\b", content):
                    errors.append(f"Strict mode found TODO/TBA in {path.relative_to(root)}.")

    return {
        "valid": not errors,
        "skill": name or None,
        "packaging": detectedPackaging,
        "skillMdLines": lineCount,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
    }


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Validate a generated Agent Skill package without executing it."
    )
    parser.add_argument("skillDir", help="Source skill directory (not a symlink).")
    parser.add_argument(
        "--expectedPackaging",
        choices=["cbd", "std"],
        help="Require matching lowercase mode suffix and invariants.",
    )
    parser.add_argument(
        "--allowProposed",
        action="store_true",
        help="Permit explicitly proposed steps in a staged, non-final package.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat TODO/TBA markers in text/code as errors.",
    )
    parser.add_argument("--output", help="Optional JSON report path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the package validation CLI."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parseArgs(argv)
    root = Path(args.skillDir).expanduser().resolve()
    report = validatePackage(
        root, args.expectedPackaging, args.strict, args.allowProposed
    )
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        logging.info("Wrote validation report: %s", output)
    print(rendered, end="")
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    sys.exit(main())
