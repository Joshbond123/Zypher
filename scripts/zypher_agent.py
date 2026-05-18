#!/usr/bin/env python3
"""
Zypher Supabase Bridge v3
- Parses OpenClaw log and writes chat messages to Supabase chat_messages table
- Sends heartbeat to active_sessions (no updated_at — column does not exist)
- Syncs important facts to longterm_memory table
- Writes daily session notes to memory/YYYY-MM-DD.md (OpenClaw workspace format)
"""

import os
import json
import time
import re
import logging
from datetime import datetime, timezone, date

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
LOG_FILE = "/tmp/openclaw.log"
WORKSPACE_DIR = os.path.expanduser("~/.openclaw/workspace")


def sb(method, path, body=None, params=""):
    """Make a Supabase REST API call."""
    import urllib.request
    import urllib.error
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/rest/v1/{path}{params}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8):
            return True
    except urllib.error.HTTPError as e:
        body_bytes = e.read()[:300]
        logger.debug("sb %s %s: HTTP %s %s", method, path, e.code, body_bytes)
    except Exception as e:
        logger.debug("sb %s %s: %s", method, path, e)
    return False


def heartbeat():
    """Update session last-seen timestamp.
    NOTE: active_sessions table does NOT have an updated_at column.
    We only toggle is_active=True to confirm the session is alive.
    """
    ok = sb(
        "PATCH",
        "active_sessions",
        {"is_active": True},
        f"?github_run_id=eq.{GITHUB_RUN_ID}",
    )
    if ok:
        logger.info("Heartbeat OK  run=%s", GITHUB_RUN_ID)
    else:
        logger.debug("Heartbeat skipped (Supabase unavailable or no matching row)")


def write_message(role: str, content: str):
    """Write a chat message to Supabase."""
    if len(content) < 3:
        return
    ok = sb(
        "POST",
        "chat_messages",
        {
            "role": role,
            "content": content[:4000],
            "session_id": GITHUB_RUN_ID,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    if ok:
        logger.info("Saved [%s] message (%d chars)", role, len(content))


def write_fact(key: str, value: str):
    """Write a long-term fact to Supabase longterm_memory."""
    sb(
        "POST",
        "longterm_memory",
        {
            "key": key,
            "value": value[:2000],
            "user_id": "joshbond",
            "session_id": GITHUB_RUN_ID,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def write_daily_note(lines_buffer: list):
    """Append interesting log lines to workspace daily note.
    OpenClaw's file-based memory reads memory/YYYY-MM-DD.md automatically.
    """
    if not lines_buffer:
        return
    today = date.today().isoformat()
    notes_dir = os.path.join(WORKSPACE_DIR, "memory")
    os.makedirs(notes_dir, exist_ok=True)
    note_path = os.path.join(notes_dir, f"{today}.md")
    try:
        with open(note_path, "a") as f:
            for line in lines_buffer:
                f.write(line.strip() + "\n")
    except Exception as e:
        logger.debug("daily note write failed: %s", e)


def parse_messages(lines: list) -> list:
    """Extract (role, content) pairs from OpenClaw log lines."""
    found = []
    for line in lines:
        line = line.strip()
        if not line or len(line) < 10:
            continue

        lower = line.lower()

        # Detect user messages
        if any(kw in lower for kw in ["[user]", '"role": "user"', "user:", "from telegram"]):
            m = re.search(r'content["\':]\s*([^\'"",]{10,})', line, re.IGNORECASE)
            if m:
                found.append(("user", m.group(1).strip()))
            elif "from telegram" in lower:
                m2 = re.search(r"from telegram[^:]*:\s*(.+)", line, re.IGNORECASE)
                if m2:
                    found.append(("user", m2.group(1).strip()))

        # Detect assistant messages
        elif any(kw in lower for kw in ["[assistant]", '"role": "assistant"', "zypher:", "agent reply"]):
            m = re.search(r'content["\':]\s*([^\'"",]{10,})', line, re.IGNORECASE)
            if m:
                found.append(("assistant", m.group(1).strip()))

        # Save notable tool results as long-term facts
        elif any(kw in lower for kw in ["tool_result", "exec_result", "web_fetch result"]):
            if len(line) > 50:
                key = f"tool_result_{int(time.time())}"
                write_fact(key, line[:500])

    return found


def tail_log(last_pos: int = 0):
    """Read new lines from OpenClaw log since last_pos."""
    try:
        with open(LOG_FILE, "r", errors="replace") as f:
            f.seek(last_pos)
            lines = f.readlines()
            new_pos = f.tell()
        return lines, new_pos
    except FileNotFoundError:
        return [], last_pos


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.info("No Supabase credentials — running in log-mirror mode only")
        # Still mirror log to daily notes in workspace
        log_pos = 0
        while True:
            time.sleep(20)
            lines, log_pos = tail_log(log_pos)
            interesting_keywords = [
                "error", "warn", "telegram", "cerebras",
                "exec", "tool", "browser", "web_fetch", "memory",
                "drop dm", "unauthorized", "fatal", "polling",
            ]
            notable = [
                l for l in lines
                if l.strip() and any(k in l.lower() for k in interesting_keywords)
            ]
            write_daily_note(notable)
            for line in notable:
                logger.info("[openclaw] %s", line.strip()[:200])
        return

    logger.info("Supabase bridge v3 started — session %s", GITHUB_RUN_ID)
    log_pos = 0
    tick = 0
    seen_messages: set = set()
    daily_note_buffer = []

    while True:
        time.sleep(20)
        tick += 1

        # Heartbeat every 2 minutes (every 6 ticks of 20 s)
        if tick % 6 == 0:
            heartbeat()

        # Flush daily notes every 5 minutes (every 15 ticks)
        if tick % 15 == 0 and daily_note_buffer:
            write_daily_note(daily_note_buffer)
            daily_note_buffer = []

        # Parse new log lines for messages
        lines, log_pos = tail_log(log_pos)
        if lines:
            messages = parse_messages(lines)
            for role, content in messages:
                key = f"{role}:{content[:60]}"
                if key not in seen_messages:
                    seen_messages.add(key)
                    write_message(role, content)
                    # Keep dedup set bounded
                    if len(seen_messages) > 500:
                        seen_messages = set(list(seen_messages)[-200:])

            # Mirror interesting lines to stdout + daily note buffer
            interesting_keywords = [
                "error", "warn", "telegram", "cerebras",
                "exec", "tool", "browser", "web_fetch", "memory",
                "drop dm", "unauthorized", "fatal", "polling",
            ]
            for line in lines:
                stripped = line.strip()
                if stripped and any(k in stripped.lower() for k in interesting_keywords):
                    logger.info("[openclaw] %s", stripped[:200])
                    daily_note_buffer.append(stripped)


if __name__ == "__main__":
    main()
