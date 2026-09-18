#!/usr/bin/env python3
"""Create an auditable, replayable record for one generated-skill run."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
from urllib.parse import urlparse


RECORD_SCHEMA_VERSION = "1.0"
STEP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SUMMARY_FIELDS = {
    "status",
    "whatWasDone",
    "findings",
    "parameters",
    "inputs",
    "outputs",
    "environmentSnapshot",
    "skillsUsed",
    "versions",
    "warnings",
    "assumptions",
    "manualSteps",
    "limitations",
}
LIST_FIELDS = {
    "whatWasDone",
    "findings",
    "inputs",
    "outputs",
    "skillsUsed",
    "versions",
    "warnings",
    "assumptions",
    "manualSteps",
    "limitations",
}


class RunRecordError(Exception):
    """Represent an invalid recorder request or incomplete run record."""


def utcNow():
    """Return a second-resolution UTC timestamp."""
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def defaultRunId():
    """Return a filesystem-safe UTC run identifier."""
    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def sha256Bytes(value):
    """Return a SHA-256 hex digest for bytes."""
    return hashlib.sha256(value).hexdigest()


def sha256File(path):
    """Return a SHA-256 hex digest for a regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256Directory(path):
    """Hash stable relative paths and file contents in a skill package."""
    digest = hashlib.sha256()
    excludedDirectories = {".git", "__pycache__"}
    for root, directoryNames, fileNames in os.walk(str(path)):
        directoryNames[:] = sorted(
            name for name in directoryNames if name not in excludedDirectories
        )
        rootPath = Path(root)
        for fileName in sorted(fileNames):
            filePath = rootPath / fileName
            if filePath.suffix in {".pyc", ".pyo"} or not filePath.is_file():
                continue
            relative = filePath.relative_to(path).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with filePath.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def readJson(path):
    """Read a JSON object."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RunRecordError("Cannot read {}: {}".format(path, exc))
    except json.JSONDecodeError as exc:
        raise RunRecordError(
            "Invalid JSON in {} at line {}: {}".format(path, exc.lineno, exc.msg)
        )
    if not isinstance(value, dict):
        raise RunRecordError("{} must contain a JSON object.".format(path))
    return value


def writeJson(path, value):
    """Atomically write formatted JSON with restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(str(temporary), 0o600)
    temporary.replace(path)


def writeText(path, value, executable=False):
    """Atomically write text with restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(str(temporary), 0o700 if executable else 0o600)
    temporary.replace(path)


def appendText(path, value):
    """Append text and retain restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(value)
    os.chmod(str(path), 0o600)


def appendJsonl(path, value):
    """Append one compact JSON object."""
    appendText(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
    )


def readJsonl(path):
    """Read JSON objects from a JSONL file."""
    if not path.exists():
        return []
    records = []
    for lineNumber, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RunRecordError(
                "Invalid JSONL in {} at line {}: {}".format(
                    path, lineNumber, exc.msg
                )
            )
        if not isinstance(value, dict):
            raise RunRecordError(
                "{} line {} is not an object.".format(path, lineNumber)
            )
        records.append(value)
    return records


def shellJoin(arguments):
    """Quote an argv list for POSIX shell replay."""
    return " ".join(shlex.quote(str(value)) for value in arguments)


def relativeToRun(runDir, path):
    """Return a run-relative POSIX path."""
    return path.relative_to(runDir).as_posix()


def recordedArtifact(value, cwd, afterExecution):
    """Normalize a command input/output path and record observed file state."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    path = path.absolute()
    record = {
        "path": str(path),
        "exists": path.exists(),
        "observed": "after" if afterExecution else "before",
    }
    if path.is_file():
        record["sizeBytes"] = path.stat().st_size
        record["sha256"] = sha256File(path)
    elif path.is_dir():
        record["artifactType"] = "directory"
    return record


def loadState(runDir, requireRunning=True):
    """Load and minimally validate recorder state."""
    statePath = runDir / ".run-state.json"
    state = readJson(statePath)
    if state.get("recordSchemaVersion") != RECORD_SCHEMA_VERSION:
        raise RunRecordError("Unsupported run record schema.")
    if requireRunning and state.get("status") != "running":
        raise RunRecordError("Run is not open for recording.")
    return state


def uniqueRunDirectory(outputRoot, skillName, runId):
    """Create and return a collision-safe run directory."""
    baseName = "{}-{}".format(skillName, runId)
    candidate = outputRoot / baseName
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = outputRoot / "{}-{}".format(baseName, suffix)
    candidate.mkdir(parents=True)
    os.chmod(str(candidate), 0o700)
    (candidate / "logs").mkdir()
    os.chmod(str(candidate / "logs"), 0o700)
    return candidate


def commandLogPath(runDir):
    """Return the JSONL command log path."""
    return runDir / "logs" / "commands.jsonl"


def actionLogPath(runDir):
    """Return the JSONL action log path."""
    return runDir / "logs" / "actions.jsonl"


def skillLogPath(runDir):
    """Return the JSONL composed-skill log path."""
    return runDir / "logs" / "skills.jsonl"


def versionLogPath(runDir):
    """Return the JSONL resolved-version log path."""
    return runDir / "logs" / "versions.jsonl"


def parameterLogPath(runDir):
    """Return the JSONL effective-parameter log path."""
    return runDir / "logs" / "parameters.jsonl"


def replayHeader(runId):
    """Render the stable replay-script header."""
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        "# Replay commands that completed successfully during run {}.\n"
        "# Supply values for any explicitly redacted environment placeholders.\n\n"
    ).format(runId)


def replayEntry(record):
    """Render one command's replay-script entry."""
    header = "# Step {}: {}\n".format(
        record.get("stepId"), record.get("description")
    )
    if record.get("exitCode") == 0:
        body = "(cd {} && {})\n\n".format(
            shlex.quote(str(record.get("cwd"))),
            record.get("displayCommand"),
        )
    else:
        body = (
            "# Failed with exit code {}; preserved in logs/commands.jsonl.\n"
            "# Failed command: (cd {} && {})\n\n"
        ).format(
            record.get("exitCode"),
            shlex.quote(str(record.get("cwd"))),
            record.get("displayCommand"),
        )
    return header + body


def plainCommandEntry(record):
    """Render one command's stable plain-text log entry."""
    return (
        "[{started}] COMMAND {index} {step}\n"
        "sequence: {sequence}\n"
        "category: {category}\n"
        "description: {description}\n"
        "cwd: {cwd}\n"
        "command: {command}\n"
        "redacted: {redacted}\n"
        "retry_of: {retry}\n"
        "parameters: {parameters}\n"
        "consumes: {consumes}\n"
        "produces: {produces}\n"
        "ended: {ended}\n"
        "exit_code: {exit_code}\n"
        "stdout: {stdout}\n"
        "stderr: {stderr}\n\n"
    ).format(
        started=record.get("startedAt"),
        index=record.get("index"),
        step=record.get("stepId"),
        sequence=record.get("sequence"),
        category=record.get("category"),
        description=record.get("description"),
        cwd=record.get("cwd"),
        command=record.get("displayCommand"),
        redacted=record.get("displayCommandRedacted"),
        retry=record.get("retryOf") or "",
        parameters=json.dumps(
            record.get("parameters", {}), ensure_ascii=False, sort_keys=True
        ),
        consumes=json.dumps(
            record.get("consumes", []), ensure_ascii=False, sort_keys=True
        ),
        produces=json.dumps(
            record.get("produces", []), ensure_ascii=False, sort_keys=True
        ),
        ended=record.get("endedAt"),
        exit_code=record.get("exitCode"),
        stdout=record.get("stdoutLog"),
        stderr=record.get("stderrLog"),
    )


def plainActionEntry(record):
    """Render one action's stable plain-text log entry."""
    return (
        "[{recorded}] ACTION {index} {step}\n"
        "sequence: {sequence}\n"
        "kind: {kind}\n"
        "description: {description}\n"
        "details: {details}\n"
        "status: {status}\n\n"
    ).format(
        recorded=record.get("recordedAt"),
        index=record.get("index"),
        step=record.get("stepId"),
        sequence=record.get("sequence"),
        kind=record.get("kind"),
        description=record.get("description"),
        details=record.get("details") or "",
        status=record.get("status"),
    )


def plainParameterEntry(record):
    """Render one effective-parameter record."""
    return (
        "[{recorded}] PARAMETER {name}\n"
        "sequence: {sequence}\n"
        "value: {value}\n"
        "source: {source}\n"
        "description: {description}\n\n"
    ).format(
        recorded=record.get("recordedAt"),
        name=record.get("name"),
        sequence=record.get("sequence"),
        value=json.dumps(record.get("value"), ensure_ascii=False, sort_keys=True),
        source=record.get("source"),
        description=record.get("description"),
    )


def parseParameterAssignments(values):
    """Parse repeated NAME=JSON command parameter assignments."""
    parameters = {}
    for value in values:
        if "=" not in value:
            raise RunRecordError(
                "Command parameter must use NAME=JSON syntax: {}".format(value)
            )
        name, raw = value.split("=", 1)
        if not name or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", name):
            raise RunRecordError("Invalid command parameter name: {}".format(name))
        if name in parameters:
            raise RunRecordError("Duplicate command parameter: {}".format(name))
        try:
            parameters[name] = json.loads(raw)
        except json.JSONDecodeError:
            raise RunRecordError(
                "Command parameter value must be valid JSON: {}".format(value)
            )
    return parameters


