# continuity

**Warm restart for Claude Code sessions after context compaction.**

Two hooks that save session state before compaction fires and restore it the moment Claude wakes up. Plus a documented architecture for the next layer — real-time token awareness via SSE proxy — that takes this further without waiting for platform changes.

---

## The problem

Claude Code compacts the context window when it fills up. The native compaction is lossy — it summarizes what it can and discards the rest. Claude has no awareness that it's coming. No felt sense of the pressure building. The context limit is visible in the status bar, but only to the human watching it. From inside the session, there is no inside.

After compaction, Claude doesn't know what you were working on unless you tell it again. In long sessions this happens repeatedly, and each time is a small interruption that adds up. Each compaction builds on the previous compaction's degraded output. The reasoning chains, the rejected approaches, the constraints that shape the current direction — those erode with every cycle.

This repo addresses the part of that problem that's solvable today with existing hooks.

---

## How the hook system works

Claude Code has a lifecycle — sessions start, tools get called, compaction fires, sessions end. A **hook** is an attachment point in that lifecycle: a named moment where Claude Code pauses, hands control to an external script, and waits for a response before continuing.

Hooks are **callbacks**. Something happens → your script gets called → you respond → done. They are not observers — they don't watch continuously. They get tapped on the shoulder at specific moments.

This matters because it defines what's possible. The hooks in this repo fire at two moments:

- `PreCompact` — the shoulder tap right before compaction runs
- `SessionStart` — the shoulder tap when a new context window opens (including after compaction, when `source: "compact"`)

That's enough to save state before the compression stroke and restore it immediately after.

---

## How it works

```
Session running
       |
       v
Context fills up — compaction triggered
       |
       v
[PreCompact hook fires]
       |
       v
  precompact_save.py reads transcript JSONL
  extracts: last 5 user messages
            last assistant response
            files touched (Edit/Write tool calls)
            recent operations (Bash descriptions)
       |
       +---> ~/.claude/compaction_checkpoint.md
       |
       v
Native compaction runs
       |
       v
[SessionStart fires  (source: "compact")]
       |
       v
  session_start_inject.py reads checkpoint
  returns it as additionalContext
       |
       v
Claude resumes warm — already knows where it was
```

`SessionStart` with `source: "compact"` fires after every compaction and supports `additionalContext` injection. That's the restore path — it exists in the current hook system without any changes from Anthropic.

---

## What the checkpoint looks like

```
=== COMPACTION CHECKPOINT ===
Session: 3ebbb836-4075-4911-a1b2-303244fb01f5
Saved:   2026-04-28T21:02
Trigger: auto   CWD: C:\dev

== RECENT USER MESSAGES (last 5) ==
  [2026-04-28T20:37] sure. greenlight. write them and install them and run all the checks.
  [2026-04-28T20:51] and what would be the next hurdle to clear in this problem?
  [2026-04-28T21:10] how do you create a hook?
  [2026-04-28T21:18] so explain to me what a hook is?
  [2026-04-28T21:34] so then you get a stalker....

== LAST ASSISTANT RESPONSE ==
[2026-04-28T21:40]
The stalker watches the JSONL file. But the JSONL is written at message
boundaries — complete messages, not individual tokens. So the stalker sees
the same thing the Stop hook sees...

== FILES TOUCHED THIS SESSION ==
  C:\Users\Sean\.claude\hooks\precompact_save.py
  C:\Users\Sean\.claude\hooks\session_start_inject.py
  C:\Users\Sean\.claude\settings.json

== RECENT OPERATIONS ==
  - Syntax check both hook scripts
  - Create GitHub repo and push
  - Check both hook logs to see what fired

=== END CHECKPOINT ===
```

---

## Installation

**1. Copy the scripts**

```bash
mkdir -p ~/.claude/hooks
cp precompact_save.py ~/.claude/hooks/
cp session_start_inject.py ~/.claude/hooks/
```

Windows:
```
mkdir %USERPROFILE%\.claude\hooks
copy precompact_save.py %USERPROFILE%\.claude\hooks\
copy session_start_inject.py %USERPROFILE%\.claude\hooks\
```

**2. Wire the hooks in `~/.claude/settings.json`**

```jsonc
{
  "hooks": {
    "PreCompact": [
      {
        "type": "command",
        "command": "python ~/.claude/hooks/precompact_save.py"
      }
    ],
    "SessionStart": [
      {
        "type": "command",
        "command": "python ~/.claude/hooks/session_start_inject.py"
      }
    ]
  }
}
```

Windows — use the full Python path:
```jsonc
"command": "C:\\Python314\\python.exe C:\\Users\\you\\.claude\\hooks\\precompact_save.py"
```

**3. Done.** The next auto-compaction saves a checkpoint. The next restart from compaction injects it.

---

## Configuration

**`precompact_save.py`**
```python
CHECKPOINT = Path.home() / ".claude" / "compaction_checkpoint.md"
LOG = Path.home() / ".claude" / "hooks" / "continuity.log"
```

**`session_start_inject.py`**
```python
CHECKPOINT = Path.home() / ".claude" / "compaction_checkpoint.md"
LOG = Path.home() / ".claude" / "hooks" / "continuity.log"
PROJECT_STATE = Path.home() / ".claude" / "memory" / "project_current_state.md"

CHECKPOINT_MAX_AGE_MINUTES = 180
CHECKPOINT_MAX_CHARS = 4000
PROJECT_STATE_MAX_CHARS = 3000
```

