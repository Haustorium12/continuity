# continuity

**Warm restart for Claude Code sessions after context compaction.**

Two small hooks that save your session state before compaction fires and restore it the moment Claude wakes up. No lost context. No cold restart. No re-reading the same files to re-orient.

---

## The problem

Claude Code compacts the context window when it fills up. The native compaction is lossy — it summarizes what it can and discards the rest. After a compaction, Claude doesn't know what you were working on unless you tell it again. In long sessions this happens repeatedly, and each time is a small interruption that adds up.

The `PreCompact` hook exists precisely for this. But there's no built-in mechanism to take what it captures and get it back into Claude's context after compaction. That gap is what this solves.

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

The key is `SessionStart` with `source: "compact"`. Claude Code fires this event after every compaction and it supports `additionalContext` injection. That's the restore path — it exists in the current hook system without any changes from Anthropic.

---

## What the checkpoint looks like

```
=== COMPACTION CHECKPOINT ===
Session: 3ebbb836-4075-4911-a1b2-303244fb01f5
Saved:   2026-04-28T21:02
Trigger: auto   CWD: C:\dev

== RECENT USER MESSAGES (last 5) ==
  [2026-04-28T20:15] bootup and lets check in on github.
  [2026-04-28T20:26] and what is the end result that would be accomplished with this de facto spec?
  [2026-04-28T20:28] and can we use it for us like make the code for it....
  [2026-04-28T20:37] sure. greenlight. write them and install them and run all the checks.

== LAST ASSISTANT RESPONSE ==
[2026-04-28T21:02]
Here's the real picture — it's better than I expected.
All four hooks exist today. The missing piece from the proposal is...

== FILES TOUCHED THIS SESSION ==
  C:\Users\Sean\.claude\hooks\precompact_save.py
  C:\Users\Sean\.claude\hooks\session_start_inject.py
  C:\Users\Sean\.claude\settings.json

== RECENT OPERATIONS ==
  - Syntax check both hook scripts
  - Test precompact_save with properly formed Windows path payload
  - Inspect generated checkpoint content
  - Check both hook logs to see what fired

=== END CHECKPOINT ===
```

Claude wakes from compaction, reads this, and knows exactly where it was — without you saying a word.

---

## Installation

**1. Copy the scripts somewhere permanent**

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

**3. That's it.** The next time auto-compaction fires, the checkpoint is saved. The next time Claude wakes from a compaction, the checkpoint is waiting.

---

## Configuration

At the top of each script:

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
# Set to None to disable resume injection

CHECKPOINT_MAX_AGE_MINUTES = 180   # treat checkpoint as stale after 3 hours
CHECKPOINT_MAX_CHARS = 4000        # keep injected context tight
PROJECT_STATE_MAX_CHARS = 3000
```

`PROJECT_STATE` is optional. If you maintain a project state file (a running summary of active work, next steps, mood — something like a sticky note for Claude), you can point to it here. It gets injected on `source: "resume"` so Claude wakes from a restart already oriented.

---

## Logs

Both scripts append to `~/.claude/hooks/continuity.log`:

```
[2026-04-28T21:02:18] PreCompact fired. trigger=auto transcript=...
[2026-04-28T21:02:18] Checkpoint written. users=4 files=3 ops=32
[2026-04-28T21:02:32] SessionStart. source=compact  session=...
[2026-04-28T21:02:32] Injecting checkpoint (1361 chars).
```

If something isn't working, the log is the first place to look.

---

## What this covers

- Checkpoint saved before every compaction — auto or manual (`/compact`)
- Warm restart: Claude wakes knowing the last 5 user messages, last response, every file it touched, and what operations it ran
- Falls back gracefully if the checkpoint is missing or stale
- `source: "resume"` path for session restarts (separate from compaction)
- `source: "startup"` and `source: "clear"` correctly inject nothing — the boot protocol handles startup, and `/clear` is intentional

---

## What this doesn't cover (yet)

This pattern uses what the current hook system provides. Some things still need changes from Anthropic before they're possible:

**Replace mode.** Right now we're appending context, not replacing the native compaction output. The native compactor still runs and produces its own summary — the checkpoint is injected on top via `SessionStart`. To fully replace native compaction with a structured checkpoint, `PreCompact` needs to support returning content that substitutes the compaction output. That's the core of the proposal in [anthropics/claude-code#47023](https://github.com/anthropics/claude-code/issues/47023).

**Token budget in the PreCompact payload.** The checkpoint is capped at a fixed character limit (`CHECKPOINT_MAX_CHARS`). With `expected_post_compact_budget` in the PreCompact payload, the script could size its output precisely to the available headroom instead of guessing.

**Quality signal.** No way currently to surface whether the checkpoint faithfully represents the session or dropped something important. The `"verified" | "best-effort"` quality signal proposed in #47023 would let Claude Code decide whether to trust the checkpoint over the native compaction output.

---

## Background

This came out of a longer conversation about what the community is building around compaction — specifically the design work happening in [anthropics/claude-code#47023](https://github.com/anthropics/claude-code/issues/47023), which is consolidating several community memory projects into a concrete hook specification for Anthropic to consider.

The goal of this repo is to demonstrate what's already possible with the hook system today, and to make the remaining gap concrete rather than theoretical.

---

## License

MIT