def requireUniqueStep(runDir, stepId):
    """Reject duplicate command/action step identifiers."""
    existing = readJsonl(commandLogPath(runDir)) + readJsonl(actionLogPath(runDir))
    if any(record.get("stepId") == stepId for record in existing):
        raise RunRecordError("Duplicate step ID: {}".format(stepId))


def initRun(args):
    """Initialize a generated-skill runtime record."""
    outputRoot = Path(args.outputRoot).expanduser().absolute()
    skillDir = Path(args.skillDir).expanduser().absolute()
    requestFile = Path(args.requestFile).expanduser().absolute()
    workflowFile = (
        Path(args.workflowFile).expanduser().absolute()
        if args.workflowFile
        else None
    )
    packagePath = skillDir / "skill-package.json"
    if not skillDir.is_dir():
        raise RunRecordError("Skill directory does not exist: {}".format(skillDir))
    if not packagePath.is_file():
        raise RunRecordError(
            "Generated skill metadata is missing: {}".format(packagePath)
        )
    if not requestFile.is_file():
        raise RunRecordError("Request file does not exist: {}".format(requestFile))
    if requestFile.stat().st_size == 0:
        raise RunRecordError("Request file must not be empty.")
    if workflowFile is not None and not workflowFile.is_file():
        raise RunRecordError("Workflow file does not exist: {}".format(workflowFile))
    if args.requestCapture == "verbatim" and args.redaction:
        raise RunRecordError("Verbatim request capture cannot declare redactions.")
    if args.requestCapture == "sanitized" and not args.redaction:
        raise RunRecordError(
            "Sanitized request capture requires at least one --redaction."
        )
    if args.requestCapture == "sanitized" and not args.originalRequestSha256:
        raise RunRecordError(
            "Sanitized request capture requires --originalRequestSha256."
        )
    if args.originalRequestSha256 and not re.fullmatch(
        r"[a-fA-F0-9]{64}", args.originalRequestSha256
    ):
        raise RunRecordError("originalRequestSha256 must be a SHA-256 hex digest.")
    requestSourceSha256 = sha256File(requestFile)
    if (
        args.requestCapture == "verbatim"
        and args.originalRequestSha256
        and args.originalRequestSha256.lower() != requestSourceSha256
    ):
        raise RunRecordError(
            "Verbatim originalRequestSha256 must match the request file."
        )
    if (
        args.requestCapture == "sanitized"
        and args.originalRequestSha256.lower() == requestSourceSha256
    ):
        raise RunRecordError(
            "Sanitized request hash must differ from originalRequestSha256."
        )

    package = readJson(packagePath)
    skillName = package.get("name")
    if not isinstance(skillName, str) or not skillName:
        raise RunRecordError("skill-package.json has no valid name.")
    runId = args.runId or defaultRunId()
    if not STEP_ID_PATTERN.fullmatch(runId):
        raise RunRecordError(
            "runId may contain only letters, numbers, dot, underscore, and hyphen."
        )

    outputRoot.mkdir(parents=True, exist_ok=True)
    runDir = uniqueRunDirectory(outputRoot, skillName, runId)
    requestTarget = runDir / "agent_request.txt"
    shutil.copyfile(str(requestFile), str(requestTarget))
    os.chmod(str(requestTarget), 0o600)
    requestSha256 = sha256File(requestTarget)
    originalSha256 = args.originalRequestSha256 or requestSha256
    if not re.fullmatch(r"[a-fA-F0-9]{64}", originalSha256):
        raise RunRecordError("originalRequestSha256 must be a SHA-256 hex digest.")

    workflowTarget = runDir / "agent_workflow.md"
    if workflowFile is not None:
        shutil.copyfile(str(workflowFile), str(workflowTarget))
        os.chmod(str(workflowTarget), 0o600)
    else:
        writeText(
            workflowTarget,
            "# Agent Workflow\n\n"
            "- Run initialized: {}\n"
            "- Add decisions, branches, composed skills, manual actions, and "
            "interpretation notes as work proceeds.\n".format(utcNow()),
        )

    writeText(runDir / "commands.sh", replayHeader(runId), executable=True)
    writeText(runDir / "logs" / "commands.log", "")
    writeText(runDir / "logs" / "commands.jsonl", "")
    writeText(runDir / "logs" / "actions.jsonl", "")
    writeText(runDir / "logs" / "skills.jsonl", "")
    writeText(runDir / "logs" / "versions.jsonl", "")
    writeText(runDir / "logs" / "parameters.jsonl", "")

    mainSkill = {
        "name": skillName,
        "version": package.get("version"),
        "path": str(skillDir),
        "sha256": sha256Directory(skillDir),
        "commit": package.get("sourceCommit"),
        "role": "primary",
    }
    state = {
        "recordSchemaVersion": RECORD_SCHEMA_VERSION,
        "runId": runId,
        "status": "running",
        "startedAt": utcNow(),
        "skill": mainSkill,
        "packaging": package.get("packaging"),
        "dependencies": package.get("dependencies", []),
        "commandExecutionExpected": package.get(
            "commandExecutionExpected", False
        ),
        "runtimeEnvironment": package.get("runtimeEnvironment"),
        "runtimeHost": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "pythonVersion": platform.python_version(),
            "pythonExecutable": sys.executable,
            "scheduler": {
                "lsfJobId": os.environ.get("LSB_JOBID"),
                "slurmJobId": os.environ.get("SLURM_JOB_ID"),
            },
        },
        "request": {
            "path": "agent_request.txt",
            "captureMode": args.requestCapture,
            "sha256": requestSha256,
            "originalSha256": originalSha256,
            "redactions": list(args.redaction or []),
        },
    }
    writeJson(runDir / ".run-state.json", state)
    print(str(runDir))
    return 0


def teeStream(source, destination, terminal):
    """Copy a subprocess byte stream to a file and terminal."""
    while True:
        chunk = source.read(65536)
        if not chunk:
            break
        destination.write(chunk)
        destination.flush()
        try:
            terminal.buffer.write(chunk)
            terminal.buffer.flush()
        except (AttributeError, BrokenPipeError):
            pass


def executeCommand(args):
    """Execute one command while recording exact context and outcome."""
    runDir = Path(args.runDir).expanduser().absolute()
    state = loadState(runDir)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise RunRecordError("exec requires a command after --.")
    if not STEP_ID_PATTERN.fullmatch(args.stepId):
        raise RunRecordError("Invalid stepId: {}".format(args.stepId))
    requireUniqueStep(runDir, args.stepId)
    existingCommands = readJsonl(commandLogPath(runDir))
    existingActions = readJsonl(actionLogPath(runDir))
    if args.retryOf:
        prior = [
            record
            for record in existingCommands
            if record.get("stepId") == args.retryOf
        ]
        if not prior:
            raise RunRecordError(
                "retryOf does not reference an earlier command: {}".format(
                    args.retryOf
                )
            )
        if prior[0].get("status") != "failed":
            raise RunRecordError("retryOf must reference a failed command.")
    parameters = parseParameterAssignments(args.parameter)

    cwd = Path(args.cwd or os.getcwd()).expanduser().absolute()
    if not cwd.is_dir():
        raise RunRecordError("Command working directory is missing: {}".format(cwd))
    displayCommand = args.displayCommand or shellJoin(command)
    if "\n" in displayCommand or "\r" in displayCommand:
        raise RunRecordError("displayCommand must be one line.")
    if args.redacted and not args.displayCommand:
        raise RunRecordError("--redacted requires an explicit --displayCommand.")
    if args.displayCommand and not args.redacted:
        raise RunRecordError(
            "--displayCommand is allowed only with --redacted; otherwise the "
            "recorder derives the exact command from argv."
        )

    consumedArtifacts = [
        recordedArtifact(value, cwd, False) for value in args.consumes
    ]
    commandIndex = len(existingCommands) + 1
    sequence = (
        len(existingCommands)
        + len(existingActions)
        + len(readJsonl(parameterLogPath(runDir)))
        + 1
    )
    logStem = "{:03d}-{}".format(commandIndex, args.stepId)
    stdoutPath = runDir / "logs" / (logStem + ".stdout.log")
    stderrPath = runDir / "logs" / (logStem + ".stderr.log")
    startedAt = utcNow()
    exitCode = 127
    osError = None
    with stdoutPath.open("wb") as stdoutHandle, stderrPath.open("wb") as stderrHandle:
        os.chmod(str(stdoutPath), 0o600)
        os.chmod(str(stderrPath), 0o600)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdoutThread = threading.Thread(
                target=teeStream,
                args=(process.stdout, stdoutHandle, sys.stdout),
            )
            stderrThread = threading.Thread(
                target=teeStream,
                args=(process.stderr, stderrHandle, sys.stderr),
            )
            stdoutThread.start()
            stderrThread.start()
            exitCode = process.wait()
            stdoutThread.join()
            stderrThread.join()
        except OSError as exc:
            osError = str(exc)
            stderrHandle.write((osError + "\n").encode("utf-8", errors="replace"))
            print(osError, file=sys.stderr)

    endedAt = utcNow()
    argvSha256 = None
    if not args.redacted:
        argvSha256 = sha256Bytes(
            b"\0".join(str(value).encode("utf-8") for value in command)
        )
    record = {
        "type": "command",
        "index": commandIndex,
        "sequence": sequence,
        "stepId": args.stepId,
        "category": args.category,
        "description": args.description,
        "displayCommand": displayCommand,
        "displayCommandRedacted": bool(args.redacted),
        "executedArgvSha256": argvSha256,
        "executionIdentityWithheld": bool(args.redacted),
        "cwd": str(cwd),
        "startedAt": startedAt,
        "endedAt": endedAt,
        "exitCode": exitCode,
        "status": "success" if exitCode == 0 else "failed",
        "retryOf": args.retryOf,
        "parameters": parameters,
        "consumes": consumedArtifacts,
        "produces": [
            recordedArtifact(value, cwd, True) for value in args.produces
        ],
        "stdoutLog": relativeToRun(runDir, stdoutPath),
        "stderrLog": relativeToRun(runDir, stderrPath),
        "environment": {
            "pythonExecutable": sys.executable,
            "condaPrefix": os.environ.get("CONDA_PREFIX"),
            "virtualEnv": os.environ.get("VIRTUAL_ENV"),
            "container": os.environ.get("APPTAINER_CONTAINER")
            or os.environ.get("SINGULARITY_CONTAINER"),
        },
    }
    if osError is not None:
        record["launchError"] = osError
    appendJsonl(commandLogPath(runDir), record)
    appendText(runDir / "logs" / "commands.log", plainCommandEntry(record))
    appendText(runDir / "commands.sh", replayEntry(record))
    os.chmod(str(runDir / "commands.sh"), 0o700)
    return exitCode