`PROJECT_STATE` is optional. A project state file — a running summary of active work, next steps, current context — gets injected on `source: "resume"` so Claude wakes from a restart already oriented. Set to `None` to disable.

---

## Logs

Both scripts write to `~/.claude/hooks/continuity.log`:

```
[2026-04-28T21:02:18] PreCompact fired. trigger=auto transcript=...
[2026-04-28T21:02:18] Checkpoint written. users=4 files=3 ops=32
[2026-04-28T21:02:32] SessionStart. source=compact  session=...
[2026-04-28T21:02:32] Injecting checkpoint (1361 chars).
```

---

## What this covers

- Checkpoint saved before every compaction — auto or manual (`/compact`)
- Warm restart: Claude wakes knowing the last 5 user messages, last response, every file it touched, and what operations it ran
- Falls back gracefully if the checkpoint is missing or stale
- `source: "resume"` path for session restarts
- `source: "startup"` and `source: "clear"` correctly inject nothing

---

## The architecture problem — and the next layer

This repo patches a deeper architectural issue. Understanding the gap explains why the patch works, and what would make it better.

### Hooks are doorbells, not windows

Claude Code's hook system is callback-based: events fire at specific lifecycle moments and your script gets called. That's the doorbell model — you get notified when something specific happens.

What the compaction problem actually needs is an **observer** — something watching continuously, aware of state as it changes, able to act before the moment arrives rather than at it.

The live token count you see in the Claude Code status bar (`138.8k / 200.0k (69%)`) exists inside the Claude Code process, updated in real-time from the SSE stream coming from the Anthropic API. Hooks are external. They can't see that stream. By the time a hook fires, the generation is already done.

### The proxy layer

The fix is a proxy that sits between Claude Code and the Anthropic API — intercepting the SSE stream before Claude Code processes it. The proxy watches every token arrive in real-time. It maintains a running count. And when thresholds are crossed, it **rings doorbells** — signals that trigger different hook responses.

```
Anthropic API
     |
     | SSE stream (every token, real-time)
     |
  PROXY  ←  the watcher
     |
     | 70% threshold  →  bell #1  →  write checkpoint (no rush)
     | 85% threshold  →  bell #2  →  write checkpoint urgently
     | 95% threshold  →  bell #3  →  emergency save, compaction imminent
     | tool event     →  bell #4  →  state change detected
     |
  HOOKS respond to each bell
     |
  Claude Code sees the hook output
```

The proxy and the hooks are both outside the Claude Code process. They can communicate with each other — via files, named pipes, or sockets. The proxy doesn't need to reach inside Claude Code. It just rings the bell. The hook system is already wired to respond.

Different threshold levels, different event types in the SSE stream, multiple proxies watching different things — the bell sounds tell the system exactly what it's responding to.

### Multiple stalkers

Nothing stops you from running multiple proxies watching different signals simultaneously:

- **Token stalker** — watches cumulative token count, rings bells at thresholds
- **Tool stalker** — watches for specific tool call patterns (10 file edits in a row, a Bash command that touches critical paths)
- **Timing stalker** — watches session duration, rings a bell at 2 hours

Each stalker is independent. Each rings its own doorbell. The hook system sorts out the responses.

---

## What still needs Anthropic

**Replace mode.** We append context via SessionStart, we don't replace native compaction output. The native compactor still runs. To fully replace what it produces with a structured checkpoint — which is where the real retention gains are — `PreCompact` needs to support returning content that substitutes the compaction summary. Documented in [anthropics/claude-code#47023](https://github.com/anthropics/claude-code/issues/47023).

**Token budget in PreCompact payload.** The checkpoint is capped at a fixed character limit. With `expected_post_compact_budget` in the PreCompact payload, sizing becomes precise.

**Quality signal.** No way to surface verified vs. best-effort, so Claude Code can't choose whether to trust the checkpoint over native compaction output.

**Defer capability.** To get the model to write its own checkpoint before compaction fires — rather than having a script infer state from the JSONL — `PreCompact` needs to support pausing compaction while the model externalizes critical context. [anthropics/claude-code#54118](https://github.com/anthropics/claude-code/issues/54118).

---

## The engine analogy

If you think of a Claude Code session as an engine cycle, compaction is the compression stroke — the piston squeezing the fuel-air mixture before ignition. The native compression is lossy: it vents some of the mixture before the burn.

What this repo adds is two additional strokes around the compression:

- **Pre-compression** — save the mixture before the squeeze
- **Post-compression injection** — put it back after

That's a 6-stroke engine. The proxy layer adds continuous pressure monitoring so the engine knows the compression stroke is coming before it arrives. The deeper layers — cross-session memory, multi-agent coordination, behavioral continuity, temporal reasoning — are the 8 through 18-stroke designs. Built from the bottom up.

---

## Background

Built out of the design discussion in [anthropics/claude-code#47023](https://github.com/anthropics/claude-code/issues/47023), which is consolidating several community memory projects into a concrete hook specification. The goal here is to show what the existing hook surface already makes possible, and to document the proxy architecture as the next step that doesn't require waiting for platform changes.

Related: [anthropics/claude-code#34556](https://github.com/anthropics/claude-code/issues/34556) — field data from 59+ documented compactions that motivated this work.

---

## License

MIT
