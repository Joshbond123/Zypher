#!/usr/bin/env python3
"""
Zypher Status Agent v3 — Fast Progress Reporter
================================================
v3 improvements over v2:
- Poll interval: 4s (was 8s) — 2x faster log detection
- Stall threshold: 45s (was 150s) — alerts 3x sooner
- Stall repeat cooldown: 90s (was 300s)
- Pulse interval: 120s/2min (was 900s/15min)
- Immediate "received" ack when user message detected
- Tool-call announcements for expensive tools (exec, browser, web_search)
- Better JSON log parsing covers more OpenClaw 2026.5.x log formats
- Startup delay: 20s (was 30s)
- Silent tool calls below threshold to reduce noise
"""
import os, re, json, time, logging, hashlib
from urllib.request import urlopen, Request
from urllib.error import HTTPError

logging.basicConfig(format="%(asctime)s [STATUS] %(message)s", level=logging.INFO)
log = logging.getLogger("status-agent")

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID         = "6317345496"
GITHUB_RUN_ID   = os.environ.get("GITHUB_RUN_ID", "?")
INSTANCE_NUM    = os.environ.get("INSTANCE_NUMBER", "1")
LOG_FILE        = "/tmp/openclaw.log"
POLL_INTERVAL   = 4        # seconds between log polls (was 8)
STALL_THRESHOLD = 45       # seconds before stall alert (was 150)
STALL_COOLDOWN  = 90       # seconds between repeated stall alerts (was 300)
PULSE_INTERVAL  = 120      # 2min pulse when busy (was 900/15min)
ERROR_COOLDOWN  = 120      # seconds between duplicate error alerts (was 180)
STARTUP_DELAY   = 20       # seconds after launch before startup message (was 30)
TOOL_ANNOUNCE_THRESHOLD = 1  # announce every Nth expensive tool call (1=all)

# Tools worth announcing (expensive/long-running ones)
EXPENSIVE_TOOLS = {
    "exec", "bash", "shell", "web_search", "search", "tavily",
    "browser", "playwright", "web_fetch", "fetch",
    "computer", "screenshot",
}


def tg_send(text, silent=False):
    """Send a Telegram message. Returns True on success."""
    if not TELEGRAM_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN — notifications disabled")
        return False
    try:
        payload = {
            "chat_id":              CHAT_ID,
            "text":                 text[:4096],
            "disable_notification": silent,
        }
        data = json.dumps(payload).encode()
        req = Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=12) as r:
            result = json.loads(r.read())
            return result.get("ok", False)
    except HTTPError as e:
        log.debug("tg_send HTTPError %d: %s", e.code, e.read().decode()[:100])
        return False
    except Exception as e:
        log.debug("tg_send error: %s", e)
        return False


def tg_typing():
    """Send typing action to Telegram (shows '...' indicator)."""
    if not TELEGRAM_TOKEN:
        return False
    try:
        payload = {"chat_id": CHAT_ID, "action": "typing"}
        data = json.dumps(payload).encode()
        req = Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",
            data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=5) as r:
            return True
    except Exception:
        return False


# Regex patterns for OpenClaw 2026.5.x log formats
TOOL_RE = re.compile(
    r'(?:tool_call|exec|bash|web_fetch|web_search|browser|playwright|'
    r'running tool|executing|tool use|calling tool|tool:\s*)\s*[:\s]\s*([^\n]{3,80})',
    re.I
)
USER_RE = re.compile(
    r'(?:inbound|from telegram|user message|telegram message|'
    r'received message|incoming|dm from|message from)\s*[:\s]\s*(.{3,100})',
    re.I
)
REPLY_RE = re.compile(
    r'(?:sending reply|telegram send|response sent|reply sent|'
    r'message sent|delivered|outbound|sent to telegram)',
    re.I
)
ERROR_RE = re.compile(
    r'(?:\berror\b|\bERROR\b|fatal|crash|refused|timeout|'
    r'rate.?limit|EFATAL|exception|EmbeddedAttempt|Takeover)',
    re.I
)
LANE_ERROR_RE = re.compile(r'lane task error.*error="([^"]{5,})"', re.I)
JSON_TOOL_RE  = re.compile(r'"tool(?:_call|Name|Call)?"\s*:\s*"([^"]+)"', re.I)
JSON_USER_RE  = re.compile(r'"role"\s*:\s*"user"', re.I)
JSON_ASST_RE  = re.compile(r'"role"\s*:\s*"assistant"', re.I)
INBOUND_RE    = re.compile(r'[Ii]nbound.*?(?:->|from)\s*\S+.*?(\d+\s*chars?|chars?\s*=?\s*\d+)', re.I)