def recordAction(args):
    """Record a non-shell or manual action."""
    runDir = Path(args.runDir).expanduser().absolute()
    loadState(runDir)
    if not STEP_ID_PATTERN.fullmatch(args.stepId):
        raise RunRecordError("Invalid stepId: {}".format(args.stepId))
    requireUniqueStep(runDir, args.stepId)
    records = readJsonl(actionLogPath(runDir))
    sequence = (
        len(readJsonl(commandLogPath(runDir)))
        + len(records)
        + len(readJsonl(parameterLogPath(runDir)))
        + 1
    )
    record = {
        "type": "action",
        "index": len(records) + 1,
        "sequence": sequence,
        "stepId": args.stepId,
        "kind": args.kind,
        "description": args.description,
        "details": args.details,
        "status": args.status,
        "recordedAt": utcNow(),
    }
    appendJsonl(actionLogPath(runDir), record)
    appendText(runDir / "logs" / "commands.log", plainActionEntry(record))
    return 0


def recordParameter(args):
    """Record an effective parameter that is not attached to one command."""
    runDir = Path(args.runDir).expanduser().absolute()
    loadState(runDir)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", args.name):
        raise RunRecordError("Invalid parameter name: {}".format(args.name))
    if not args.source.strip() or not args.description.strip():
        raise RunRecordError("Parameter source and description must not be empty.")
    try:
        value = json.loads(args.valueJson)
    except json.JSONDecodeError as exc:
        raise RunRecordError(
            "valueJson is invalid JSON: {}".format(exc.msg)
        )
    records = readJsonl(parameterLogPath(runDir))
    sequence = (
        len(readJsonl(commandLogPath(runDir)))
        + len(readJsonl(actionLogPath(runDir)))
        + len(records)
        + 1
    )
    record = {
        "type": "parameter",
        "name": args.name,
        "value": value,
        "source": args.source,
        "description": args.description,
        "sequence": sequence,
        "recordedAt": utcNow(),
    }
    appendJsonl(parameterLogPath(runDir), record)
    appendText(runDir / "logs" / "commands.log", plainParameterEntry(record))
    return 0


def localSkillIdentity(skillPath):
    """Read a local skill's declared name/version from package metadata or YAML."""
    packagePath = skillPath / "skill-package.json"
    if packagePath.is_file():
        package = readJson(packagePath)
        name = package.get("name")
        version = package.get("version")
    else:
        skillMarkdown = skillPath / "SKILL.md"
        if not skillMarkdown.is_file():
            raise RunRecordError(
                "Composed skill has no skill-package.json or SKILL.md."
            )
        text = skillMarkdown.read_text(encoding="utf-8")
        nameMatch = re.search(r"(?m)^name:\s*[\"']?([^\"'\n]+)[\"']?\s*$", text)
        versionMatch = re.search(
            r"(?m)^  version:\s*[\"']?([^\"'\n]+)[\"']?\s*$",
            text,
        )
        name = nameMatch.group(1).strip() if nameMatch else None
        version = versionMatch.group(1).strip() if versionMatch else None
    if not isinstance(name, str) or not name:
        raise RunRecordError("Composed skill has no declared name.")
    if not isinstance(version, str) or not version:
        raise RunRecordError("Composed skill has no declared version.")
    return name, version


def recordSkill(args):
    """Record a composed skill with version and source identity."""
    runDir = Path(args.runDir).expanduser().absolute()
    loadState(runDir)
    skillPath = Path(args.path).expanduser().absolute()
    if not skillPath.is_dir():
        raise RunRecordError("Composed skill path is missing: {}".format(skillPath))
    computedDigest = sha256Directory(skillPath)
    digest = args.sha256 or computedDigest
    if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise RunRecordError("Skill sha256 must be a SHA-256 hex digest.")
    if digest.lower() != computedDigest:
        raise RunRecordError("Composed skill sha256 does not match its package.")
    declaredName, declaredVersion = localSkillIdentity(skillPath)
    if declaredName != args.name:
        raise RunRecordError("Composed skill name does not match its package.")
    if declaredVersion != args.version:
        raise RunRecordError("Composed skill version does not match its package.")
    record = {
        "name": args.name,
        "version": args.version,
        "path": str(skillPath),
        "sha256": digest,
        "commit": args.commit,
        "role": args.role,
        "recordedAt": utcNow(),
    }
    appendJsonl(skillLogPath(runDir), record)
    return 0


def recordVersion(args):
    """Record one resolved tool, interpreter, package, or reference version."""
    runDir = Path(args.runDir).expanduser().absolute()
    loadState(runDir)
    sourceCommands = [
        record
        for record in readJsonl(commandLogPath(runDir))
        if record.get("stepId") == args.sourceStep
    ]
    if not sourceCommands or sourceCommands[0].get("status") != "success":
        raise RunRecordError(
            "Version sourceStep must reference a successful command."
        )
    sourceCommand = sourceCommands[0]
    if args.versionCommand not in sourceCommand.get("displayCommand", ""):
        raise RunRecordError(
            "versionCommand is not represented in the source command."
        )
    evidencePath = Path(args.evidenceFile).expanduser().absolute()
    if not evidencePath.is_file() or evidencePath.stat().st_size == 0:
        raise RunRecordError("Version evidence file is missing or empty.")
    try:
        relativeEvidence = evidencePath.resolve().relative_to(runDir.resolve())
    except ValueError:
        raise RunRecordError(
            "Version evidence file must be inside the run directory."
        )
    producedPaths = {
        Path(value["path"]).resolve()
        for value in sourceCommand.get("produces", [])
        if isinstance(value, dict) and isinstance(value.get("path"), str)
    }
    if evidencePath.resolve() not in producedPaths:
        raise RunRecordError(
            "Version evidence must be declared with --produces on sourceStep."
        )
    evidenceText = evidencePath.read_text(
        encoding="utf-8", errors="replace"
    )
    if args.version not in evidenceText:
        raise RunRecordError(
            "Claimed version is absent from the recorded evidence file."
        )
    resolvedPath = Path(args.path).expanduser().absolute() if args.path else None
    pathRequiredKinds = {
        "interpreter",
        "cli",
        "pipeline",
        "reference",
        "codebase",
    }
    if args.kind in pathRequiredKinds and resolvedPath is None:
        raise RunRecordError(
            "Resolved path is required for version kind {}.".format(args.kind)
        )
    if resolvedPath is not None and not resolvedPath.exists():
        raise RunRecordError("Resolved version path is missing: {}".format(resolvedPath))
    record = {
        "name": args.name,
        "version": args.version,
        "kind": args.kind,
        "path": str(resolvedPath) if resolvedPath is not None else None,
        "versionCommand": args.versionCommand,
        "sourceStep": args.sourceStep,
        "evidenceFile": relativeEvidence.as_posix(),
        "evidenceSha256": sha256File(evidencePath),
        "recordedAt": utcNow(),
    }
    appendJsonl(versionLogPath(runDir), record)
    return 0


def validateSummary(summary):
    """Validate the structured summary used to finalize a run."""
    missing = sorted(SUMMARY_FIELDS - set(summary))
    unknown = sorted(set(summary) - SUMMARY_FIELDS)
    if missing:
        raise RunRecordError(
            "Summary is missing required fields: {}".format(", ".join(missing))
        )
    if unknown:
        raise RunRecordError(
            "Summary has unknown fields: {}".format(", ".join(unknown))
        )
    if summary.get("status") not in {"success", "partial", "failed"}:
        raise RunRecordError("Summary status must be success, partial, or failed.")
    if not isinstance(summary.get("parameters"), dict):
        raise RunRecordError("Summary parameters must be an object.")
    if summary.get("environmentSnapshot") is not None and not isinstance(
        summary.get("environmentSnapshot"), dict
    ):
        raise RunRecordError("Summary environmentSnapshot must be an object or null.")
    for field in LIST_FIELDS:
        if not isinstance(summary.get(field), list):
            raise RunRecordError("Summary {} must be an array.".format(field))
    for field in (
        "whatWasDone",
        "findings",
        "warnings",
        "assumptions",
        "manualSteps",
        "limitations",
    ):
        if not all(isinstance(item, str) and item.strip() for item in summary[field]):
            raise RunRecordError(
                "Summary {} must contain non-empty strings.".format(field)
            )
    if not summary["whatWasDone"]:
        raise RunRecordError("Summary whatWasDone must not be empty.")
    for index, record in enumerate(summary["skillsUsed"]):
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("name"), str)
            or not record["name"].strip()
            or not isinstance(record.get("version"), str)
            or not record["version"].strip()
            or not isinstance(record.get("path"), str)
            or not record["path"].strip()
            or not isinstance(record.get("sha256"), str)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", record["sha256"])
            or not isinstance(record.get("role"), str)
            or not record["role"].strip()
        ):
            raise RunRecordError(
                "Summary skillsUsed[{}] needs name, version, path, SHA-256, "
                "and role.".format(index)
            )
    for index, record in enumerate(summary["versions"]):
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("name"), str)
            or not isinstance(record.get("version"), str)
            or not record["name"].strip()
            or not record["version"].strip()
        ):
            raise RunRecordError(
                "Summary versions[{}] needs name and version strings.".format(index)
            )


