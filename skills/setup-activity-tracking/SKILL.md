---
name: setup-activity-tracking
description: Set up, verify, and safely remove local DevSQL and Atuin activity tracking for AutoCAB on Linux or macOS. Use when preparing a workstation for local Claude Code or Codex history detection, checking ActivityWatch or VS Code watcher availability, or uninstalling only artifacts previously installed by AutoCAB.
license: MIT
metadata:
  author: AutoCAB Team
  version: "0.4.0"
  status: experimental
  last_reviewed: "2026-09-16"
allowed-tools: shell python
---

# Set Up Activity Tracking

## Purpose

Prepare a Linux or macOS workstation for local AutoCAB activity evidence using
DevSQL and Atuin. Keep dependency setup separate from activity collection,
workflow inference, and skill generation.

## When to Use

Use this skill when the user asks to:

- install or verify the local DevSQL and Atuin dependencies,
- detect native Claude Code or Codex activity histories,
- check whether ActivityWatch and its VS Code watcher are available, or
- remove only the dependency artifacts previously installed by AutoCAB.

## When Not to Use

Do not use this skill to:

- install dependencies outside Linux or macOS,
- create an Atuin account, synchronize history, or import existing history,
- install ActivityWatch or its VS Code watcher, or
- collect activity, infer workflows, or promote generated skill proposals.

## Compatibility

Installation supports Linux and macOS and requires Bash, Python 3.10 or newer,
`curl`, `tar`, and `xz`. Network access is required only to download DevSQL and
Atuin. Verification and uninstall are local.

## Safety Boundaries

- Explain which tools and shell files will change. Obtain explicit approval
  immediately before installation or removal.
- Keep DevSQL and Atuin as external executables. Do not modify their source.
- Treat commands, local paths, and activity records as sensitive. Report status
  without printing queried commands, history contents, or local paths.
- Do not install Atuin hooks for Claude Code, Codex, or other agents
  automatically. DevSQL already reads native Claude Code and Codex histories,
  so hooks can create duplicate command records.
- Detect ActivityWatch through its local service only. Do not install it.

## Workflow

### 1. Run the Read-Only Verifier

From the repository root, run:

```bash
python3 skills/setup-activity-tracking/scripts/verify_install.py
```

Summarize missing requirements without exposing commands, history contents, or
machine-specific paths.

### 2. Install Missing Dependencies

Explain that the installer downloads DevSQL and Atuin, writes binaries below
the user's home directory, and may add idempotent initialization lines to the
active Bash, Zsh, or Fish profile. After the user approves those changes, run:

```bash
bash skills/setup-activity-tracking/scripts/install_dependencies.sh
```

The installer writes a private ownership receipt below the user's XDG state
directory. The receipt distinguishes artifacts created by AutoCAB from tools
and shell configuration that already existed.

### 3. Verify the Installation

Ask the user to open a new shell, then rerun the verifier. Report required
failures separately from optional ActivityWatch, VS Code watcher, and Atuin
agent-hook status.

## Scripts

- `scripts/verify_install.py`: performs read-only dependency, history,
  ActivityWatch, watcher, and hook checks without displaying activity data.
- `scripts/install_dependencies.sh`: installs missing DevSQL and Atuin binaries,
  configures a supported shell, and records AutoCAB ownership.
- `scripts/logging.sh`: provides the shared console log format used by the Bash
  scripts.
- `scripts/uninstall_dependencies.sh`: plans, applies, and verifies removal of
  only the artifacts recorded as AutoCAB-owned.

## Uninstall

Keep dependency removal separate from deleting this skill folder or purging
activity data.

1. Display the removal plan. This default action does not change files:

   ```bash
   bash skills/setup-activity-tracking/scripts/uninstall_dependencies.sh
   ```

2. Review the complete plan with the user. Stop if any artifact requires manual
   review.
3. After the user explicitly approves the displayed plan, apply it using the
   required confirmation phrase:

   ```bash
   bash skills/setup-activity-tracking/scripts/uninstall_dependencies.sh \
       apply \
       "REMOVE AUTOCAB ACTIVITY TRACKING"
   ```

4. Verify that AutoCAB-owned installation artifacts are absent by rerunning the
   read-only default action:

   ```bash
   bash skills/setup-activity-tracking/scripts/uninstall_dependencies.sh
   ```

The uninstaller preserves Atuin history and configuration, DevSQL worklogs and
caches, ActivityWatch data, Claude Code and Codex histories, agent hooks, and
preexisting tools. Do not add or perform a data purge without separate explicit
approval.

## Optional Agent Hooks

Only discuss these commands when the user explicitly wants agent-attributed
Atuin records and accepts possible duplication with DevSQL's native histories:

```bash
atuin hook install claude-code
atuin hook install codex
```

Never run either command without separate explicit approval.

## Quality Checks

Before finishing, confirm that:

- the verifier completed without exposing activity contents,
- required binaries are executable when installation was requested,
- shell initialization was added at most once,
- the ownership receipt exists after an AutoCAB-managed installation, and
- uninstall plans contain only allowlisted files and exact recorded shell lines.

## Failure and Escalation

- Outside Linux or macOS, report that installation is unsupported. The verifier
  may still report local status.
- If a required command is missing, stop and report the smallest installation
  command provided by the installer.
- If an uninstall artifact changed after installation or falls outside the
  allowlist, stop automatic removal and report that manual review is required.
- If ActivityWatch, its VS Code watcher, or optional hooks are absent, report
  them as optional. Do not install or enable them without a new request.
