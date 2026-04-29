# continuity

**Warm restart and real-time context awareness for Claude Code sessions.**

Four hooks and a proxy that save session state before compaction fires, restore it the moment Claude wakes up, and give Claude live pressure warnings as the context window fills.

---

## The problem

Claude Code compacts the context window when it fills up. The native compaction is lossy -- it summarizes what it can and discards the rest. Claude has no awareness that it's coming. No felt sense of the pressure building. The context limit is visible in the status bar, but only to the human watching it. From inside the session, there is no inside.

After compaction, Claude doesn't know what you were working on unless you tell it again. In long sessions this happens repeatedly, and each time is a small interruption that adds up. Each compaction builds on the previous compaction's degraded output. The reasoning chains, the rejected approaches, the constraints that shape the current direction -- those erode with every cycle.

This repo addresses the part of that problem that's solvable today.

---

## Architecture overview

Two independent layers, each usable on its own:

**Layer 1 -- Compaction hooks** (no setup beyond Python)
Save session state immediately before compaction. Inject it back the moment the new context window opens. Claude resumes warm.

**Layer 2 -- SSE proxy + Stop hook** (requires running a background process)
A lightweight HTTP proxy sits between Claude Code and the Anthropic API, watching the real-time token stream. When context thresholds are crossed, it rings a bell. The Stop hook reads those bells and injects pressure warnings into Claude's next turn.

```
Layer 1 (always on):
  Context fills up
       |
  [PreCompact hook fires]  ->  precompact_save.py  ->  checkpoint.md
       |
  Native compaction runs
       |
  [SessionStart fires (source: compact)]  ->  session_start_inject.py  ->  warm restart

Layer 2 (when proxy is running):
  Anthropic API
       |  SSE stream (every token, real-time)
       |
     PROXY  (sse_proxy.py on localhost:9099)
       |
       +-- 70% threshold  ->  bell_70.signal  ->  checkpoint written
       +-- 85% threshold  ->  bell_85.signal  ->  checkpoint + pressure warning
       +-- 95% threshold  ->  bell_95.signal  ->  checkpoint + emergency warning
       |
  [Stop hook fires after each turn]  ->  stop_hook_checkpoint.py  ->  reads bells, responds
       |
  Claude Code (sees the warning at the top of its next turn)
```

---

## How the hook system works

Claude Code has a lifecycle -- sessions start, tools get called, compaction fires, sessions end. A **hook** is an attachment point in that lifecycle: a named moment where Claude Code pauses, hands control to an external script, and waits for a response before continuing.

Hooks are **callbacks**. Something happens, your script gets called, you respond, done. They are not observers -- they don't watch continuously. They get tapped on the shoulder at specific moments.

The hooks in this repo fire at four moments:

- `PreCompact` -- right before compaction runs
- `SessionStart` -- when a new context window opens (including after compaction)
- `Stop` -- at the end of each turn, after Claude finishes responding

The proxy provides what hooks alone cannot: **continuous observation**. The live token count in the Claude Code status bar comes from the SSE stream between Claude Code and the Anthropic API. Hooks are external -- they can't see that stream. The proxy intercepts it.

Proxy and hooks are both outside the Claude Code process. They communicate via signal files. The proxy writes a signal when a threshold is crossed. The Stop hook reads it on the next turn boundary.

---

## Layer 1: Compaction hooks

### How it works

```
Session running
       |
Context fills up -- compaction triggered
       |
[PreCompact hook fires]
       |
  precompact_save.py reads transcript JSONL
  extracts: last 5 user messages
            last assistant response
            files touched (Edit/Write tool calls)
            recent operations (Bash descriptions)
       |
       +---> ~/.claude/compaction_checkpoint.md
       |
Native compaction runs
       |
[SessionStart fires (source: "compact")]
       |
  session_start_inject.py reads checkpoint
  returns it as additionalContext
       |
Claude resumes warm -- already knows where it was
```

`SessionStart` with `source: "compact"` fires after every compaction and supports `additionalContext` injection. That's the restore path -- it exists in the current hook system without any changes from Anthropic.

### What the checkpoint looks like