def parse_line_event(line):
    """Return (event_type, event_data) from a log line.

    event_type: 'user' | 'tool' | 'reply' | 'error' | None
    """
    s = line.strip()
    if not s or len(s) < 4:
        return None, None

    # --- JSON structured log lines ---
    try:
        obj = json.loads(s)
        msg   = str(obj.get("msg", "") or obj.get("message", "") or "")
        role  = str(obj.get("role", ""))
        level = str(obj.get("level", "") or obj.get("severity", ""))
        msg_l = msg.lower()

        # User message
        if (role == "user"
                or "inbound" in msg_l
                or "telegram message" in msg_l
                or "user message" in msg_l
                or "from telegram" in msg_l):
            text = (obj.get("text") or obj.get("content") or msg or "")[:120]
            return "user", text

        # Assistant reply
        if (role == "assistant"
                or "sending reply" in msg_l
                or "response sent" in msg_l
                or "outbound" in msg_l
                or "telegram send" in msg_l):
            return "reply", msg[:80]

        # Tool call (multiple patterns)
        tool = (obj.get("tool") or obj.get("toolName") or obj.get("tool_name")
                or obj.get("toolCall"))
        if tool:
            return "tool", str(tool)[:60]
        if "tool_call" in msg_l or "calling tool" in msg_l or "tool use" in msg_l:
            return "tool", msg[:60]
        m = JSON_TOOL_RE.search(s)
        if m:
            return "tool", m.group(1)[:60]

        # JSON role shortcuts
        if JSON_USER_RE.search(s):
            return "user", msg[:80]
        if JSON_ASST_RE.search(s):
            return "reply", msg[:80]

        # Error / fatal
        if level in ("error", "fatal") or "error" in msg_l or obj.get("err"):
            err_text = msg or str(obj.get("err", ""))
            return "error", err_text[:200]

        # Lane task errors (OpenClaw internal)
        m = LANE_ERROR_RE.search(s)
        if m:
            return "error", m.group(1)[:200]

        return None, None

    except (json.JSONDecodeError, ValueError):
        pass

    # --- Plain text fallback ---
    # Lane error (high priority)
    m = LANE_ERROR_RE.search(s)
    if m:
        return "error", m.group(1)[:200]

    if USER_RE.search(s):
        m2 = USER_RE.search(s)
        return "user", (m2.group(1) if m2 else s[:80])

    # Inbound message shorthand
    if INBOUND_RE.search(s) or re.search(r'[Ii]nbound.*?telegram', s):
        return "user", s[:100]

    if REPLY_RE.search(s):
        return "reply", ""

    m = TOOL_RE.search(s)
    if m:
        return "tool", m.group(1)[:60]

    if ERROR_RE.search(s):
        return "error", s[:200]

    return None, None


def tool_is_expensive(name):
    """Return True if this tool should generate a Telegram notification."""
    if not name:
        return False
    name_l = name.lower().split()[0].strip(":")
    return any(exp in name_l for exp in EXPENSIVE_TOOLS)


class Tracker:
    def __init__(self):
        self.last_activity   = time.time()
        self.last_pulse      = time.time()
        self.last_stall      = 0
        self.last_typing     = 0
        self.is_busy         = False
        self.current_task    = ""
        self.tools_announced = 0
        self.tools_total     = 0
        self.dedup_cache     = {}   # hash -> timestamp
        self.log_pos         = 0
        self.received_ack    = False  # sent "received" for current task?

    def on_user(self, text=""):
        self.last_activity   = time.time()
        self.current_task    = (text or "")[:100]
        self.is_busy         = True
        self.tools_announced = 0
        self.tools_total     = 0
        self.received_ack    = False

    def on_tool(self, name=""):
        self.last_activity = time.time()
        self.tools_total  += 1
        self.is_busy       = True
        return name

    def on_reply(self):
        self.last_activity   = time.time()
        self.is_busy         = False
        self.current_task    = ""
        self.tools_announced = 0
        self.tools_total     = 0
        self.received_ack    = False

    def should_stall_alert(self):
        if not self.is_busy:
            return False
        idle = time.time() - self.last_activity
        if idle < STALL_THRESHOLD:
            return False
        if time.time() - self.last_stall < STALL_COOLDOWN:
            return False
        return True

    def should_pulse(self):
        return self.is_busy and (time.time() - self.last_pulse > PULSE_INTERVAL)

    def should_send_typing(self):
        """Send typing action every 4s while busy."""
        return self.is_busy and (time.time() - self.last_typing > 4)

    def error_is_fresh(self, err_text):
        h = hashlib.md5(err_text.encode()).hexdigest()[:8]
        last = self.dedup_cache.get(h, 0)
        if time.time() - last > ERROR_COOLDOWN:
            self.dedup_cache[h] = time.time()
            return True
        return False