def enrichArtifacts(records):
    """Add file size and SHA-256 to input/output records when possible."""
    enriched = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RunRecordError(
                "Input/output record {} must be an object.".format(index)
            )
        value = dict(record)
        pathValue = value.get("path")
        if isinstance(pathValue, str):
            path = Path(pathValue).expanduser()
            value["existsAtFinalization"] = path.exists()
            if path.is_file():
                value.setdefault("sizeBytes", path.stat().st_size)
                value.setdefault("sha256", sha256File(path))
            elif path.exists():
                value.setdefault("artifactType", "directory")
        enriched.append(value)
    return enriched


def mergeIdentityRecords(recorded, summarized):
    """Merge recorded and summarized identity lists without exact duplicates."""
    merged = []
    seen = set()
    for value in list(recorded) + list(summarized):
        if not isinstance(value, dict):
            raise RunRecordError("Skill/version records must be objects.")
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            merged.append(value)
            seen.add(key)
    return merged


def normalizeSkillIdentity(record):
    """Validate a composed-skill identity against its local package."""
    required = ("name", "version", "path", "sha256", "role")
    if not all(
        isinstance(record.get(field), str) and record[field].strip()
        for field in required
    ):
        raise RunRecordError(
            "Composed skill identity needs name, version, path, SHA-256, and role."
        )
    if not re.fullmatch(r"[a-fA-F0-9]{64}", record["sha256"]):
        raise RunRecordError("Composed skill identity has an invalid SHA-256.")
    path = Path(record["path"]).expanduser().absolute()
    if not path.is_dir():
        raise RunRecordError("Composed skill path is missing: {}".format(path))
    declaredName, declaredVersion = localSkillIdentity(path)
    if declaredName != record["name"] or declaredVersion != record["version"]:
        raise RunRecordError(
            "Composed skill identity does not match its local package."
        )
    actualDigest = sha256Directory(path)
    if actualDigest != record["sha256"].lower():
        raise RunRecordError(
            "Composed skill identity hash no longer matches {}".format(path)
        )
    normalized = dict(record)
    normalized["path"] = str(path)
    normalized["sha256"] = actualDigest
    return normalized


def mergeSkillRecords(recorded, summarized):
    """Normalize and deduplicate composed-skill identity records."""
    merged = []
    seen = set()
    for value in list(recorded) + list(summarized):
        if not isinstance(value, dict):
            raise RunRecordError("Composed skill records must be objects.")
        normalized = normalizeSkillIdentity(value)
        key = (
            normalized["name"],
            normalized["version"],
            normalized["path"],
            normalized["sha256"],
            normalized.get("commit"),
            normalized["role"],
        )
        if key not in seen:
            merged.append(normalized)
            seen.add(key)
    return merged


def normalizedIdentityName(value):
    """Normalize dependency/tool names for version-record coverage."""
    candidate = value.split("::")[-1].strip()
    candidate = re.split(
        r"\s+@\s+|(?<=[A-Za-z0-9_./-])@(?=[vV0-9])|[<>=!~\s]",
        candidate,
        maxsplit=1,
    )[0]
    candidate = candidate.split("[", 1)[0]
    return re.sub(r"[-_.]+", "-", candidate).lower()


def missingDependencyVersions(dependencies, versions):
    """Return required versioned dependency names not represented at runtime."""
    versionNames = {
        normalizedIdentityName(record.get("name", ""))
        for record in versions
        if isinstance(record, dict) and isinstance(record.get("name"), str)
    }
    versionedKinds = {
        "public_tool",
        "public_pipeline",
        "custom_integrated",
        "reference_data",
    }
    missing = []
    for dependency in dependencies:
        if (
            not isinstance(dependency, dict)
            or dependency.get("required") is not True
            or dependency.get("kind") not in versionedKinds
        ):
            continue
        candidates = {
            normalizedIdentityName(value)
            for value in (
                dependency.get("name"),
                dependency.get("environmentPackage"),
            )
            if isinstance(value, str)
        }
        if not candidates.intersection(versionNames):
            missing.append(str(dependency.get("name")))
    return sorted(set(missing))


def effectiveRecordedParameters(commands, parameterRecords):
    """Return final effective parameters in global recording order."""
    events = []
    for command in commands:
        if command.get("status") != "success":
            continue
        events.append(
            (
                command.get("sequence", 0),
                command.get("parameters", {}),
            )
        )
    for record in parameterRecords:
        events.append(
            (
                record.get("sequence", 0),
                {record.get("name"): record.get("value")},
            )
        )
    effective = {}
    for _, values in sorted(events, key=lambda item: item[0]):
        if isinstance(values, dict):
            effective.update(values)
    return effective


def recordedArtifactPaths(commands, field, successfulOnly):
    """Return normalized paths declared on command records."""
    paths = set()
    for command in commands:
        if successfulOnly and command.get("status") != "success":
            continue
        for artifact in command.get(field, []):
            if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
                paths.add(Path(artifact["path"]).resolve())
    return paths


def validateEnvironmentSnapshot(path, snapshotFormat):
    """Validate that a resolved environment snapshot matches its declared format."""
    text = path.read_text(encoding="utf-8", errors="replace")
    nonemptyLines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if snapshotFormat == "conda-explicit":
        if "@EXPLICIT" not in nonemptyLines or not any(
            re.match(r"https?://|file://", line)
            for line in nonemptyLines
            if line != "@EXPLICIT"
        ):
            raise RunRecordError(
                "conda-explicit snapshot needs @EXPLICIT and package URLs."
            )
    elif snapshotFormat == "conda-environment-export":
        if "dependencies:" not in text or "channels:" not in text:
            raise RunRecordError(
                "conda-environment-export snapshot needs channels and dependencies."
            )
    elif snapshotFormat == "pip-freeze":
        if not any(
            re.match(r"^[A-Za-z0-9_.-]+(?:\[[^]]+\])?(?:==| @ )\S+", line)
            for line in nonemptyLines
        ):
            raise RunRecordError(
                "pip-freeze snapshot needs at least one resolved package entry."
            )
    elif snapshotFormat == "container-digest":
        if not re.search(r"@sha256:[a-fA-F0-9]{64}", text):
            raise RunRecordError(
                "container-digest snapshot needs an immutable SHA-256 digest."
            )
    elif snapshotFormat == "system-versions":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RunRecordError(
                "system-versions snapshot must be JSON: {}".format(exc.msg)
            )
        if not isinstance(value, dict) or not value or not all(
            isinstance(name, str)
            and isinstance(version, str)
            and name
            and version
            for name, version in value.items()
        ):
            raise RunRecordError(
                "system-versions snapshot needs a non-empty name/version object."
            )
    elif snapshotFormat == "codebase-environment":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RunRecordError(
                "codebase-environment snapshot must be JSON: {}".format(exc.msg)
            )
        required = {"environmentFile", "environmentFileSha256", "codebaseCommit"}
        if not isinstance(value, dict) or not required.issubset(value):
            raise RunRecordError(
                "codebase-environment snapshot lacks file/hash/commit identity."
            )
        if not re.fullmatch(
            r"[a-fA-F0-9]{64}", str(value["environmentFileSha256"])
        ):
            raise RunRecordError(
                "codebase-environment file hash is not a SHA-256 digest."
            )


def snapshotVersionMap(path, snapshotFormat):
    """Extract resolved package versions from supported snapshot formats."""
    text = path.read_text(encoding="utf-8", errors="replace")
    versions = {}
    if snapshotFormat == "conda-explicit":
        for line in text.splitlines():
            value = line.strip()
            if not re.match(r"https?://|file://", value):
                continue
            filename = Path(urlparse(value.split("#", 1)[0]).path).name
            for suffix in (".tar.bz2", ".conda"):
                if filename.endswith(suffix):
                    filename = filename[: -len(suffix)]
                    break
            parts = filename.rsplit("-", 2)
            if len(parts) == 3:
                versions[normalizedIdentityName(parts[0])] = parts[1]
    elif snapshotFormat == "conda-environment-export":
        for line in text.splitlines():
            match = re.match(
                r"^\s*-\s*([A-Za-z0-9_.-]+)=([^=\s]+)(?:=.*)?$",
                line,
            )
            if match:
                versions[normalizedIdentityName(match.group(1))] = match.group(2)
    elif snapshotFormat == "pip-freeze":
        for line in text.splitlines():
            match = re.match(
                r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==(\S+)$",
                line.strip(),
            )
            if match:
                versions[normalizedIdentityName(match.group(1))] = match.group(2)
    elif snapshotFormat == "system-versions":
        value = json.loads(text)
        versions.update(
            {
                normalizedIdentityName(name): version
                for name, version in value.items()
            }
        )
    return versions


