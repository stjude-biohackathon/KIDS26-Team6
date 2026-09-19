---
name: recorder
description: Record a bioinformatics workflow with AutoCAB so it can become a reviewed skill draft. Use when the user asks to start, finish, pause, resume, annotate, inspect, or adjust recording.
---

# AutoCAB workflow recorder

Drive AutoCAB's recorder on the user's behalf. It captures a session into a
folder containing an append-only JSONL timeline plus sidecar artifacts, which
AutoCAB can turn into a reviewed `SKILL.md` draft.

Use `autocab record` for the session lifecycle and `autocab status` for state.
The legacy `wfrec` alias still provides advanced source, hook, remote, and
diagnostic controls. Never edit files under `~/.autocab/` or `~/.wfrec/`
directly.

## Verb table

| The user says | Run |
|---|---|
| "start recording my variant calling work" | `autocab record start --title "variant calling" --watch .` |
| "start screen recording" | `wfrec source screen on` |
| "stop screen recording" | `wfrec source screen off` |
| "stop logging my bash history" | `wfrec source shell off` |
| "start capturing my commands again" | `wfrec source shell on` |
| "also record command output" | Explain that current backends do not capture terminal stdout or stderr |
| "pause, I'm waiting on the alignment job" | `autocab record pause --reason "waiting on alignment" --expect 6h` |
| "resume" / "I'm back" | `autocab record resume` |
| "note that I reran this because the BAM was truncated" | `autocab record note "..." --label why` |
| "mark this point" | `wfrec mark "<label>"` |
| "watch this directory too" | `wfrec watch <dir>` |
| "what are you recording right now?" | `autocab status` |
| "why isn't screen capture working?" | `wfrec doctor` |
| "wrap up and hand this to AutoCAB" | `autocab record finish --seal` |
| "combine my session with Bob's" | `wfrec merge <id> <id> --output merged.json` |

The five capture sources are `screen`, `context`, `shell`, `agents`, `files`.
Each is independently toggleable at any time, including from terminals the user
already has open.

## Rules

1. **Never start recording unprompted.** This tool captures screenshots, shell
   commands, file changes and agent transcripts. Starting it is the user's
   decision every time, and so is each source they want on.

2. **Check first, act second.** Run `autocab status` before lifecycle changes.
   Most errors are just "no active session"; `autocab record start` fixes those, and
   guessing a session id does not.

3. **Always pass a reason when pausing.** `--reason` is what lets a downstream
   skill distinguish *"the analyst waited six hours on a BWA job"* from
   *"nothing happened"*. A pause without a reason discards that signal.

4. **Prefer `--title` and `--watch` at start.** The title becomes the workflow
   family AutoCAB clusters on, and `--watch` is what makes file changes and
   diffs get captured at all — without a declared root, the `files` source
   reports `no-watch-roots` and records nothing.

5. **Relay redactions.** `autocab record note` reports which patterns it scrubbed. Tell
   the user, so they know an identifier was caught rather than assuming
   nothing was there.

6. **Do not fight a degraded source.** If `wfrec status` shows a source as
   unavailable with a reason (`wayland-no-unattended-capture`,
   `no-agent-tools-found`, `no-watch-roots`), report the reason and the
   suggested fallback. Do not retry in a loop or try to work around the
   platform.

7. **`--json` for parsing, plain output for the user.** Use
   `autocab status --json` when you need to branch on a field; show the plain
   output when reporting back.

## Setup, once per machine

```bash
wfrec doctor             # confirms which backend each source resolved to, and why
wfrec hooks install      # only when doctor selects the hook-spool fallback
```

DevSQL with Atuin is the primary local shell backend. The fallback hook only
affects new terminals when first installed. To load it in an existing terminal,
run `wfrec hooks eval` and have the user paste the line it prints.

## Handing off to AutoCAB

```bash
autocab record finish --seal
autocab forge --session <session-id>
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
  synthetic and public datasets. De-identification is a **measured** control now
  rather than a best-effort one -- `docs/deid-evaluation.md` carries per-label
  recall from `autocab deid eval` -- but a measured recall number is not a HIPAA
  Safe Harbor determination, name recall from the pattern tier is around 0.45,
  and HIPAA identifier 17 (full-face photographs) is uncovered entirely. Read
  "What this does not prove" in that document before telling a user anything
  about the guarantees.
- **Always tell the user to seal a session before forging it.** AutoCAB refuses
  an unsealed session, so a stopped session is not yet usable. Prefer
  `autocab record finish --seal`. Sealing rewrites the session in place and is
  irreversible -- no reverse map is written and the key is discarded -- so say so
  before running it, and use `wfrec seal --dry-run <id>` if the user wants to see
  what it would find first.
- Do not install shell hooks on a shared or remote host without saying so
  first. `wfrec ssh <host>` bootstraps a hook into the remote `~/.bashrc`, and
  that is a change to infrastructure the user may share with colleagues.
