#!/usr/bin/env python3
"""Resolve, validate, and atomically update local AutoCAB CBD configuration."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


CONFIG_FILENAME = "codebase-dependent-skill.config"
AGENT_PATHS = {
    "cursor": Path(".cursor/autoCAB") / CONFIG_FILENAME,
    "claude": Path(".claude/autoCAB") / CONFIG_FILENAME,
    "copilot": Path(".github/autoCAB") / CONFIG_FILENAME,
    "codex": Path(".agents/autoCAB") / CONFIG_FILENAME,
    "generic": Path(".agents/autoCAB") / CONFIG_FILENAME,
}
RECOGNIZED_PATHS = (
    AGENT_PATHS["cursor"],
    AGENT_PATHS["claude"],
    AGENT_PATHS["copilot"],
    AGENT_PATHS["codex"],
    Path(".codex/autoCAB") / CONFIG_FILENAME,
)


class ConfigError(Exception):
    """Represent an actionable CBD configuration failure."""


def utcNow() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def projectPath(value: str | None) -> Path:
    """Resolve the project root without requiring a Git repository."""
    return Path(value or Path.cwd()).expanduser().resolve()


def explicitConfigPath(value: str, projectRoot: Path) -> Path:
    """Resolve an explicit config path relative to the project root."""
    path = Path(value).expanduser()
    return path.absolute() if path.is_absolute() else (projectRoot / path).absolute()


def existingRecognizedConfigs(projectRoot: Path) -> list[Path]:
    """Return recognized config files already present in a project."""
    return [
        (projectRoot / relative).absolute()
        for relative in RECOGNIZED_PATHS
        if (projectRoot / relative).is_file()
    ]


def resolveConfigPath(
    projectRoot: Path,
    explicit: str | None = None,
    agent: str | None = None,
) -> Path:
    """Resolve config path using explicit, environment, existing, and portable rules.

    Args:
        projectRoot (Path): Project root containing agent directories.
        explicit (str | None): Explicit CLI path.
        agent (str | None): Agent identifier.

    Returns:
        Path: Resolved config file path.

    Raises:
        ConfigError: If configuration selection is ambiguous or agent is unknown.
    """
    if explicit:
        return explicitConfigPath(explicit, projectRoot)
    environmentPath = os.environ.get("AUTOCAB_CBD_CONFIG")
    if environmentPath:
        return explicitConfigPath(environmentPath, projectRoot)

    existing = existingRecognizedConfigs(projectRoot)
    if len(existing) == 1:
        return existing[0]
    if len(existing) > 1:
        rendered = ", ".join(str(path) for path in existing)
        raise ConfigError(
            "Multiple AutoCAB CBD configs exist; pass --config to select one: "
            f"{rendered}"
        )

    selectedAgent = agent or os.environ.get("AUTOCAB_AGENT") or "generic"
    if selectedAgent not in AGENT_PATHS:
        supported = ", ".join(sorted(AGENT_PATHS))
        raise ConfigError(
            f"Unknown agent '{selectedAgent}'. Use one of {supported}, "
            "or pass --config for another agent."
        )
    return (projectRoot / AGENT_PATHS[selectedAgent]).absolute()


def emptyConfig() -> dict[str, Any]:
    """Return a new CBD configuration document."""
    return {"schemaVersion": "1.0", "skills": {}}


def validateSentinel(value: str) -> str:
    """Validate and normalize a relative sentinel path."""
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value.strip() in {"", "."}:
        raise ConfigError(
            f"Sentinel must be a non-empty relative path without '..': {value!r}"
        )
    return path.as_posix()


def validateConfigData(data: Any) -> dict[str, Any]:
    """Validate the supported CBD JSON structure.

    Args:
        data (Any): Parsed JSON.

    Returns:
        dict[str, Any]: Validated configuration.

    Raises:
        ConfigError: If required fields or values are malformed.
    """
    if not isinstance(data, dict):
        raise ConfigError("CBD config root must be a JSON object.")
    if data.get("schemaVersion") != "1.0":
        raise ConfigError("CBD config schemaVersion must be '1.0'.")
    skills = data.get("skills")
    if not isinstance(skills, dict):
        raise ConfigError("CBD config 'skills' must be a JSON object.")
    for skill, entry in skills.items():
        if not isinstance(skill, str) or not skill:
            raise ConfigError("CBD config skill keys must be non-empty strings.")
        if not isinstance(entry, dict):
            raise ConfigError(f"Config entry for {skill!r} must be an object.")
        roots = entry.get("roots")
        sentinels = entry.get("sentinels")
        gitRemotes = entry.get("gitRemotes")
        lastValidated = entry.get("lastValidatedUtc")
        if (
            not isinstance(roots, list)
            or not roots
            or not all(isinstance(root, str) and root for root in roots)
        ):
            raise ConfigError(f"{skill}: roots must be a non-empty string array.")
        if (
            not isinstance(sentinels, list)
            or not sentinels
            or not all(isinstance(item, str) for item in sentinels)
        ):
            raise ConfigError(f"{skill}: sentinels must be a non-empty string array.")
        for sentinel in sentinels:
            validateSentinel(sentinel)
        if not isinstance(gitRemotes, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in gitRemotes.items()
        ):
            raise ConfigError(f"{skill}: gitRemotes must be a string map.")
        if lastValidated is not None and not isinstance(lastValidated, str):
            raise ConfigError(f"{skill}: lastValidatedUtc must be a string or null.")
    return data


def loadConfig(path: Path, allowMissing: bool = False) -> dict[str, Any]:
    """Load and validate a CBD config."""
    if not path.exists():
        if allowMissing:
            return emptyConfig()
        raise ConfigError(f"CBD config does not exist: {path}")
    if path.is_symlink():
        raise ConfigError(f"CBD config must not be a symlink: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid CBD config JSON at line {exc.lineno}: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read CBD config {path}: {exc}") from exc
    return validateConfigData(data)


def atomicWrite(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON with owner-only permissions where supported."""
    validateConfigData(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise ConfigError(f"Refusing to replace symlink config: {path}")
    temporaryName: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporaryName = handle.name
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporaryName, 0o600)
        os.replace(temporaryName, path)
    except OSError as exc:
        if temporaryName:
            try:
                Path(temporaryName).unlink(missing_ok=True)
            except OSError:
                pass
        raise ConfigError(f"Cannot atomically write CBD config {path}: {exc}") from exc