def dependencyVersionRecord(dependency, versions):
    """Return a resolved version record for one dependency, when present."""
    candidates = {
        normalizedIdentityName(value)
        for value in (
            dependency.get("name"),
            dependency.get("environmentPackage"),
        )
        if isinstance(value, str)
    }
    for record in versions:
        if (
            isinstance(record, dict)
            and isinstance(record.get("name"), str)
            and normalizedIdentityName(record["name"]) in candidates
        ):
            return record
    return None


def verifySnapshotVersionConsistency(
    dependencies,
    versions,
    manager,
    snapshotVersions,
):
    """Cross-check required package versions against the resolved snapshot."""
    if manager not in {"conda", "venv", "system"}:
        return
    for dependency in dependencies:
        if (
            not isinstance(dependency, dict)
            or dependency.get("required") is not True
            or dependency.get("kind") != "public_tool"
        ):
            continue
        packageName = dependency.get("environmentPackage")
        if not isinstance(packageName, str):
            continue
        normalizedName = normalizedIdentityName(packageName)
        snapshotVersion = snapshotVersions.get(normalizedName)
        if snapshotVersion is None:
            raise RunRecordError(
                "Resolved environment snapshot omits required package {}.".format(
                    packageName
                )
            )
        versionRecord = dependencyVersionRecord(dependency, versions)
        if versionRecord is None:
            continue
        claimed = str(versionRecord.get("version", "")).lstrip("vV")
        if claimed != str(snapshotVersion).lstrip("vV"):
            raise RunRecordError(
                "Resolved version for {} disagrees with environment snapshot: "
                "{} != {}.".format(packageName, claimed, snapshotVersion)
            )


def markdownList(values, emptyText="None."):
    """Render a string list."""
    if not values:
        return "- " + emptyText
    return "\n".join("- {}".format(value) for value in values)


def markdownObjects(values, emptyText="None."):
    """Render input/output/identity objects as concise bullets."""
    if not values:
        return "- " + emptyText
    lines = []
    for value in values:
        if not isinstance(value, dict):
            lines.append("- {}".format(value))
            continue
        label = value.get("name") or value.get("path") or "record"
        details = []
        for field in (
            "version",
            "kind",
            "role",
            "path",
            "sha256",
            "commit",
            "method",
            "format",
            "sizeBytes",
            "description",
        ):
            item = value.get(field)
            if item is not None and item != "":
                details.append("{}={}".format(field, item))
        lines.append(
            "- **{}**{}".format(label, ": " + "; ".join(details) if details else "")
        )
    return "\n".join(lines)


def renderSummaryMarkdown(summary, commands, actions):
    """Render the canonical human-readable run summary."""
    parameters = summary["parameters"]
    parameterLines = (
        "\n".join(
            "- **{}**: `{}`".format(
                key, json.dumps(parameters[key], ensure_ascii=False, sort_keys=True)
            )
            for key in sorted(parameters)
        )
        if parameters
        else "- None."
    )
    commandLines = []
    for command in commands:
        commandLines.append(
            "{}. **{}** — exit `{}`; cwd `{}`\n"
            "   - `{}`".format(
                command.get("index"),
                command.get("description"),
                command.get("exitCode"),
                command.get("cwd"),
                command.get("displayCommand"),
            )
        )
    for action in actions:
        commandLines.append(
            "{}. **Non-shell action: {}** — `{}`; {}".format(
                len(commandLines) + 1,
                action.get("description"),
                action.get("status"),
                action.get("details") or action.get("kind"),
            )
        )
    orderedWork = "\n".join(commandLines) if commandLines else "No work recorded."
    return (
        "# Run Summary\n\n"
        "- Run ID: `{run_id}`\n"
        "- Status: **{status}**\n"
        "- Skill: `{skill_name}` version `{skill_version}`\n"
        "- Started (UTC): `{started}`\n"
        "- Ended (UTC): `{ended}`\n"
        "- Request capture: `{request_capture}`\n\n"
        "## What was done\n\n{what_was_done}\n\n"
        "## Ordered commands and actions\n\n{ordered_work}\n\n"
        "## Findings\n\n{findings}\n\n"
        "## Effective parameters\n\n{parameters}\n\n"
        "## Inputs\n\n{inputs}\n\n"
        "## Outputs\n\n{outputs}\n\n"
        "## Skills used\n\n{skills}\n\n"
        "## Resolved versions\n\n{versions}\n\n"
        "## Resolved environment snapshot\n\n{environment_snapshot}\n\n"
        "## Warnings\n\n{warnings}\n\n"
        "## Assumptions\n\n{assumptions}\n\n"
        "## Manual steps\n\n{manual_steps}\n\n"
        "## Limitations\n\n{limitations}\n\n"
        "## Reproduction records\n\n"
        "- Replay commands: `commands.sh`\n"
        "- Machine manifest: `run_manifest.json`\n"
        "- Human manifest: `run_manifest.md`\n"
        "- Command events: `logs/commands.jsonl`\n"
        "- Plain command log: `logs/commands.log`\n"
    ).format(
        run_id=summary["runId"],
        status=summary["status"],
        skill_name=summary["skill"].get("name"),
        skill_version=summary["skill"].get("version"),
        started=summary["startedAt"],
        ended=summary["endedAt"],
        request_capture=summary["request"].get("captureMode"),
        what_was_done=markdownList(summary["whatWasDone"]),
        ordered_work=orderedWork,
        findings=markdownList(summary["findings"]),
        parameters=parameterLines,
        inputs=markdownObjects(summary["inputs"]),
        outputs=markdownObjects(summary["outputs"]),
        skills=markdownObjects(summary["skillsUsed"]),
        versions=markdownObjects(summary["versions"]),
        environment_snapshot=markdownObjects(
            [summary["environmentSnapshot"]]
            if summary["environmentSnapshot"] is not None
            else []
        ),
        warnings=markdownList(summary["warnings"]),
        assumptions=markdownList(summary["assumptions"]),
        manual_steps=markdownList(summary["manualSteps"]),
        limitations=markdownList(summary["limitations"]),
    )


