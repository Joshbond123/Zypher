#!/usr/bin/env python3
"""
Zypher Status Agent v2 — Dedicated Telegram Progress Reporter
=============================================================
Improvements over v1:
- Stall threshold raised 90s -> 150s (reduces false positives)
- Startup confirmation delayed 30s (gives gateway time to fully start)
- Better tool/reply detection covering more OpenClaw log patterns
- Error dedup with 180s cooldown (was 120s)
- Pulse every 15min when busy (was 10min — less noisy)
- No longer crashes if Telegram token missing (graceful no-op)
- Handles JSON log lines correctly
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
POLL_INTERVAL   = 8       # seconds between log polls
STALL_THRESHOLD = 150     # seconds with no activity before stall alert (was 90)
PULSE_INTERVAL  = 900     # 15min pulse when busy (was 600)
ERROR_COOLDOWN  = 180     # seconds between duplicate error alerts (was 120)
STARTUP_DELAY   = 30      # seconds after launch before sending startup message


def tg_send(text, silent=False):
    """Send a Telegram message. Returns True on success."""
    if not TELEGRAM_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN — Telegram notifications disabled")
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


# Regex patterns — cover both JSON-structured and plain-text log lines
TOOL_RE  = re.compile(
    r'(?:tool_call|exec|bash|web_fetch|web_search|browser|playwright|running|executing)\s*[:\s]\s*([^\n]{5,})',
    re.I
)
USER_RE  = re.compile(
    r'(?:inbound|from telegram|user message|telegram message|received message)\s*[:\s]\s*(.{5,})',
    re.I
)
REPLY_RE = re.compile(
    r'(?:sending reply|telegram send|response sent|reply sent|message sent|delivered)',
    re.I
)
ERROR_RE = re.compile(
    r'(?:\berror\b|\bERROR\b|fatal|crash|refused|timeout|rate.?limit|EFATAL|exception)',
    re.I
)
JSON_TOOL_RE = re.compile(r'"tool(?:_call|Name)"\s*:\s*"([^"]+)"', re.I)
JSON_USER_RE = re.compile(r'"role"\s*:\s*"user"', re.I)
JSON_ASST_RE = re.compile(r'"role"\s*:\s*"assistant"', re.I)


def parse_line_event(line):
    """Return event type: 'user', 'tool', 'reply', 'error', or None."""
    s = line.strip()
    if not s:
        return None, None

    # Try JSON parse first
    try:
        obj = json.loads(s)
        msg = obj.get("msg", "") or obj.get("message", "")
        role = obj.get("role", "")
        if role == "user" or "inbound" in msg.lower() or "telegram message" in msg.lower():
            return "user", (obj.get("text") or msg or "")[:100]
        if role == "assistant" or "sending reply" in msg.lower() or "response sent" in msg.lower():
            return "reply", msg[:100]
        if obj.get("tool") or "tool_call" in msg.lower():
            tool = obj.get("tool") or obj.get("toolName") or msg
            return "tool", str(tool)[:80]
        if "error" in msg.lower() or obj.get("level") in ("error", "fatal"):
            return "error", msg[:200]
        # JSON tool call pattern
        m = JSON_TOOL_RE.search(s)
        if m:
            return "tool", m.group(1)[:80]
        if JSON_USER_RE.search(s):
            return "user", msg[:100]
        if JSON_ASST_RE.search(s):
            return "reply", msg[:100]
        return None, None
    except (json.JSONDecodeError, ValueError):
        pass

    # Plain text fallback
    if USER_RE.search(s):
        m = USER_RE.search(s)
        return "user", (m.group(1) if m else "")[:100]
    if REPLY_RE.search(s):
        return "reply", ""
    m = TOOL_RE.search(s)
    if m:
        return "tool", m.group(1)[:80]
    if ERROR_RE.search(s):
        return "error", s[:200]
    return None, None


class Tracker:
    def __init__(self):
        self.last_activity = time.time()
        self.last_pulse    = time.time()
        self.last_stall    = 0
        self.is_busy       = False
        self.current_task  = ""
        self.tools         = 0
        self.dedup_cache   = {}   # hash -> timestamp for errors
        self.log_pos       = 0

    def on_user(self, text=""):
        self.last_activity = time.time()
        self.current_task  = (text or "")[:100]
        self.is_busy       = True
        self.tools         = 0

    def on_tool(self, name=""):
        self.last_activity = time.time()
        self.tools        += 1
        self.is_busy       = True

    def on_reply(self):
        self.last_activity = time.time()
        self.is_busy       = False
        self.current_task  = ""
        self.tools         = 0

    def should_stall_alert(self):
        if not self.is_busy:
            return False
        idle_secs = time.time() - self.last_activity
        if idle_secs < STALL_THRESHOLD:
            return False
        if time.time() - self.last_stall < STALL_THRESHOLD * 2:
            return False  # don't repeat stall alerts too quickly
        return True

    def should_pulse(self):
        return self.is_busy and (time.time() - self.last_pulse > PULSE_INTERVAL)

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

    # Wait for gateway to fully start before sending startup message
    log.info("Status agent started — waiting %ds for gateway startup...", STARTUP_DELAY)
    time.sleep(STARTUP_DELAY)

    # Send startup confirmation
    start_msg = (
        f"Zypher online | Run #{GITHUB_RUN_ID[-6:]} | Instance #{INSTANCE_NUM}\n"
        f"Ready — send me a task."
    )
    if tg_send(start_msg):
        log.info("Startup notification sent")
    else:
        log.warning("Startup notification failed (Telegram unreachable?)")

    log.info("Status agent monitoring | stall_threshold=%ds | pulse_interval=%ds",
             STALL_THRESHOLD, PULSE_INTERVAL)

    while True:
        try:
            time.sleep(POLL_INTERVAL)

            # Read new lines from the gateway log
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

            for line in new_lines:
                event_type, event_data = parse_line_event(line)

                if event_type == "user":
                    tracker.on_user(event_data)
                    log.info("User message detected: %s...", (event_data or "")[:50])

                elif event_type == "tool":
                    tracker.on_tool(event_data)
                    log.debug("Tool call: %s", event_data)

                elif event_type == "reply":
                    tracker.on_reply()
                    log.info("Reply sent")

                elif event_type == "error":
                    if tracker.error_is_fresh(event_data or ""):
                        err_msg = f"Zypher error detected:\n{(event_data or '')[:300]}"
                        tg_send(err_msg, silent=True)
                        log.warning("Error alert sent: %s...", (event_data or "")[:80])

            # Stall detection
            if tracker.should_stall_alert():
                idle = int(time.time() - tracker.last_activity)
                tools = tracker.tools
                task  = tracker.current_task or "unknown task"
                stall_msg = (
                    f"Zypher is working but no progress for {idle}s\n"
                    f"Task: {task[:80]}\n"
                    f"Tools called so far: {tools}\n"
                    f"Run: #{GITHUB_RUN_ID[-6:]}"
                )
                if tg_send(stall_msg, silent=True):
                    tracker.last_stall = time.time()
                    log.info("Stall alert sent (idle %ds)", idle)

            # Activity pulse
            if tracker.should_pulse():
                tools = tracker.tools
                task  = tracker.current_task or "processing"
                pulse_msg = (
                    f"Zypher working: {task[:80]}\n"
                    f"Tools used: {tools} | Run #{GITHUB_RUN_ID[-6:]}"
                )
                tg_send(pulse_msg, silent=True)
                tracker.last_pulse = time.time()
                log.info("Pulse sent (tools=%d)", tools)

        except KeyboardInterrupt:
            log.info("Status agent shutting down")
            break
        except Exception as e:
            log.error("Status agent loop error: %s", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