def parseGitRemote(values: list[str], roots: list[str]) -> dict[str, str]:
    """Parse repeated ROOT=URL values and validate root membership."""
    rootSet = set(roots)
    remotes: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ConfigError("--gitRemote must use ROOT=URL.")
        rootValue, remote = value.split("=", 1)
        normalizedRoot = str(Path(rootValue).expanduser().resolve())
        if normalizedRoot not in rootSet:
            raise ConfigError(
                f"--gitRemote root is not present in --root values: {rootValue}"
            )
        if not remote:
            raise ConfigError("--gitRemote URL must not be empty.")
        remotes[normalizedRoot] = remote
    return remotes


def localGitRemote(root: Path) -> tuple[str | None, str | None]:
    """Read local origin URL without network access."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if completed.returncode == 0:
        return completed.stdout.strip(), None
    return None, completed.stderr.strip() or "No remote.origin.url configured."


def validateSkillEntry(entry: dict[str, Any]) -> dict[str, Any]:
    """Validate configured roots, sentinels, and optional local Git remotes."""
    rootReports: list[dict[str, Any]] = []
    validRoots: list[Path] = []
    for rootValue in entry["roots"]:
        root = Path(rootValue).expanduser()
        exists = root.exists()
        isDirectory = root.is_dir()
        readable = os.access(root, os.R_OK | os.X_OK) if exists else False
        valid = exists and isDirectory and readable
        rootReports.append(
            {
                "path": str(root),
                "exists": exists,
                "isDirectory": isDirectory,
                "readable": readable,
                "valid": valid,
            }
        )
        if valid:
            validRoots.append(root.resolve())

    sentinelReports: list[dict[str, Any]] = []
    for sentinelValue in entry["sentinels"]:
        sentinel = validateSentinel(sentinelValue)
        matches = [
            str(root / sentinel)
            for root in validRoots
            if (root / sentinel).is_file() or (root / sentinel).is_dir()
        ]
        sentinelReports.append(
            {"sentinel": sentinel, "matches": matches, "valid": bool(matches)}
        )

    remoteReports: list[dict[str, Any]] = []
    for rootValue, expected in entry["gitRemotes"].items():
        root = Path(rootValue).expanduser()
        actual, error = localGitRemote(root)
        remoteReports.append(
            {
                "root": str(root),
                "expected": expected,
                "actual": actual,
                "error": error,
                "valid": actual == expected,
            }
        )

    valid = (
        all(report["valid"] for report in rootReports)
        and all(report["valid"] for report in sentinelReports)
        and all(report["valid"] for report in remoteReports)
    )
    return {
        "valid": valid,
        "roots": rootReports,
        "sentinels": sentinelReports,
        "gitRemotes": remoteReports,
        "lastValidatedUtc": entry.get("lastValidatedUtc"),
        "networkChecked": False,
    }


def addCommonOptions(parser: argparse.ArgumentParser) -> None:
    """Add shared path-resolution options to a subcommand."""
    parser.add_argument(
        "--projectRoot",
        help="Project root (default: current working directory).",
    )
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_PATHS),
        help="Agent-specific AutoCAB path; generic uses .agents/autoCAB.",
    )
    parser.add_argument(
        "--config",
        help="Explicit config path; overrides environment and agent selection.",
    )


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Manage machine-local AutoCAB codebase roots for CBD skills."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolveParser = subparsers.add_parser("resolve", help="Print resolved config path.")
    addCommonOptions(resolveParser)

    listParser = subparsers.add_parser("list", help="List configured skill names.")
    addCommonOptions(listParser)

    getParser = subparsers.add_parser("get", help="Print one skill entry.")
    addCommonOptions(getParser)
    getParser.add_argument("--skill", required=True)

    setParser = subparsers.add_parser(
        "set", help="Create or replace one skill entry atomically."
    )
    addCommonOptions(setParser)
    setParser.add_argument("--skill", required=True)
    setParser.add_argument("--root", action="append", required=True)
    setParser.add_argument("--sentinel", action="append", required=True)
    setParser.add_argument(
        "--gitRemote",
        action="append",
        default=[],
        metavar="ROOT=URL",
        help="Optional expected local origin URL; may be repeated.",
    )

    validateParser = subparsers.add_parser(
        "validate", help="Validate roots/sentinels without fetching or pulling."
    )
    addCommonOptions(validateParser)
    validateParser.add_argument("--skill", required=True)
    validateParser.add_argument(
        "--touch",
        action="store_true",
        help="Atomically update lastValidatedUtc only after successful validation.",
    )

    platformsParser = subparsers.add_parser(
        "platforms", help="Print supported AutoCAB agent path mappings."
    )
    addCommonOptions(platformsParser)
    return parser.parse_args(argv)


def runCommand(args: argparse.Namespace) -> int:
    """Execute a parsed config subcommand."""
    root = projectPath(args.projectRoot)
    if args.command == "platforms":
        print(
            json.dumps(
                {
                    "portableDefault": str(AGENT_PATHS["generic"]),
                    "agents": {
                        name: str(relative) for name, relative in AGENT_PATHS.items()
                    },
                    "compatibilityPaths": [
                        str(Path(".codex/autoCAB") / CONFIG_FILENAME)
                    ],
                },
                indent=2,
            )
        )
        return 0
    path = resolveConfigPath(root, args.config, args.agent)
    if args.command == "resolve":
        print(path)
        return 0

    config = loadConfig(path, allowMissing=args.command == "set")
    skills = config["skills"]

    if args.command == "list":
        print(json.dumps({"config": str(path), "skills": sorted(skills)}, indent=2))
        return 0
    if args.command == "get":
        if args.skill not in skills:
            raise ConfigError(f"Skill is not configured: {args.skill}")
        print(
            json.dumps(
                {"config": str(path), "skill": args.skill, "entry": skills[args.skill]},
                indent=2,
            )
        )
        return 0
    if args.command == "set":
        roots = list(
            dict.fromkeys(
                str(Path(value).expanduser().resolve()) for value in args.root
            )
        )
        sentinels = list(
            dict.fromkeys(validateSentinel(value) for value in args.sentinel)
        )
        skills[args.skill] = {
            "roots": roots,
            "sentinels": sentinels,
            "gitRemotes": parseGitRemote(args.gitRemote, roots),
            "lastValidatedUtc": None,
        }
        atomicWrite(path, config)
        print(
            json.dumps(
                {"config": str(path), "skill": args.skill, "updated": True},
                indent=2,
            )
        )
        return 0
    if args.command == "validate":
        if args.skill not in skills:
            raise ConfigError(f"Skill is not configured: {args.skill}")
        report = validateSkillEntry(skills[args.skill])
        if report["valid"] and args.touch:
            skills[args.skill]["lastValidatedUtc"] = utcNow()
            report["lastValidatedUtc"] = skills[args.skill]["lastValidatedUtc"]
            atomicWrite(path, config)
        print(
            json.dumps(
                {"config": str(path), "skill": args.skill, "validation": report},
                indent=2,
            )
        )
        return 0 if report["valid"] else 3
    raise ConfigError(f"Unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    """Run the CBD configuration CLI."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parseArgs(argv)
    try:
        return runCommand(args)
    except ConfigError as exc:
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
