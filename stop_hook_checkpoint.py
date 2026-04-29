"""
stop_hook_checkpoint.py

Fires on every Stop event (end of turn).
Reads bell signal files written by sse_proxy.py.

If any bells have fired since the last check:
  bell_70 -> write/update checkpoint, no warning
  bell_85 -> write/update checkpoint + inject mild pressure warning
  bell_95 -> write/update checkpoint + inject emergency warning

The warning is injected as additionalContext so Claude sees it
at the top of the next turn and can react (wrap up, save state, etc.).

Bells are cleared after reading so each threshold fires once
per crossing, not once per turn.
"""

import sys
import json
import os
from datetime import datetime
from pathlib import Path

SIGNALS_DIR = Path.home() / ".claude" / "hooks" / "signals"
CHECKPOINT = Path.home() / ".claude" / "compaction_checkpoint.md"
LOG = Path.home() / ".claude" / "hooks" / "sse_proxy.log"


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(datetime.now().isoformat()[:19], msg))
    except Exception:
        pass


def read_and_clear_signals():
    """Return (highest_level, signal_data_dict). Clears all signal files."""
    highest = None
    data = {}
    for level in [95, 85, 70]:
        path = SIGNALS_DIR / "bell_{}.signal".format(level)
        if path.exists():
            try:
                data[level] = json.loads(path.read_text(encoding="utf-8"))
                if highest is None:
                    highest = level
            except Exception:
                pass
            try:
                path.unlink()
            except Exception:
                pass
    return highest, data


# ---- transcript parsing (mirrors precompact_save.py) ----

def extract_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts).strip()
    return ""


def parse_transcript(transcript_path):
    user_msgs = []
    assistant_msgs = []
    files_touched = []
    bash_descs = []
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                mtype = obj.get("type")
                ts = obj.get("timestamp", "")[:16]
                if mtype == "user":
                    content = obj.get("message", {}).get("content", [])
                    if isinstance(content, list) and any(
                        isinstance(c, dict) and c.get("type") == "tool_result"
                        for c in content
                    ):
                        continue
                    text = extract_text(content)
                    if text:
                        user_msgs.append((ts, text))
                elif mtype == "assistant":
                    content = obj.get("message", {}).get("content", [])
                    if not isinstance(content, list):
                        continue
                    text_parts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "tool_use":
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            if name in ("Edit", "Write"):
                                fp = inp.get("file_path", "")
                                if fp and fp not in files_touched:
                                    files_touched.append(fp)
                            elif name == "Bash":
                                desc = inp.get("description", "")
                                cmd = inp.get("command", "")
                                entry = desc if desc else cmd[:80]
                                if entry:
                                    bash_descs.append(entry)
                    text = "\n".join(text_parts).strip()
                    if text:
                        assistant_msgs.append((ts, text))
    except Exception as e:
        log("parse_transcript error: {}".format(e))
    return user_msgs, assistant_msgs, files_touched, bash_descs


def write_checkpoint(transcript_path, session_id, level, pct):
    try:
        user_msgs, assistant_msgs, files_touched, bash_descs = parse_transcript(transcript_path)
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
        lines = [
            "=== COMPACTION CHECKPOINT (proactive @ {}%) ===".format(int(pct)),
            "Session: {}".format(session_id),
            "Saved:   {}".format(now),
            "Trigger: bell_{}".format(level),
            "",
            "== RECENT USER MESSAGES (last 5) ==",
        ]
        for ts, text in user_msgs[-5:]:
            lines.append("  [{}] {}".format(ts, text[:200].replace("\n", " ")))
        lines.append("")
        lines.append("== LAST ASSISTANT RESPONSE ==")
        if assistant_msgs:
            ts, text = assistant_msgs[-1]
            lines.append("[{}]".format(ts))
            lines.append(text[:1000])
        else:
            lines.append("(none recorded)")
        lines.append("")
        if files_touched:
            lines.append("== FILES TOUCHED THIS SESSION ==")
            for fp in files_touched[-20:]:
                lines.append("  {}".format(fp))
            lines.append("")
        if bash_descs:
            lines.append("== RECENT OPERATIONS ==")
            for desc in bash_descs[-10:]:
                lines.append("  - {}".format(desc[:100]))
            lines.append("")
        lines.append("=== END CHECKPOINT ===")
        CHECKPOINT.write_text("\n".join(lines), encoding="utf-8")
        log("Checkpoint written (bell_{}): users={} files={} ops={}".format(
            level, len(user_msgs), len(files_touched), len(bash_descs)))
    except Exception as e:
        log("Checkpoint write error: {}".format(e))


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        payload = {}

    highest, signal_data = read_and_clear_signals()

    if highest is None:
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    transcript_path = payload.get("transcript_path", "")
    info = signal_data.get(highest, {})
    pct = info.get("percentage", float(highest))
    token_count = info.get("token_count", "?")
    max_tokens = info.get("max_tokens", 200000)

    log("Stop hook: bell_{} active. pct={:.1f}% session={}".format(highest, pct, session_id))

    if transcript_path and os.path.exists(transcript_path):
        write_checkpoint(transcript_path, session_id, highest, pct)
    else:
        log("No transcript path in Stop payload -- skipping checkpoint write.")

    if highest >= 85:
        if highest >= 95:
            msg = (
                "[CONTEXT CRITICAL: {:.1f}% ({}/{} tokens used)] "
                "Compaction is imminent. Finish the current task, "
                "externalize critical state to memory files, "
                "and prepare for context reset."
            ).format(pct, token_count, max_tokens)
        else:
            msg = (
                "[CONTEXT PRESSURE: {:.1f}% ({}/{} tokens used)] "
                "Context window is filling. Consider wrapping up long threads "
                "and saving critical state before compaction fires."
            ).format(pct, token_count, max_tokens)

        log("Injecting pressure warning (bell_{})".format(highest))
        print(json.dumps({
            "hookSpecificOutput": {
                "additionalContext": msg
            }
        }))

    sys.exit(0)


if __name__ == "__main__":
    main()