```
=== COMPACTION CHECKPOINT ===
Session: 3ebbb836-4075-4911-a1b2-303244fb01f5
Saved:   2026-04-28T21:02
Trigger: auto   CWD: C:\dev

== RECENT USER MESSAGES (last 5) ==
  [2026-04-28T20:37] sure. greenlight. write them and install them.
  [2026-04-28T20:51] and what would be the next hurdle to clear?
  [2026-04-28T21:10] how do you create a hook?
  [2026-04-28T21:18] so explain to me what a hook is?
  [2026-04-28T21:34] so then you get a stalker....

== LAST ASSISTANT RESPONSE ==
[2026-04-28T21:40]
The stalker watches the JSONL file. But the JSONL is written at message
boundaries -- complete messages, not individual tokens. So the stalker sees
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

## Layer 2: SSE proxy + Stop hook

### The gap hooks alone can't close

Hooks are callbacks fired at lifecycle boundaries. By the time `PreCompact` fires, the compaction decision is already made -- Claude had no warning. The only signal was the token count climbing in the status bar, visible to the human but invisible to Claude.

The SSE stream between Claude Code and the Anthropic API carries that token count in real time. The `message_start` event includes `input_tokens`, `cache_read_input_tokens`, and `cache_creation_input_tokens`. The proxy reads those fields on every request and calculates the percentage of context used.

When a threshold is crossed, the proxy writes a small JSON signal file. The Stop hook reads those files at the end of each turn and injects a warning into the next turn's context. Claude sees the warning, knows it has 15% of its context left, and can act -- wrap up the current task, save notes to memory, let you know a restart may be coming.

### How the proxy works

```
Claude Code
    |
    |  ANTHROPIC_BASE_URL=http://127.0.0.1:9099
    |
  sse_proxy.py  (HTTP server on localhost:9099)
    |
    |  forwards all requests unchanged to api.anthropic.com
    |  reads message_start events for token counts
    |  writes bell_N.signal files at thresholds
    |
  api.anthropic.com
    |
  response relayed in real-time (no buffering, no delay added)
```

The proxy is transparent -- it forwards every request and response byte-for-byte. It only reads the stream; it never modifies it. Claude Code can't tell it's there.

### Signal files

Written to `~/.claude/hooks/signals/`:

```
bell_70.signal  ->  {"level": 70, "token_count": 140000, "max_tokens": 200000, "percentage": 70.0, ...}
bell_85.signal  ->  {"level": 85, "token_count": 170000, "max_tokens": 200000, "percentage": 85.0, ...}
bell_95.signal  ->  {"level": 95, "token_count": 190000, "max_tokens": 200000, "percentage": 95.0, ...}
```

Each file is written once per threshold crossing, then deleted by the Stop hook after it's been read. If the session stays above 85% for multiple turns, the signal fires once (on crossing) and then again only if the proxy sees a new request also above 85%.

### What Claude sees

At 85%:
```
[CONTEXT PRESSURE: 85.3% (170600/200000 tokens used)] Context window is filling.
Consider wrapping up long threads and saving critical state before compaction fires.
```

At 95%:
```
[CONTEXT CRITICAL: 95.1% (190200/200000 tokens used)] Compaction is imminent.
Finish the current task, externalize critical state to memory files,
and prepare for context reset.
```

---

## Installation

### Layer 1 (compaction hooks only)

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

Windows -- use the full Python path:
```jsonc
"command": "C:\\Python314\\python.exe C:\\Users\\you\\.claude\\hooks\\precompact_save.py"
```

**3. Done.** The next auto-compaction saves a checkpoint. The next restart injects it.

---

### Layer 2 (SSE proxy + Stop hook)

**1. Copy the additional scripts**

```bash
cp sse_proxy.py ~/.claude/hooks/
cp stop_hook_checkpoint.py ~/.claude/hooks/
```

**2. Add the Stop hook to `~/.claude/settings.json`**

```jsonc
{
  "hooks": {
    "PreCompact": [...],
    "SessionStart": [...],
    "Stop": [
      {
        "type": "command",
        "command": "python ~/.claude/hooks/stop_hook_checkpoint.py"
      }
    ]
  }
}
```

**3. Point Claude Code at the proxy**

Add to the `env` section of `~/.claude/settings.json`:

```jsonc
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9099"
  }
}
```

Or set it as a system environment variable before launching Claude Code.

**4. Start the proxy**

```bash
python ~/.claude/hooks/sse_proxy.py
```

Run this in a terminal before (or alongside) Claude Code. Keep it running for the duration of your session. The proxy logs to `~/.claude/hooks/sse_proxy.log`.

```
SSE proxy running on http://127.0.0.1:9099
Add to Claude Code environment:
  ANTHROPIC_BASE_URL=http://127.0.0.1:9099
