---
name: recorder
description: Record the user's bioinformatics workflow session with wfrec so AutoCAB can draft a skill from it. Use when the user asks to start/stop/pause recording, toggle a capture source ("start screen recording", "stop logging my bash history"), note context worth remembering, pause while waiting on a job, check what is being recorded, or export a session for AutoCAB.
---

# Workflow recorder (`wfrec`)

Drive the `wfrec` recorder on the user's behalf. It captures a session into a
folder containing an append-only JSONL timeline plus sidecar artifacts, which
AutoCAB then turns into a draft `SKILL.md`.

**Always act through the `wfrec` CLI.** It is the stable contract shared by the
CLI, the GUI window and this skill, so a command you run and a button the user
clicks do the same thing. Never edit files under `~/.wfrec/` directly.

## Verb table

| The user says | Run |
|---|---|
| "start recording my variant calling work" | `wfrec start --title "variant calling" --watch .` |
| "start screen recording" | `wfrec source screen on` |
| "stop screen recording" | `wfrec source screen off` |
| "stop logging my bash history" | `wfrec source shell off` |
| "start capturing my commands again" | `wfrec source shell on` |
| "also record command output" | `wfrec shell-output on` |
| "pause, I'm waiting on the alignment job" | `wfrec pause --reason "waiting on alignment" --expect 6h` |
| "resume" / "I'm back" | `wfrec resume` |
| "note that I reran this because the BAM was truncated" | `wfrec note "..." --label why` |
| "mark this point" | `wfrec mark "<label>"` |
| "watch this directory too" | `wfrec watch <dir>` |
| "what are you recording right now?" | `wfrec status` |
| "why isn't screen capture working?" | `wfrec doctor` |
| "wrap up and hand this to AutoCAB" | `wfrec stop && wfrec export --format autocab` |
| "combine my session with Bob's" | `wfrec merge <id> <id> --output merged.json` |

The five capture sources are `screen`, `context`, `shell`, `agents`, `files`.
Each is independently toggleable at any time, including from terminals the user
already has open.

## Rules

1. **Never start recording unprompted.** This tool captures screenshots, shell
   commands, file changes and agent transcripts. Starting it is the user's
   decision every time, and so is each source they want on.

2. **Check first, act second.** Run `wfrec status` before lifecycle changes.
   Most errors are just "no active session"; `wfrec start` fixes those, and
   guessing a session id does not.

3. **Always pass a reason when pausing.** `--reason` is what lets a downstream
   skill distinguish *"the analyst waited six hours on a BWA job"* from
   *"nothing happened"*. A pause without a reason discards that signal.

4. **Prefer `--title` and `--watch` at start.** The title becomes the workflow
   family AutoCAB clusters on, and `--watch` is what makes file changes and
   diffs get captured at all — without a declared root, the `files` source
   reports `no-watch-roots` and records nothing.

5. **Relay redactions.** `wfrec note` reports which patterns it scrubbed. Tell
   the user, so they know an identifier was caught rather than assuming
   nothing was there.

6. **Do not fight a degraded source.** If `wfrec status` shows a source as
   unavailable with a reason (`wayland-no-unattended-capture`,
   `no-agent-tools-found`, `no-watch-roots`), report the reason and the
   suggested fallback. Do not retry in a loop or try to work around the
   platform.

7. **`--json` for parsing, plain output for the user.** Use
   `wfrec status --json` when you need to branch on a field; show the plain
   output when reporting back.

## Setup, once per machine

```bash
wfrec hooks install      # adds a guarded two-line source block to the user's rc file
wfrec doctor             # confirms which backend each source resolved to, and why
```

Shell capture requires the hook. Installing it only affects *new* terminals; to
start capturing in a terminal that is already open, run `wfrec hooks eval` and
have the user paste the line it prints.

## Handing off to AutoCAB

```bash
wfrec stop
wfrec export --format autocab --format trace
autocab demo --input-mode session --session-dir ~/.wfrec/sessions/<id>
```

`export` writes three lossy projections into `<session>/exports/`: the strict
terminal-log format, screenpipe-style capture JSON, and a `WorkflowTrace`
array. The session's `events.jsonl` remains the source of truth — it holds exit
codes, durations, diffs and OCR that `WorkflowStep`'s four fields cannot carry.

For a multi-analyst run, point `--session-dir` at a directory *containing*
several session folders, or use `wfrec merge`.

## What this skill must not do

- Do not enable clipboard monitoring; there is no such feature, by design. The
  `context` source only records what the user deliberately pastes or types.
- Do not point the recorder at real patient data. The prototype is for
  synthetic and public datasets; redaction is best-effort, not a PHI control.
- Do not install shell hooks on a shared or remote host without saying so
  first. `wfrec ssh <host>` bootstraps a hook into the remote `~/.bashrc`, and
  that is a change to infrastructure the user may share with colleagues.
