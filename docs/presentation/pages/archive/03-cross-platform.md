---
layout: section
---

# Cross-platform

macOS · Windows · Linux · HPC

---
layout: default
---

<div class="kicker">Cross-platform</div>

# One recorder, four platforms, no pretending

<div class="mt-3">
<PlatformMatrix />
</div>

<div class="mt-5 text-sm grid grid-cols-2 gap-6">
<div>

**Two honest platform limits, documented before the demo, not during it:**
`cmd.exe` cannot be hooked, so Windows shell capture is PowerShell-only — and
one install covers both PowerShell 7 and Windows PowerShell 5.1, because
teammates run different generations on the same machine.

</div>
<div>

Wayland cannot do unattended screen capture: `mss` has no Wayland backend, the
bundled ffmpeg has neither `kmsgrab` nor pipewire, and the desktop portal
prompts on every request. Everything else works normally — so a screen-capture
demo gets planned on Xorg.

</div>
</div>

<!--
With three operating systems and two display servers, "works on my machine" is
the most expensive bug class in this project. The answer was not to hide the
matrix, it was to publish it and make every degradation self-reporting.
-->

---
layout: two-cols
---

<div class="kicker">Cross-platform</div>

# Degrade with a reason, never look broken

```bash
$ wfrec doctor
```

<div class="text-sm mt-3 opacity-85">

Run before anything else, on every machine. It reports **which backend each
source resolved to, and why anything degraded** — the difference between
diagnosing a problem and describing symptoms over Slack.

</div>

<div class="mt-5 text-sm">

Failure modes that would otherwise be silent, and are not:

- `no-watch-roots` — the `files` source has nothing declared
- `hook-spool` — install the fallback shell hook
- `no-agent-tools-found` — no transcripts; use `attach-transcript`
- Wayland screen capture — reported as a platform limit, with the reason

</div>

::right::

<div class="mt-14 text-sm">

**The macOS case that eats an afternoon if nobody wrote it down.**

Screen Recording permission is cached **per process**. Granting it while the
daemon is running does nothing:

```bash
wfrec source screen on    # triggers the prompt
# grant in System Settings → Privacy & Security
wfrec daemon --stop
wfrec daemon              # the grant takes effect here
```

Without the grant, `kCGWindowName` returns empty **with no prompt at all**, so
titles silently come back blank. `wfrec` falls back to the application name and
**records the reason** rather than appearing broken.

</div>

<!--
Every collector is failure-isolated and every degradation is named in the
timeline. That is what makes a hybrid team of five people on five machines able
to run the same tool without a support channel.
-->

---
layout: default
---

<div class="kicker">Cross-platform · HPC</div>

# The cluster is a first-class target, not an afterthought

<div class="grid grid-cols-2 gap-8 mt-3">
<div>

**Capture at submit time, because that is the only honest moment**

```bash
wfrec ssh hpc-login              # recorded remote shell
wfrec pull hpc-login --job 4213  # commands + job metadata
```

<div class="text-sm opacity-85 mt-2">

`sbatch` is wrapped on the remote so a job is captured **when it is submitted** —
the only moment `scontrol` reports the real `StdOut`, `StdErr` and `WorkDir`
paths instead of guessing `slurm-%j.out`.

Job output comes back as a **bounded head-and-tail slice**, never whole. A long
alignment's log can be gigabytes.

</div>

<div class="text-sm mt-4">

It wraps *your* `ssh` and `~/.ssh/config`, so ProxyJump, agent forwarding and
Duo/2FA behave exactly as you already expect. It only ever touches the host you
explicitly name, and the block it adds is plain text and marker-delimited.

</div>

</div>
<div>

**Run the daemon on the node, drive it from your laptop**

```bash
export WFREC_ALLOW_REMOTE=1
export WFREC_BIND_HOST=0.0.0.0
nohup wfrec daemon >> ~/.wfrec/daemon.log 2>&1 &
```

<div class="text-sm opacity-85 mt-2">

The startup summary prints **Local / Private / Public** URLs. Do not use
`--gui` on a headless login node — `BROWSER` is often `lynx`.

</div>

<div class="mt-4 p-3 rounded text-sm" style="border:1px solid rgba(208,59,59,.5)">
<strong style="color:#d03b3b">■ Security</strong> — the UI page embeds the API
token. A non-loopback bind means anyone who can reach the port can control the
recorder. Prefer <code>ssh -L 8787:127.0.0.1:8787</code>; use
<code>--bind-all</code> only on trusted networks.
</div>

<div class="text-sm mt-4">

**`inotify` delivers nothing on NFS and Lustre.** Those roots are polled with
`git status` instead, and `wfrec doctor` reports that it happened — so scratch
directories do not silently lose every file event.

</div>

</div>
</div>

<!--
This is the slide for the HPC staff in the room. The two things they will care
about: nothing is written to a shared rc file they did not name, and job logs
are sliced rather than copied wholesale onto the filesystem they maintain.
-->

---
layout: default
---

<div class="kicker">Cross-platform · shells</div>

# Five shells, two lines in your rc file

<div class="grid grid-cols-3 gap-6 mt-4">
<div>

**`bash` · `zsh` · `fish`**

Local capture on macOS and Linux. DevSQL with Atuin is primary; the hook is the
fallback when `doctor` says `hook-spool`.

</div>
<div>

**PowerShell 7 · Windows PowerShell 5.1**

Both current-user all-hosts profiles are updated by one install, so a mixed
Windows team gets the same behavior.

</div>
<div>

**POSIX `sh`**

Bootstrapped by `wfrec ssh` on the HPC login node, where you do not get to
choose the shell.

</div>
</div>

<div class="mt-8 grid grid-cols-2 gap-8">
<div>

```bash
wfrec hooks install    # detects the shell
wfrec hooks status
eval "$(wfrec hooks eval)"   # this terminal, now
```

```powershell
Invoke-Expression (wfrec hooks eval --shell powershell)
```

</div>
<div class="text-sm">

**One guarded, marker-delimited `source` line**, after a timestamped backup of
your rc file. The hook body lives in `~/.wfrec/hooks/`, so your rc file gains
**two reviewable lines** rather than a few hundred of someone else's code.

Install once and leave it alone: every later start, stop, pause and source
toggle reaches already-open shells at their next prompt.

</div>
</div>

<div class="mt-6 claim text-sm">
Writing to <code>~/.bashrc</code> on shared infrastructure is a change your
colleagues live with too. That is why it is two lines, reversible, backed up,
and only ever on the host you name.
</div>

<!--
An infostealer and a workflow recorder do exactly the same things to a machine.
The difference has to be visible in the artifacts: local-only, no egress by
default, reversible installs, marker-delimited edits, and a doctor command that
tells you what is on.
-->