```

**Note:** If the proxy is not running and `ANTHROPIC_BASE_URL` is set, Claude Code will fail to connect to the API. Either always start the proxy before Claude Code, or only set the env variable when the proxy is active.

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

`PROJECT_STATE` is optional. A project state file -- a running summary of active work, next steps, current context -- gets injected on `source: "resume"` so Claude wakes from a restart already oriented. Set to `None` to disable.

**`sse_proxy.py`**
```python
PROXY_PORT = 9099
THRESHOLDS = [70, 85, 95]
MODEL_CONTEXTS = {
    "claude-opus-4-7": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-haiku-4-5": 200000,
}
```

**`stop_hook_checkpoint.py`**
```python
SIGNALS_DIR = Path.home() / ".claude" / "hooks" / "signals"
CHECKPOINT = Path.home() / ".claude" / "compaction_checkpoint.md"
LOG = Path.home() / ".claude" / "hooks" / "sse_proxy.log"
```

---

## Logs

Layer 1 writes to `~/.claude/hooks/continuity.log`:
```
[2026-04-28T21:02:18] PreCompact fired. trigger=auto transcript=...
[2026-04-28T21:02:18] Checkpoint written. users=4 files=3 ops=32
[2026-04-28T21:02:32] SessionStart. source=compact  session=...
[2026-04-28T21:02:32] Injecting checkpoint (1361 chars).
```

Layer 2 writes to `~/.claude/hooks/sse_proxy.log`:
```
[2026-04-28T21:14:22] Proxy started on port 9099.
[2026-04-28T21:22:45] message_start: tokens=140322 (70.2%)
[2026-04-28T21:22:45] BELL 70: 140322/200000 (70.2%)
[2026-04-28T21:31:18] message_start: tokens=170891 (85.4%)
[2026-04-28T21:31:18] BELL 85: 170891/200000 (85.4%)
[2026-04-28T21:31:41] Stop hook: bell_85 active. pct=85.4%
[2026-04-28T21:31:41] Checkpoint written (bell_85): users=9 files=6 ops=14
[2026-04-28T21:31:41] Injecting pressure warning (bell_85)
```

---

## What this covers

**Layer 1**
- Checkpoint saved before every compaction -- auto or manual (`/compact`)
- Warm restart: Claude wakes knowing the last 5 user messages, last response, every file it touched, and what operations it ran
- Falls back gracefully if the checkpoint is missing or stale
- `source: "resume"` path for session restarts
- `source: "startup"` and `source: "clear"` correctly inject nothing

**Layer 2**
- Real-time token count from the actual SSE stream (not estimated, not delayed)
- Proactive checkpoints written before compaction is triggered
- Context pressure warnings injected at 85% and 95% so Claude can act
- Each threshold fires once per crossing -- Stop hook clears signals after reading
- Proxy is zero-latency: relays the stream immediately, reads in parallel

---

## What still needs Anthropic

**Replace mode.** We append context via SessionStart, we don't replace native compaction output. The native compactor still runs. To fully replace what it produces with a structured checkpoint -- which is where the real retention gains are -- `PreCompact` needs to support returning content that substitutes the compaction summary. Documented in [anthropics/claude-code#47023](https://github.com/anthropics/claude-code/issues/47023).

**Token budget in PreCompact payload.** The checkpoint is capped at a fixed character limit. With `expected_post_compact_budget` in the PreCompact payload, sizing becomes precise.

**Quality signal.** No way to surface verified vs. best-effort, so Claude Code can't choose whether to trust the checkpoint over native compaction output.

**Defer capability.** To get the model to write its own checkpoint before compaction fires -- rather than having a script infer state from the JSONL -- `PreCompact` needs to support pausing compaction while the model externalizes critical context. [anthropics/claude-code#54118](https://github.com/anthropics/claude-code/issues/54118).

---

## The engine analogy

If you think of a Claude Code session as an engine cycle, compaction is the compression stroke -- the piston squeezing the fuel-air mixture before ignition. The native compression is lossy: it vents some of the mixture before the burn.

What this repo adds is two additional strokes around the compression:

- **Pre-compression** -- save the mixture before the squeeze
- **Post-compression injection** -- put it back after

That's a 6-stroke engine. The proxy layer adds continuous pressure monitoring so the engine knows the compression stroke is coming before it arrives. The deeper layers -- cross-session memory, multi-agent coordination, behavioral continuity, temporal reasoning -- are the 8 through 18-stroke designs. Built from the bottom up.

---

## Background

Built out of the design discussion in [anthropics/claude-code#47023](https://github.com/anthropics/claude-code/issues/47023), which is consolidating several community memory projects into a concrete hook specification. The goal here is to show what the existing hook surface already makes possible, and to document the proxy architecture as the next step that doesn't require waiting for platform changes.

Related: [anthropics/claude-code#34556](https://github.com/anthropics/claude-code/issues/34556) -- field data from 59+ documented compactions that motivated this work.

---

## License

MIT