def main():
    if not TELEGRAM_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN — status agent running silently")
        while True:
            time.sleep(60)

    tracker = Tracker()

    log.info("Status agent started — waiting %ds for gateway startup...", STARTUP_DELAY)
    time.sleep(STARTUP_DELAY)

    start_msg = (
        f"Zypher online | Run #{GITHUB_RUN_ID[-6:]} | Instance #{INSTANCE_NUM}\n"
        f"Ready — send me a task.\n"
        f"(4 Cerebras keys | stall alert in {STALL_THRESHOLD}s | pulse every {PULSE_INTERVAL//60}min)"
    )
    if tg_send(start_msg):
        log.info("Startup notification sent")
    else:
        log.warning("Startup notification failed")

    log.info("Monitoring | poll=%ds stall=%ds pulse=%ds",
             POLL_INTERVAL, STALL_THRESHOLD, PULSE_INTERVAL)

    while True:
        try:
            time.sleep(POLL_INTERVAL)

            # Read new lines from gateway log
            new_lines = []
            try:
                with open(LOG_FILE, "r", errors="replace") as f:
                    f.seek(tracker.log_pos)
                    raw = f.read()
                    tracker.log_pos = f.tell()
                if raw:
                    new_lines = [l for l in raw.splitlines() if l.strip()]
            except FileNotFoundError:
                pass
            except Exception as e:
                log.debug("Log read error: %s", e)

            for line in new_lines:
                event_type, event_data = parse_line_event(line)

                if event_type == "user":
                    tracker.on_user(event_data)
                    log.info("User message: %s", (event_data or "")[:60])
                    # Immediate ack — shows user the bot received their message
                    if not tracker.received_ack:
                        task_preview = (event_data or "")[:60].strip()
                        ack = f"Got it. Working on it..."
                        if task_preview and len(task_preview) > 5:
                            ack = f"Got it. Working on: {task_preview}"
                        if tg_send(ack, silent=True):
                            tracker.received_ack = True
                            log.info("Sent task ack")

                elif event_type == "tool":
                    tool_name = tracker.on_tool(event_data)
                    log.debug("Tool: %s (total=%d)", tool_name, tracker.tools_total)
                    # Announce expensive tools
                    if tool_is_expensive(tool_name):
                        tracker.tools_announced += 1
                        tool_display = (tool_name or "tool").split()[0]
                        progress_msg = (
                            f"Running: {tool_display} "
                            f"(step {tracker.tools_announced})"
                        )
                        tg_send(progress_msg, silent=True)
                        log.info("Tool announced: %s", tool_display)

                elif event_type == "reply":
                    tracker.on_reply()
                    log.info("Reply sent")

                elif event_type == "error":
                    if tracker.error_is_fresh(event_data or ""):
                        # Shorten EmbeddedAttemptSessionTakeoverError for readability
                        err = (event_data or "")
                        if "EmbeddedAttempt" in err or "Takeover" in err:
                            err_msg = (
                                "Session lock conflict detected — "
                                "task was interrupted and will retry automatically."
                            )
                        elif "rate" in err.lower() and "limit" in err.lower():
                            err_msg = "Rate limit hit — switching to fallback model..."
                        elif "timeout" in err.lower():
                            err_msg = f"Timeout: {err[:150]}"
                        else:
                            err_msg = f"Error: {err[:200]}"
                        tg_send(f"Zypher: {err_msg}", silent=True)
                        log.warning("Error alert: %s", err[:80])

            # Send typing indicator while busy
            if tracker.should_send_typing():
                tg_typing()
                tracker.last_typing = time.time()

            # Stall detection
            if tracker.should_stall_alert():
                idle  = int(time.time() - tracker.last_activity)
                tools = tracker.tools_total
                task  = tracker.current_task or "unknown task"
                stall_msg = (
                    f"Still working... ({idle}s)\n"
                    f"Task: {task[:70]}\n"
                    f"Steps completed: {tools}"
                )
                if tg_send(stall_msg, silent=True):
                    tracker.last_stall = time.time()
                    log.info("Stall alert sent (idle %ds)", idle)

            # Activity pulse
            if tracker.should_pulse():
                tools = tracker.tools_total
                task  = tracker.current_task or "processing"
                pulse_msg = (
                    f"Working: {task[:70]}\n"
                    f"Steps: {tools} | Run #{GITHUB_RUN_ID[-6:]}"
                )
                tg_send(pulse_msg, silent=True)
                tracker.last_pulse = time.time()
                log.info("Pulse sent (steps=%d)", tools)

        except KeyboardInterrupt:
            log.info("Status agent shutting down")
            break
        except Exception as e:
            log.error("Loop error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
