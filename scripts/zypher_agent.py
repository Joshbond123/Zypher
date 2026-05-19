#!/usr/bin/env python3
"""
Zypher Supabase Bridge v5
=========================
Improvements over v4:
- Resilient to missing Supabase tables (no crashes, graceful degradation)
- Faster 5s polling of OpenClaw log
- Robust JSON + regex parsing for all OpenClaw log formats
- Writes: chat_messages, task_log, agent_status, active_sessions heartbeat
- Refreshes MEMORY.md every 5 minutes from Supabase
- Writes daily memory/YYYY-MM-DD.md notes
- All Supabase calls wrapped in try/except with retry
- Uses merge-duplicates for upserts (safe on UNIQUE constraints)
"""
import os, json, time, re, logging, hashlib, threading
from datetime import datetime, timezone, date
from urllib.request import urlopen, Request
from urllib.error import HTTPError

logging.basicConfig(
    format="%(asctime)s %(levelname)s [sidecar] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("zypher-bridge")

SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY    = os.environ.get("SUPABASE_SERVICE_KEY", "")
GITHUB_RUN_ID   = os.environ.get("GITHUB_RUN_ID", "local")
LOG_FILE        = "/tmp/openclaw.log"
WORKSPACE_DIR   = os.path.expanduser("~/.openclaw/workspace")
MEMORY_DIR      = os.path.join(WORKSPACE_DIR, "memory")
POLL_INTERVAL   = 5       # seconds between log polls
HEARTBEAT_EVERY = 6       # heartbeat every N polls (30s)
MEMORY_FLUSH    = 60      # flush memory every N polls (5min)
SB_TIMEOUT      = 8       # Supabase request timeout
MAX_RETRIES     = 2       # retries on transient Supabase errors


def sb(method, path, body=None, params="", retries=MAX_RETRIES):
    """Make a Supabase REST API request. Returns (ok:bool, data:str)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False, "no-credentials"
    url = f"{SUPABASE_URL}/rest/v1/{path}{params}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal,resolution=merge-duplicates",
    }
    for attempt in range(retries + 1):
        req = Request(url, data=data, method=method, headers=headers)
        try:
            with urlopen(req, timeout=SB_TIMEOUT) as resp:
                return True, resp.read().decode()
        except HTTPError as e:
            code = e.code
            body_txt = e.read().decode()[:100]
            if code in (404, 400):
                # Table missing or bad request — don't retry, degrade gracefully
                log.debug("Supabase %s %s -> %d (table may be missing): %s", method, path, code, body_txt)
                return False, f"http_{code}"
            if code in (429, 503) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return False, f"http_{code}: {body_txt}"
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
                continue
            return False, str(e)
    return False, "max-retries"


class AgentState:
    def __init__(self):
        self.current_task  = ""
        self.last_tool     = ""
        self.tools_called  = 0
        self.status        = "idle"
        self.last_activity = time.time()
        self.lock          = threading.Lock()

    def record_tool(self, name):
        with self.lock:
            self.last_tool     = name
            self.tools_called += 1
            self.status        = "working"
            self.last_activity = time.time()

    def record_task(self, task):
        with self.lock:
            self.current_task  = (task or "")[:200]
            self.status        = "working"
            self.last_activity = time.time()

    def record_reply(self):
        with self.lock:
            self.status        = "idle"
            self.last_activity = time.time()

    def to_dict(self):
        with self.lock:
            now = datetime.now(timezone.utc).isoformat()
            return {
                "github_run_id": GITHUB_RUN_ID,
                "current_task":  self.current_task,
                "last_tool":     self.last_tool,
                "tools_called":  self.tools_called,
                "status":        self.status,
                "last_activity": now,
                "updated_at":    now,
            }


STATE = AgentState()

# Regex patterns for OpenClaw log parsing
USER_PAT = [
    re.compile(r'"text"\s*:\s*"([^"]{5,})"', re.I),
    re.compile(r'inbound.*?(?:from\s+\d+|dm)[:\s]+(.{5,})', re.I),
    re.compile(r'telegram.*?message[:\s]+(.{5,})', re.I),
]
ASST_PAT = [
    re.compile(r'\[(?:agent|assistant|reply)\][:\s]+(.{5,})', re.I),
    re.compile(r'(?:sending reply|telegram send|response sent)[:\s]+(.{5,})', re.I),
    re.compile(r'"role"\s*:\s*"assistant".*?"content"\s*:\s*"([^"]{5,})"', re.I),
]
TOOL_RE  = re.compile(
    r'(?:tool_call|exec|bash|web_fetch|web_search|browser|playwright)[:\s]+([^\n]{5,})', re.I
)
TASK_RE  = re.compile(
    r'(?:processing|received message|inbound dm|new message)[:\s]+(.{5,})', re.I
)
INTERESTING = {
    "error", "warn", "telegram", "cerebras", "exec", "tool",
    "browser", "web_fetch", "memory", "reply", "inbound", "fatal",
    "assistant", "user",
}


def parse_line(line):
    """Extract structured fields from a log line."""
    result = {}
    s = line.strip()
    if not s or len(s) < 8:
        return result
    try:
        obj = json.loads(s)
        msg = obj.get("msg", "") or obj.get("message", "")
        role = obj.get("role", "")
        if role == "user" or any(k in msg.lower() for k in ("inbound", "telegram message", "user message")):
            for pat in USER_PAT:
                m = pat.search(s)
                if m:
                    result["user_msg"] = m.group(1)[:500]
                    break
            if not result.get("user_msg") and len(msg) > 5:
                result["user_msg"] = msg[:500]
        if role == "assistant" or any(k in msg.lower() for k in ("sending reply", "telegram send")):
            for pat in ASST_PAT:
                m = pat.search(s)
                if m:
                    result["asst_msg"] = m.group(1)[:500]
                    break
            if not result.get("asst_msg") and len(msg) > 5:
                result["asst_msg"] = msg[:500]
        m = TOOL_RE.search(s)
        if m:
            result["tool"] = m.group(1)[:100]
        m = TASK_RE.search(s)
        if m and not result.get("user_msg"):
            result["task"] = m.group(1)[:200]
        return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Plain-text fallback
    s_low = s.lower()
    if not any(k in s_low for k in INTERESTING):
        return result
    for pat in USER_PAT:
        m = pat.search(s)
        if m:
            result["user_msg"] = m.group(1)[:500]
            break
    for pat in ASST_PAT:
        m = pat.search(s)
        if m:
            result["asst_msg"] = m.group(1)[:500]
            break
    m = TOOL_RE.search(s)
    if m:
        result["tool"] = m.group(1)[:100]
    return result


def line_hash(line):
    return hashlib.md5(line.encode()).hexdigest()


def save_message(role, content, session_id=None):
    """Persist a chat message to Supabase."""
    ok, _ = sb("POST", "chat_messages", {
        "session_id": session_id or GITHUB_RUN_ID,
        "role":       role,
        "content":    content[:4000],
        "user_id":    "joshbond",
    })
    return ok


def push_agent_status():
    """Upsert current agent status."""
    ok, _ = sb("POST", "agent_status", STATE.to_dict())
    return ok


def heartbeat():
    """Update session heartbeat."""
    now = datetime.now(timezone.utc).isoformat()
    sb("PATCH", "active_sessions",
       {"updated_at": now, "is_active": True},
       params=f"?github_run_id=eq.{GITHUB_RUN_ID}")


def refresh_memory():
    """Pull latest memory_entries from Supabase into MEMORY.md."""
    ok, body = sb("GET", "memory_entries",
                  params="?order=updated_at.desc&limit=50&select=key,value,category,updated_at")
    if not ok:
        return
    try:
        entries = json.loads(body)
        if not isinstance(entries, list) or not entries:
            return
        mem_path = os.path.join(WORKSPACE_DIR, "MEMORY.md")
        existing = open(mem_path).read() if os.path.exists(mem_path) else ""
        # Rebuild just the Long-term Facts section
        fact_lines = []
        for e in entries:
            cat = e.get("category", "")
            key = e.get("key", "?")
            val = str(e.get("value", ""))[:300]
            prefix = f"[{cat}] " if cat else ""
            fact_lines.append(f"- {prefix}{key}: {val}")
        # Replace or append the facts section
        marker = "## Long-term Facts"
        new_section = marker + "\n" + "\n".join(fact_lines)
        if marker in existing:
            # Replace from marker to next ## or end
            before = existing[:existing.index(marker)]
            after_start = existing.index(marker) + len(marker)
            rest = existing[after_start:]
            next_h2 = rest.find("\n## ", 1)
            after = rest[next_h2:] if next_h2 > 0 else ""
            new_content = before + new_section + after
        else:
            new_content = existing + "\n\n" + new_section
        os.makedirs(WORKSPACE_DIR, exist_ok=True)
        with open(mem_path, "w") as f:
            f.write(new_content)
        log.info("Memory refreshed: %d entries", len(entries))
    except Exception as e:
        log.debug("Memory refresh error: %s", e)


def write_daily_note(text, role="event"):
    """Append to today's daily log."""
    today = date.today().isoformat()
    daily_path = os.path.join(MEMORY_DIR, f"{today}.md")
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        with open(daily_path, "a") as f:
            ts = datetime.now(timezone.utc).strftime("%H:%M")
            f.write(f"[{ts}] {role.upper()}: {text[:300]}\n")
    except Exception as e:
        log.debug("Daily note error: %s", e)


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.warning("No Supabase credentials — sidecar running in no-op mode")
        # Keep process alive so the workflow doesn't restart it
        while True:
            time.sleep(60)

    log.info("Sidecar started | run=%s | poll=%ds", GITHUB_RUN_ID, POLL_INTERVAL)

    seen_hashes = set()
    poll_count  = 0
    log_pos     = 0  # byte offset in log file

    while True:
        try:
            time.sleep(POLL_INTERVAL)
            poll_count += 1

            # Read new lines from the OpenClaw log
            new_lines = []
            try:
                with open(LOG_FILE, "r", errors="replace") as f:
                    f.seek(log_pos)
                    raw = f.read()
                    log_pos = f.tell()
                if raw:
                    new_lines = [l for l in raw.splitlines() if l.strip()]
            except FileNotFoundError:
                pass
            except Exception as e:
                log.debug("Log read error: %s", e)

            # Process new lines
            for line in new_lines:
                h = line_hash(line)
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                # Keep seen set bounded
                if len(seen_hashes) > 5000:
                    seen_hashes.clear()

                parsed = parse_line(line)

                if parsed.get("user_msg"):
                    msg = parsed["user_msg"]
                    STATE.record_task(msg)
                    save_message("user", msg)
                    write_daily_note(msg, "user")

                if parsed.get("asst_msg"):
                    msg = parsed["asst_msg"]
                    STATE.record_reply()
                    save_message("assistant", msg)
                    write_daily_note(msg, "assistant")

                if parsed.get("tool"):
                    tool = parsed["tool"]
                    STATE.record_tool(tool)

            # Heartbeat
            if poll_count % HEARTBEAT_EVERY == 0:
                heartbeat()
                push_agent_status()

            # Memory refresh
            if poll_count % MEMORY_FLUSH == 0:
                refresh_memory()

        except KeyboardInterrupt:
            log.info("Sidecar shutting down")
            break
        except Exception as e:
            log.error("Poll loop error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