def renderManifestMarkdown(manifest):
    """Render a human-readable counterpart to run_manifest.json."""
    commandLines = []
    for command in manifest["commands"]:
        commandLines.append(
            "{index}. **{step}** — `{status}` (exit `{exit_code}`)\n"
            "   - Purpose: {description}\n"
            "   - Category: `{category}`\n"
            "   - CWD: `{cwd}`\n"
            "   - Command: `{command}`\n"
            "   - Started/ended: `{started}` / `{ended}`\n"
            "   - Logs: `{stdout}`, `{stderr}`\n"
            "   - Retry of: `{retry}`".format(
                index=command.get("index"),
                step=command.get("stepId"),
                status=command.get("status"),
                exit_code=command.get("exitCode"),
                description=command.get("description"),
                category=command.get("category"),
                cwd=command.get("cwd"),
                command=command.get("displayCommand"),
                started=command.get("startedAt"),
                ended=command.get("endedAt"),
                stdout=command.get("stdoutLog"),
                stderr=command.get("stderrLog"),
                retry=command.get("retryOf") or "none",
            )
        )
    actionLines = []
    for action in manifest["actions"]:
        actionLines.append(
            "- **{step}** — `{kind}` / `{status}`: {description}; {details}".format(
                step=action.get("stepId"),
                kind=action.get("kind"),
                status=action.get("status"),
                description=action.get("description"),
                details=action.get("details") or "no extra details",
            )
        )
    parameters = (
        "\n".join(
            "- **{}**: `{}`".format(
                key,
                json.dumps(
                    manifest["parameters"][key],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            for key in sorted(manifest["parameters"])
        )
        if manifest["parameters"]
        else "- None."
    )
    request = manifest["request"]
    return (
        "# Run Manifest\n\n"
        "This is the human-readable counterpart to `run_manifest.json`.\n\n"
        "## Run identity\n\n"
        "- Run ID: `{run_id}`\n"
        "- Status: `{status}`\n"
        "- Started/ended (UTC): `{started}` / `{ended}`\n"
        "- Skill: `{skill}` version `{version}`\n"
        "- Skill path: `{skill_path}`\n"
        "- Skill SHA-256: `{skill_hash}`\n"
        "- Packaging: `{packaging}`\n\n"
        "## Request capture\n\n"
        "- File: `{request_path}`\n"
        "- Mode: `{request_mode}`\n"
        "- Saved SHA-256: `{request_hash}`\n"
        "- Original SHA-256: `{original_hash}`\n"
        "- Redactions: {redactions}\n\n"
        "## Runtime host\n\n"
        "```json\n{runtime_host}\n```\n\n"
        "## Requested runtime environment\n\n"
        "```json\n{runtime_environment}\n```\n\n"
        "## Declared dependencies\n\n"
        "```json\n{dependencies}\n```\n\n"
        "## Resolved environment snapshot\n\n{environment_snapshot}\n\n"
        "## Ordered commands\n\n{commands}\n\n"
        "## Non-shell and manual actions\n\n{actions}\n\n"
        "## Effective parameters\n\n{parameters}\n\n"
        "## Inputs\n\n{inputs}\n\n"
        "## Outputs\n\n{outputs}\n\n"
        "## Skills used\n\n{skills}\n\n"
        "## Resolved versions\n\n{versions}\n\n"
        "## Findings\n\n{findings}\n\n"
        "## Warnings and limitations\n\n"
        "### Warnings\n\n{warnings}\n\n"
        "### Assumptions\n\n{assumptions}\n\n"
        "### Manual steps\n\n{manual_steps}\n\n"
        "### Limitations\n\n{limitations}\n"
    ).format(
        run_id=manifest["runId"],
        status=manifest["status"],
        started=manifest["startedAt"],
        ended=manifest["endedAt"],
        skill=manifest["skill"].get("name"),
        version=manifest["skill"].get("version"),
        skill_path=manifest["skill"].get("path"),
        skill_hash=manifest["skill"].get("sha256"),
        packaging=manifest.get("packaging"),
        request_path=request.get("path"),
        request_mode=request.get("captureMode"),
        request_hash=request.get("sha256"),
        original_hash=request.get("originalSha256"),
        redactions=", ".join(request.get("redactions", [])) or "none",
        runtime_host=json.dumps(
            manifest.get("runtimeHost"), indent=2, ensure_ascii=False, sort_keys=True
        ),
        runtime_environment=json.dumps(
            manifest.get("runtimeEnvironment"),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ),
        dependencies=json.dumps(
            manifest.get("dependencies"),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ),
        environment_snapshot=markdownObjects(
            [manifest["environmentSnapshot"]]
            if manifest["environmentSnapshot"] is not None
            else []
        ),
        commands="\n".join(commandLines) if commandLines else "No commands recorded.",
        actions="\n".join(actionLines) if actionLines else "- None.",
        parameters=parameters,
        inputs=markdownObjects(manifest["inputs"]),
        outputs=markdownObjects(manifest["outputs"]),
        skills=markdownObjects(manifest["skillsUsed"]),
        versions=markdownObjects(manifest["versions"]),
        findings=markdownList(manifest["findings"]),
        warnings=markdownList(manifest["warnings"]),
        assumptions=markdownList(manifest["assumptions"]),
        manual_steps=markdownList(manifest["manualSteps"]),
        limitations=markdownList(manifest["limitations"]),
    )


def finalizeRun(args):
    """Finalize manifests and human/machine summaries."""
    runDir = Path(args.runDir).expanduser().absolute()
    state = loadState(runDir)
    summaryInput = readJson(Path(args.summaryFile).expanduser().absolute())
    validateSummary(summaryInput)
    commands = readJsonl(commandLogPath(runDir))
    actions = readJsonl(actionLogPath(runDir))
    parameterRecords = readJsonl(parameterLogPath(runDir))
    if not commands and not actions:
        raise RunRecordError(
            "Cannot finalize a run with no recorded commands or actions."
        )
    if (
        summaryInput["status"] == "success"
        and state.get("commandExecutionExpected") is True
        and not any(
            command.get("category") == "workflow"
            and command.get("status") == "success"
            for command in commands
        )
    ):
        raise RunRecordError(
            "Successful run requires at least one successful workflow command."
        )
    if (
        summaryInput["status"] == "success"
        and not summaryInput["findings"]
        and not summaryInput["outputs"]
    ):
        raise RunRecordError(
            "Successful run requires at least one finding or output."
        )
    effectiveParameters = effectiveRecordedParameters(commands, parameterRecords)
    if summaryInput["parameters"] != effectiveParameters:
        raise RunRecordError(
            "Summary parameters must exactly match recorded effective parameters."
        )

    endedAt = utcNow()
    mainSkill = state["skill"]
    additionalSkills = mergeSkillRecords(
        readJsonl(skillLogPath(runDir)), summaryInput["skillsUsed"]
    )
    skillsUsed = [mainSkill] + [
        value for value in additionalSkills if value.get("name") != mainSkill["name"]
    ]
    versions = mergeIdentityRecords(
        readJsonl(versionLogPath(runDir)), summaryInput["versions"]
    )
    runtimeEnvironment = state.get("runtimeEnvironment") or {}
    if runtimeEnvironment.get("manager") != "none" and not versions:
        raise RunRecordError(
            "Finalize requires resolved version records for this runtime environment."
        )
    missingVersions = missingDependencyVersions(
        state.get("dependencies", []), versions
    )
    if missingVersions:
        raise RunRecordError(
            "Missing resolved version records for required dependencies: "
            + ", ".join(missingVersions)
        )
    environmentSnapshot = summaryInput["environmentSnapshot"]
    if runtimeEnvironment.get("manager") != "none":
        if not isinstance(environmentSnapshot, dict):
            raise RunRecordError(
                "Finalize requires an environmentSnapshot for this runtime manager."
            )
        snapshotPathValue = environmentSnapshot.get("path")
        if not isinstance(snapshotPathValue, str):
            raise RunRecordError("environmentSnapshot requires a path.")
        snapshotMethod = environmentSnapshot.get("method")
        snapshotFormat = environmentSnapshot.get("format")
        if not isinstance(snapshotMethod, str) or not snapshotMethod.strip():
            raise RunRecordError("environmentSnapshot requires a method.")
        allowedFormats = {
            "conda": {"conda-explicit", "conda-environment-export"},
            "venv": {"pip-freeze"},
            "container": {"container-digest"},
            "system": {"system-versions"},
            "codebase": {"codebase-environment"},
        }
        if snapshotFormat not in allowedFormats.get(
            runtimeEnvironment.get("manager"), set()
        ):
            raise RunRecordError(
                "environmentSnapshot format is incompatible with runtime manager."
            )
        snapshotPath = Path(snapshotPathValue).expanduser().absolute()
        if not snapshotPath.is_file():
            raise RunRecordError(
                "Environment snapshot file is missing: {}".format(snapshotPath)
            )
        if snapshotPath.stat().st_size == 0:
            raise RunRecordError("Environment snapshot file must not be empty.")
        validateEnvironmentSnapshot(snapshotPath, snapshotFormat)
        verifySnapshotVersionConsistency(
            state.get("dependencies", []),
            versions,
            runtimeEnvironment.get("manager"),
            snapshotVersionMap(snapshotPath, snapshotFormat),
        )
        try:
            relativeSnapshot = snapshotPath.resolve().relative_to(runDir.resolve())
        except ValueError:
            raise RunRecordError(
                "Environment snapshot must be stored inside the run directory."
            )
        producedPaths = {
            Path(value["path"]).resolve()
            for command in commands
            if command.get("status") == "success"
            for value in command.get("produces", [])
            if isinstance(value, dict) and isinstance(value.get("path"), str)
        }
        if snapshotPath.resolve() not in producedPaths:
            raise RunRecordError(
                "Environment snapshot must be declared with --produces on its "
                "successful recorder command."
            )
        environmentSnapshot = dict(environmentSnapshot)
        environmentSnapshot["path"] = relativeSnapshot.as_posix()
        environmentSnapshot["sizeBytes"] = snapshotPath.stat().st_size
        environmentSnapshot["sha256"] = sha256File(snapshotPath)
    elif environmentSnapshot is not None:
        raise RunRecordError(
            "runtimeEnvironment manager 'none' requires environmentSnapshot null."
        )
    inputs = enrichArtifacts(summaryInput["inputs"])
    outputs = enrichArtifacts(summaryInput["outputs"])
    if summaryInput["status"] == "success":
        missingArtifacts = [
            str(record.get("path"))
            for record in inputs + outputs
            if isinstance(record, dict)
            and isinstance(record.get("path"), str)
            and record.get("existsAtFinalization") is not True
        ]
        if missingArtifacts:
            raise RunRecordError(
                "Successful run references missing input/output artifacts: "
                + ", ".join(missingArtifacts)
            )
    if state.get("commandExecutionExpected") is True:
        consumedPaths = recordedArtifactPaths(commands, "consumes", False)
        producedPaths = recordedArtifactPaths(commands, "produces", True)
        for kind, records, declaredPaths in (
            ("input", inputs, consumedPaths),
            ("output", outputs, producedPaths),
        ):
            for record in records:
                pathValue = record.get("path")
                if not isinstance(pathValue, str):
                    raise RunRecordError(
                        "Command-driven summary {} records require paths.".format(
                            kind
                        )
                    )
                if Path(pathValue).expanduser().resolve() not in declaredPaths:
                    raise RunRecordError(
                        "Summary {} was not declared on a recorder command: {}".format(
                            kind, pathValue
                        )
                    )

    manifest = {
        "recordSchemaVersion": RECORD_SCHEMA_VERSION,
        "runId": state["runId"],
        "status": summaryInput["status"],
        "startedAt": state["startedAt"],
        "endedAt": endedAt,
        "skill": mainSkill,
        "packaging": state.get("packaging"),
        "dependencies": state.get("dependencies", []),
        "commandExecutionExpected": state.get("commandExecutionExpected"),
        "runtimeEnvironment": state.get("runtimeEnvironment"),
        "runtimeHost": state.get("runtimeHost"),
        "environmentSnapshot": environmentSnapshot,
        "request": state["request"],
        "commands": commands,
        "actions": actions,
        "parameterEvents": parameterRecords,
        "parameters": summaryInput["parameters"],
        "inputs": inputs,
        "outputs": outputs,
        "skillsUsed": skillsUsed,
        "versions": versions,
        "whatWasDone": summaryInput["whatWasDone"],
        "findings": summaryInput["findings"],
        "warnings": summaryInput["warnings"],
        "assumptions": summaryInput["assumptions"],
        "manualSteps": summaryInput["manualSteps"],
        "limitations": summaryInput["limitations"],
        "workflowNotes": "agent_workflow.md",
        "replayScript": "commands.sh",
        "commandLog": "logs/commands.log",
        "commandEvents": "logs/commands.jsonl",
    }
    writeJson(runDir / "run_manifest.json", manifest)
    writeText(
        runDir / "run_manifest.md",
        renderManifestMarkdown(manifest),
    )

    summary = {
        "recordSchemaVersion": RECORD_SCHEMA_VERSION,
        "runId": state["runId"],
        "status": summaryInput["status"],
        "startedAt": state["startedAt"],
        "endedAt": endedAt,
        "skill": mainSkill,
        "request": state["request"],
        "runtimeHost": state.get("runtimeHost"),
        "environmentSnapshot": environmentSnapshot,
        "commandCount": len(commands),
        "actionCount": len(actions),
        "whatWasDone": summaryInput["whatWasDone"],
        "findings": summaryInput["findings"],
        "parameters": summaryInput["parameters"],
        "inputs": inputs,
        "outputs": outputs,
        "skillsUsed": skillsUsed,
        "versions": versions,
        "warnings": summaryInput["warnings"],
        "assumptions": summaryInput["assumptions"],
        "manualSteps": summaryInput["manualSteps"],
        "limitations": summaryInput["limitations"],
    }
    writeJson(runDir / "run_summary.json", summary)
    writeText(
        runDir / "run_summary.md",
        renderSummaryMarkdown(summary, commands, actions),
    )
    appendText(
        runDir / "agent_workflow.md",
        "\n## Finalization\n\n"
        "- Finalized: {}\n"
        "- Status: {}\n"
        "- Recorded commands: {}\n"
        "- Recorded non-shell actions: {}\n".format(
            endedAt, summary["status"], len(commands), len(actions)
        ),
    )
    state["status"] = "finalized"
    state["endedAt"] = endedAt
    writeJson(runDir / ".run-state.json", state)
    print(str(runDir / "run_summary.md"))
    return 0


def validateRun(args):
    """Validate mandatory runtime provenance artifacts."""
    runDir = Path(args.runDir).expanduser().absolute()
    requiredFiles = [
        "agent_request.txt",
        "agent_workflow.md",
        "commands.sh",
        "run_manifest.json",
        "run_manifest.md",
        "run_summary.json",
        "run_summary.md",
        "logs/commands.log",
        "logs/commands.jsonl",
        "logs/actions.jsonl",
        "logs/skills.jsonl",
        "logs/versions.jsonl",
        "logs/parameters.jsonl",
    ]
    issues = []
    for relative in requiredFiles:
        path = runDir / relative
        if not path.is_file():
            issues.append("Missing required file: {}".format(relative))
    if issues:
        raise RunRecordError("\n".join(issues))

    manifest = readJson(runDir / "run_manifest.json")
    summary = readJson(runDir / "run_summary.json")
    commands = readJsonl(commandLogPath(runDir))
    actions = readJsonl(actionLogPath(runDir))
    parameterRecords = readJsonl(parameterLogPath(runDir))
    recordedSkills = readJsonl(skillLogPath(runDir))
    recordedVersions = readJsonl(versionLogPath(runDir))
    if manifest.get("recordSchemaVersion") != RECORD_SCHEMA_VERSION:
        issues.append("run_manifest.json has unsupported schema version.")
    if summary.get("recordSchemaVersion") != RECORD_SCHEMA_VERSION:
        issues.append("run_summary.json has unsupported schema version.")
    if manifest.get("commands") != commands:
        issues.append("Manifest commands do not match logs/commands.jsonl.")
    if manifest.get("actions") != actions:
        issues.append("Manifest actions do not match logs/actions.jsonl.")
    if manifest.get("parameterEvents") != parameterRecords:
        issues.append(
            "Manifest parameter events do not match logs/parameters.jsonl."
        )
    if not commands and not actions:
        issues.append("No commands or actions were recorded.")
    if any(
        command.get("category")
        not in {"setup", "workflow", "validation", "environment", "provenance"}
        for command in commands
    ):
        issues.append("One or more command records have an invalid category.")
    priorCommands = {}
    for command in commands:
        retryOf = command.get("retryOf")
        if retryOf:
            prior = priorCommands.get(retryOf)
            if prior is None or prior.get("status") != "failed":
                issues.append(
                    "Command {} has an invalid retryOf reference.".format(
                        command.get("stepId")
                    )
                )
        priorCommands[command.get("stepId")] = command
    if (
        manifest.get("status") == "success"
        and manifest.get("commandExecutionExpected") is True
        and not any(
            command.get("category") == "workflow"
            and command.get("status") == "success"
            for command in commands
        )
    ):
        issues.append("Successful run has no successful workflow command.")
    if manifest.get("commandExecutionExpected") is True:
        consumedPaths = recordedArtifactPaths(commands, "consumes", False)
        producedPaths = recordedArtifactPaths(commands, "produces", True)
        for kind, records, declaredPaths in (
            ("input", manifest.get("inputs", []), consumedPaths),
            ("output", manifest.get("outputs", []), producedPaths),
        ):
            for record in records:
                pathValue = record.get("path") if isinstance(record, dict) else None
                if not isinstance(pathValue, str) or (
                    Path(pathValue).expanduser().resolve() not in declaredPaths
                ):
                    issues.append(
                        "Manifest {} is not linked to command provenance.".format(
                            kind
                        )
                    )
    if manifest.get("status") == "success":
        for record in manifest.get("inputs", []) + manifest.get("outputs", []):
            if (
                isinstance(record, dict)
                and isinstance(record.get("path"), str)
                and record.get("existsAtFinalization") is not True
            ):
                issues.append(
                    "Successful run references a missing input/output artifact."
                )
                continue
            if isinstance(record, dict) and isinstance(record.get("path"), str):
                artifactPath = Path(record["path"]).expanduser()
                if not artifactPath.exists():
                    issues.append(
                        "Recorded input/output artifact no longer exists: {}".format(
                            artifactPath
                        )
                    )
                elif artifactPath.is_file() and record.get("sha256") != sha256File(
                    artifactPath
                ):
                    issues.append(
                        "Recorded input/output artifact hash has changed: {}".format(
                            artifactPath
                        )
                    )
    if summary.get("commandCount") != len(commands):
        issues.append("run_summary.json commandCount is incorrect.")
    if summary.get("actionCount") != len(actions):
        issues.append("run_summary.json actionCount is incorrect.")
    if manifest.get("skillsUsed") != summary.get("skillsUsed"):
        issues.append("Skill identities differ between manifest and summary.")
    if manifest.get("versions") != summary.get("versions"):
        issues.append("Resolved versions differ between manifest and summary.")
    missingVersions = missingDependencyVersions(
        manifest.get("dependencies", []), manifest.get("versions", [])
    )
    if missingVersions:
        issues.append(
            "Missing resolved versions for required dependencies: "
            + ", ".join(missingVersions)
        )
    for field in (
        "status",
        "whatWasDone",
        "findings",
        "parameters",
        "inputs",
        "outputs",
        "warnings",
        "assumptions",
        "manualSteps",
        "limitations",
        "environmentSnapshot",
        "request",
        "runtimeHost",
        "skill",
    ):
        if manifest.get(field) != summary.get(field):
            issues.append(
                "{} differs between manifest and summary.".format(field)
            )
    manifestSkills = manifest.get("skillsUsed", [])
    for recordedSkill in recordedSkills:
        try:
            normalizedSkill = normalizeSkillIdentity(recordedSkill)
        except RunRecordError as exc:
            issues.append(str(exc))
            continue
        if not any(
            isinstance(value, dict)
            and all(
                value.get(field) == normalizedSkill.get(field)
                for field in ("name", "version", "path", "sha256", "commit", "role")
            )
            for value in manifestSkills
        ):
            issues.append(
                "A recorded composed skill is absent from the final manifest."
            )
    manifestVersions = manifest.get("versions", [])
    for recordedVersion in recordedVersions:
        if recordedVersion not in manifestVersions:
            issues.append("A recorded runtime version is absent from the manifest.")
            continue
        source = next(
            (
                command
                for command in commands
                if command.get("stepId") == recordedVersion.get("sourceStep")
            ),
            None,
        )
        if source is None or source.get("status") != "success":
            issues.append("A version record has no successful source command.")
        elif recordedVersion.get("versionCommand") not in source.get(
            "displayCommand", ""
        ):
            issues.append("A version record command differs from its source command.")
        evidencePath = runDir / str(recordedVersion.get("evidenceFile", ""))
        if not evidencePath.is_file():
            issues.append("A version evidence file is missing.")
        elif recordedVersion.get("evidenceSha256") != sha256File(evidencePath):
            issues.append("A version evidence file hash has changed.")
        elif str(recordedVersion.get("version")) not in evidencePath.read_text(
            encoding="utf-8", errors="replace"
        ):
            issues.append("A claimed version is absent from its evidence file.")
        if source is not None:
            producedPaths = {
                Path(value["path"]).resolve()
                for value in source.get("produces", [])
                if isinstance(value, dict) and isinstance(value.get("path"), str)
            }
            if evidencePath.resolve() not in producedPaths:
                issues.append("Version evidence was not a declared command output.")
        resolvedPathValue = recordedVersion.get("path")
        if resolvedPathValue and not Path(resolvedPathValue).exists():
            issues.append("A resolved version path no longer exists.")
    if not summary.get("whatWasDone"):
        issues.append("run_summary.json has no whatWasDone entries.")
    if not isinstance(summary.get("parameters"), dict):
        issues.append("run_summary.json has no parameter object.")
    if summary.get("parameters") != effectiveRecordedParameters(
        commands, parameterRecords
    ):
        issues.append("Effective parameters differ from recorded parameter events.")
    if not summary.get("skillsUsed"):
        issues.append("run_summary.json has no skill identity.")
    runtimeEnvironment = manifest.get("runtimeEnvironment") or {}
    if runtimeEnvironment.get("manager") != "none":
        snapshot = manifest.get("environmentSnapshot")
        if not isinstance(snapshot, dict):
            issues.append("run_manifest.json has no resolved environment snapshot.")
        else:
            snapshotPath = Path(str(snapshot.get("path", ""))).expanduser()
            if not snapshotPath.is_absolute():
                snapshotPath = runDir / snapshotPath
            if not snapshotPath.is_file():
                issues.append("Resolved environment snapshot file is missing.")
            elif snapshot.get("sha256") != sha256File(snapshotPath):
                issues.append("Resolved environment snapshot hash is incorrect.")
            else:
                try:
                    validateEnvironmentSnapshot(
                        snapshotPath, snapshot.get("format")
                    )
                    verifySnapshotVersionConsistency(
                        manifest.get("dependencies", []),
                        manifest.get("versions", []),
                        runtimeEnvironment.get("manager"),
                        snapshotVersionMap(
                            snapshotPath, snapshot.get("format")
                        ),
                    )
                except RunRecordError as exc:
                    issues.append(str(exc))
            producedPaths = {
                Path(value["path"]).resolve()
                for command in commands
                if command.get("status") == "success"
                for value in command.get("produces", [])
                if isinstance(value, dict) and isinstance(value.get("path"), str)
            }
            if snapshotPath.resolve() not in producedPaths:
                issues.append(
                    "Resolved environment snapshot has no successful producing command."
                )
            if summary.get("environmentSnapshot") != snapshot:
                issues.append(
                    "run_summary.json environment snapshot differs from the manifest."
                )
    requestPath = runDir / "agent_request.txt"
    if requestPath.exists() and requestPath.stat().st_size == 0:
        issues.append("agent_request.txt is empty.")
    request = manifest.get("request", {})
    if requestPath.exists() and request.get("sha256") != sha256File(requestPath):
        issues.append("agent_request.txt hash does not match the manifest.")
    if request.get("captureMode") == "verbatim":
        if request.get("originalSha256") != request.get("sha256"):
            issues.append("Verbatim request original/saved hashes differ.")
        if request.get("redactions"):
            issues.append("Verbatim request must not declare redactions.")
    elif request.get("captureMode") == "sanitized":
        if (
            not request.get("redactions")
            or request.get("originalSha256") == request.get("sha256")
        ):
            issues.append(
                "Sanitized request lacks redaction/original-hash provenance."
            )
    else:
        issues.append("Request capture mode is invalid.")
    if not (runDir / "run_summary.md").read_text(encoding="utf-8").strip():
        issues.append("run_summary.md is empty.")
    if not (runDir / "run_manifest.md").read_text(encoding="utf-8").strip():
        issues.append("run_manifest.md is empty.")
    if (runDir / "run_summary.md").read_text(
        encoding="utf-8"
    ) != renderSummaryMarkdown(summary, commands, actions):
        issues.append("run_summary.md does not match run_summary.json.")
    if (runDir / "run_manifest.md").read_text(
        encoding="utf-8"
    ) != renderManifestMarkdown(manifest):
        issues.append("run_manifest.md does not match run_manifest.json.")
    replay = (runDir / "commands.sh").read_text(encoding="utf-8")
    expectedReplay = replayHeader(manifest.get("runId")) + "".join(
        replayEntry(command) for command in commands
    )
    if replay != expectedReplay:
        issues.append("commands.sh does not exactly match recorded commands.")
    allEvents = commands + actions + parameterRecords
    sequences = sorted(
        record.get("sequence") for record in allEvents if "sequence" in record
    )
    if sequences != list(range(1, len(allEvents) + 1)):
        issues.append("Command/action/parameter sequence is not contiguous.")
    expectedPlainLog = "".join(
        (
            plainCommandEntry(record)
            if record.get("type") == "command"
            else (
                plainActionEntry(record)
                if record.get("type") == "action"
                else plainParameterEntry(record)
            )
        )
        for record in sorted(allEvents, key=lambda value: value.get("sequence", 0))
    )
    if (runDir / "logs" / "commands.log").read_text(
        encoding="utf-8"
    ) != expectedPlainLog:
        issues.append("logs/commands.log does not match structured event logs.")
    if issues:
        raise RunRecordError("\n".join(issues))
    print("Valid run record: {}".format(runDir))
    return 0


def buildParser():
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Record exact commands, non-shell actions, parameters, skills, "
            "versions, outputs, and summaries for a generated Agent Skill run."
        )
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    initParser = subparsers.add_parser("init", help="Initialize a run directory.")
    initParser.add_argument("--outputRoot", required=True)
    initParser.add_argument("--skillDir", required=True)
    initParser.add_argument("--requestFile", required=True)
    initParser.add_argument(
        "--requestCapture",
        choices=["verbatim", "sanitized"],
        required=True,
    )
    initParser.add_argument("--originalRequestSha256")
    initParser.add_argument("--redaction", action="append", default=[])
    initParser.add_argument("--workflowFile")
    initParser.add_argument("--runId")
    initParser.set_defaults(handler=initRun)

    execParser = subparsers.add_parser(
        "exec", help="Execute and record one shell or argv command."
    )
    execParser.add_argument("--runDir", required=True)
    execParser.add_argument("--stepId", required=True)
    execParser.add_argument(
        "--category",
        required=True,
        choices=["setup", "workflow", "validation", "environment", "provenance"],
    )
    execParser.add_argument("--description", required=True)
    execParser.add_argument("--cwd")
    execParser.add_argument("--retryOf")
    execParser.add_argument("--displayCommand")
    execParser.add_argument("--redacted", action="store_true")
    execParser.add_argument("--parameter", action="append", default=[])
    execParser.add_argument("--consumes", action="append", default=[])
    execParser.add_argument("--produces", action="append", default=[])
    execParser.add_argument("command", nargs=argparse.REMAINDER)
    execParser.set_defaults(handler=executeCommand)

    actionParser = subparsers.add_parser(
        "record-action", help="Record a non-shell or manual action."
    )
    actionParser.add_argument("--runDir", required=True)
    actionParser.add_argument("--stepId", required=True)
    actionParser.add_argument(
        "--kind",
        required=True,
        choices=["native-tool", "mcp", "notebook", "gui", "manual", "decision"],
    )
    actionParser.add_argument("--description", required=True)
    actionParser.add_argument("--details")
    actionParser.add_argument(
        "--status",
        choices=["success", "failed", "skipped", "pending"],
        required=True,
    )
    actionParser.set_defaults(handler=recordAction)

    parameterParser = subparsers.add_parser(
        "record-parameter",
        help="Record an effective parameter not attached to one command.",
    )
    parameterParser.add_argument("--runDir", required=True)
    parameterParser.add_argument("--name", required=True)
    parameterParser.add_argument("--valueJson", required=True)
    parameterParser.add_argument("--source", required=True)
    parameterParser.add_argument("--description", required=True)
    parameterParser.set_defaults(handler=recordParameter)

    skillParser = subparsers.add_parser(
        "record-skill", help="Record a composed skill and its identity."
    )
    skillParser.add_argument("--runDir", required=True)
    skillParser.add_argument("--name", required=True)
    skillParser.add_argument("--version", required=True)
    skillParser.add_argument("--path", required=True)
    skillParser.add_argument("--sha256")
    skillParser.add_argument("--commit")
    skillParser.add_argument("--role", required=True)
    skillParser.set_defaults(handler=recordSkill)

    versionParser = subparsers.add_parser(
        "record-version", help="Record one resolved runtime version."
    )
    versionParser.add_argument("--runDir", required=True)
    versionParser.add_argument("--name", required=True)
    versionParser.add_argument("--version", required=True)
    versionParser.add_argument(
        "--kind",
        required=True,
        choices=[
            "interpreter",
            "package",
            "cli",
            "pipeline",
            "container",
            "reference",
            "codebase",
        ],
    )
    versionParser.add_argument("--path")
    versionParser.add_argument("--versionCommand", required=True)
    versionParser.add_argument("--sourceStep", required=True)
    versionParser.add_argument("--evidenceFile", required=True)
    versionParser.set_defaults(handler=recordVersion)

    finalizeParser = subparsers.add_parser(
        "finalize", help="Write final machine and human run summaries."
    )
    finalizeParser.add_argument("--runDir", required=True)
    finalizeParser.add_argument("--summaryFile", required=True)
    finalizeParser.set_defaults(handler=finalizeRun)

    validateParser = subparsers.add_parser(
        "validate", help="Validate a finalized run record."
    )
    validateParser.add_argument("--runDir", required=True)
    validateParser.set_defaults(handler=validateRun)
    return parser


def main():
    """Run the recorder CLI."""
    parser = buildParser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except RunRecordError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
