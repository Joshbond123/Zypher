#!/usr/bin/env python3
"""
Zypher Status Agent v4 — API-First, Spam-Free
===============================================
Root cause (v3 bug): is_busy NEVER reset because REPLY_RE patterns
("sending reply", "response sent", etc.) don't appear in OpenClaw's
actual pino log output. Result: infinite spam every 90s indefinitely.

Fixes in v4:
  1. OpenClaw HTTP API: channels.status outboundAt > inboundAt = reply sent
  2. OpenClaw HTTP API: tasks.list active count for background task tracking
  3. Auto-reset is_busy after MAX_BUSY_S (10 min) — unconditional safety net
  4. MAX_STALL_ALERTS = 3 per task lifecycle — spam impossible even if API fails
  5. Conservative thresholds: STALL=180s, PULSE=600s, COOLDOWN=300s
  6. Gateway health probe before monitoring starts
  7. Removed per-tool-call announcements (noisy, caused false busy states)
  8. Broader log scanning for inbound messages + errors
"""
import os, re, json, time, logging, hashlib
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

logging.basicConfig(format="%(asctime)s [STATUS] %(message)s", level=logging.INFO)
log = logging.getLogger("status-agent")

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID          = "6317345496"
GITHUB_RUN_ID    = os.environ.get("GITHUB_RUN_ID", "?")
INSTANCE_NUM     = os.environ.get("INSTANCE_NUMBER", "1")
LOG_FILE         = "/tmp/openclaw.log"
GW_RPC_URL       = "http://127.0.0.1:18789/api/v1/admin/rpc"

POLL_INTERVAL    = 5          # seconds between main loop iterations
STALL_THRESHOLD  = 180        # 3 min before stall alert   (was 45s  — too aggressive)
STALL_COOLDOWN   = 300        # 5 min between stall alerts (was 90s  — spammed too fast)
PULSE_INTERVAL   = 600        # 10 min activity pulse      (was 120s — sent too often)
ERROR_COOLDOWN   = 120        # seconds between same-error alerts
MAX_BUSY_S       = 600        # 10 min → force idle (safety net, NEW)
MAX_STALL_ALERTS = 3          # max stall alerts per task — prevents infinite spam (NEW)
API_EVERY        = 3          # call gateway API every Nth poll (~15s)
STARTUP_DELAY    = 25         # seconds before sending startup notification
GATEWAY_WAIT_S   = 90         # max wait for gateway health check


# ─── Telegram helpers ────────────────────────────────────────────────────────

def tg_send(text, silent=False):
    """Send a Telegram message. Returns True on success."""
    if not TELEGRAM_TOKEN:
        return False
    try:
        data = json.dumps({
            "chat_id": CHAT_ID,
            "text": text[:4096],
            "disable_notification": silent,
        }).encode()
        req = Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=12) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception as e:
        log.debug("tg_send error: %s", e)
        return False


def tg_typing():
    """Send typing chat action (shows '...' indicator)."""
    if not TELEGRAM_TOKEN:
        return
    try:
        data = json.dumps({"chat_id": CHAT_ID, "action": "typing"}).encode()
        req = Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",
            data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        urlopen(req, timeout=5)
    except Exception:
        pass


# ─── OpenClaw gateway HTTP API ───────────────────────────────────────────────

def gw_rpc(method, params=None, timeout=5):
    """POST to gateway RPC. Returns (ok:bool, payload:dict|None)."""
    try:
        body = {"method": method}
        if params:
            body["params"] = params
        data = json.dumps(body).encode()
        req = Request(
            GW_RPC_URL, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
            if resp.get("ok"):
                return True, resp.get("payload") or {}
            return False, None
    except Exception as e:
        log.debug("gw_rpc(%s) error: %s", method, e)
        return False, None


def gw_health():
    """True if gateway HTTP API responds."""
    ok, _ = gw_rpc("health", timeout=3)
    return ok


def gw_channel_activity():
    """Return (inbound_ts_ms, outbound_ts_ms) for the telegram channel, or (None, None)."""
    ok, payload = gw_rpc("channels.status", {"channel": "telegram"}, timeout=6)
    if not ok or not isinstance(payload, dict):
        return None, None
    # channels.status payload contains accounts with lastActivity or similar
    # Walk the response looking for inboundAt / outboundAt timestamps
    try:
        accounts = payload.get("accounts") or payload.get("channels") or []
        if isinstance(accounts, dict):
            accounts = list(accounts.values())
        # Flatten to find any inboundAt / outboundAt
        inbound_ts = None
        outbound_ts = None
        def _walk(obj):
            nonlocal inbound_ts, outbound_ts
            if isinstance(obj, dict):
                ia = obj.get("inboundAt") or obj.get("lastInbound") or obj.get("inbound_at")
                oa = obj.get("outboundAt") or obj.get("lastOutbound") or obj.get("outbound_at")
                if isinstance(ia, (int, float)):
                    inbound_ts = max(inbound_ts or 0, ia)
                if isinstance(oa, (int, float)):
                    outbound_ts = max(outbound_ts or 0, oa)
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    _walk(v)
        _walk(payload)
        return inbound_ts, outbound_ts
    except Exception as e:
        log.debug("gw_channel_activity parse error: %s", e)
        return None, None


def gw_active_task_count():
    """Return count of queued+running background tasks, or None if unavailable."""
    ok, payload = gw_rpc("tasks.list", {"status": ["queued", "running"]}, timeout=5)
    if not ok or not isinstance(payload, dict):
        return None
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return None
    return sum(1 for t in tasks if isinstance(t, dict) and t.get("status") in ("queued", "running"))


# ─── Log parsing ─────────────────────────────────────────────────────────────

ERROR_RE = re.compile(
    r'FailoverError|LLM request timed out|lane task error|'
    r'EmbeddedAttemptSession|SessionTakeover|EFATAL|rate.?limit|'
    r'invalid slug|RuntimeDeadlock|WorkflowStall',
    re.I
)
LANE_ERR_RE  = re.compile(r'lane task error.*?error[=:]["\'s ]*([^"\']{5,})', re.I)
TEXT_RE      = re.compile(r'"(?:text|content|message)"\s*:\s*"([^"]{5,200})"')
INBOUND_KEYS = ("inbound", "telegram message", "from telegram", "user message",
                "received message", "incoming message", "dm from", "new message")


def scan_new_lines(log_pos):
    """Read new lines from the gateway log file.
    Returns (new_pos, list_of_(event_type, data) tuples).
    event_type: 'user' | 'error'
    """
    events = []
    new_pos = log_pos
    try:
        with open(LOG_FILE, "r", errors="replace") as f:
            f.seek(log_pos)
            raw = f.read()
            new_pos = f.tell()
        if not raw:
            return new_pos, events
        for line in raw.splitlines():
            s = line.strip()
            if not s or len(s) < 8:
                continue

            # ── JSON structured log ──
            try:
                obj = json.loads(s)
                msg   = str(obj.get("msg") or obj.get("message") or "")
                level = str(obj.get("level") or obj.get("severity") or "")
                msg_l = msg.lower()

                # Inbound user message
                if any(k in msg_l for k in INBOUND_KEYS):
                    text = (obj.get("text") or obj.get("content") or
                            obj.get("message") or msg or "")[:120]
                    events.append(("user", text))
                    continue

                # Error/fatal level
                if level in ("50", "60", "error", "fatal"):
                    err = msg or str(obj.get("err") or "")
                    if err and ERROR_RE.search(err):
                        events.append(("error", err[:200]))
                    continue

                # Lane task errors embedded in any level
                m = LANE_ERR_RE.search(s)
                if m:
                    events.append(("error", m.group(1)[:200]))
                    continue

            except (json.JSONDecodeError, ValueError):
                pass

            # ── Plain text fallback ──
            s_l = s.lower()
            if any(k in s_l for k in INBOUND_KEYS):
                m = TEXT_RE.search(s)
                events.append(("user", m.group(1)[:120] if m else s[:100]))
            elif ERROR_RE.search(s):
                m = LANE_ERR_RE.search(s)
                events.append(("error", m.group(1)[:200] if m else s[:200]))

    except FileNotFoundError:
        pass
    except Exception as e:
        log.debug("Log read error: %s", e)
    return new_pos, events


# ─── Task state tracker ───────────────────────────────────────────────────────

class Tracker:
    def __init__(self):
        self.log_pos           = 0
        self.is_busy           = False
        self.busy_since        = 0.0    # epoch when became busy
        self.became_busy_at_ms = 0      # ms timestamp (for API comparison)
        self.current_task      = ""
        self.last_activity     = time.time()
        self.last_stall        = 0.0
        self.last_pulse        = time.time()
        self.last_typing       = 0.0
        self.stall_count       = 0      # stall alerts sent this task cycle
        self.received_ack      = False
        self.dedup_cache       = {}     # hash -> last_sent_ts
        self.api_ok            = False  # gateway API responding?
        self.api_outbound_used = False  # have we confirmed reply via API?

    # ── State transitions ──

    def set_busy(self, task=""):
        if not self.is_busy:
            self.busy_since        = time.time()
            self.became_busy_at_ms = int(time.time() * 1000)
            self.stall_count       = 0
            self.api_outbound_used = False
        self.last_activity = time.time()
        self.is_busy       = True
        if task and not self.current_task:
            self.current_task = task[:100]

    def set_idle(self, reason=""):
        if self.is_busy:
            elapsed = int(time.time() - self.busy_since)
            log.info("→ idle (%s, was busy %ds)", reason or "task done", elapsed)
        self.is_busy           = False
        self.busy_since        = 0.0
        self.became_busy_at_ms = 0
        self.current_task      = ""
        self.stall_count       = 0
        self.received_ack      = False
        self.last_activity     = time.time()

    # ── Trigger checks ──

    def should_stall(self):
        if not self.is_busy:
            return False
        if self.stall_count >= MAX_STALL_ALERTS:
            return False
        if time.time() - self.last_activity < STALL_THRESHOLD:
            return False
        if time.time() - self.last_stall < STALL_COOLDOWN:
            return False
        return True

    def should_auto_reset(self):
        """Hard safety net: if busy > MAX_BUSY_S, force idle."""
        return self.is_busy and self.busy_since > 0 and (time.time() - self.busy_since > MAX_BUSY_S)

    def should_pulse(self):
        return self.is_busy and (time.time() - self.last_pulse > PULSE_INTERVAL)

    def should_typing(self):
        return self.is_busy and (time.time() - self.last_typing > 4)

    def error_is_fresh(self, text):
        h = hashlib.md5(text.encode()).hexdigest()[:8]
        last = self.dedup_cache.get(h, 0)
        if time.time() - last > ERROR_COOLDOWN:
            self.dedup_cache[h] = time.time()
            return True
        return False


# ─── Main loop ───────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN — status agent running silently")
        while True:
            time.sleep(60)

    tracker = Tracker()

    log.info("Status agent v4 started — waiting %ds for gateway...", STARTUP_DELAY)
    time.sleep(STARTUP_DELAY)

    # Wait up to GATEWAY_WAIT_S for gateway to become healthy
    waited = 0
    while waited < GATEWAY_WAIT_S:
        if gw_health():
            tracker.api_ok = True
            log.info("Gateway API healthy (waited %ds)", waited)
            break
        time.sleep(5)
        waited += 5
    else:
        log.warning("Gateway did not respond in %ds — log-only mode", GATEWAY_WAIT_S)

    # Startup notification
    start_msg = (
        f"\U0001f7e2 Zypher online | Run #{GITHUB_RUN_ID[-6:]} | Instance #{INSTANCE_NUM}\n"
        f"Ready — send me a task."
    )
    if tg_send(start_msg):
        log.info("Startup notification sent")
    else:
        log.warning("Startup notification failed")

    log.info("Monitoring | poll=%ds stall=%ds(max%d) pulse=%ds auto-reset=%ds",
             POLL_INTERVAL, STALL_THRESHOLD, MAX_STALL_ALERTS, PULSE_INTERVAL, MAX_BUSY_S)

    poll_count = 0

    while True:
        try:
            time.sleep(POLL_INTERVAL)
            poll_count += 1

            # ── 1. Scan new log lines ──────────────────────────────────────
            tracker.log_pos, events = scan_new_lines(tracker.log_pos)

            for event_type, data in events:
                if event_type == "user":
                    log.info("User message: %s", (data or "")[:60])
                    tracker.set_busy(data)
                    if not tracker.received_ack:
                        preview = (data or "").strip()[:60]
                        ack = f"Got it. Working on: {preview}" if preview and len(preview) > 8 else "Got it. Working on it..."
                        if tg_send(ack, silent=True):
                            tracker.received_ack = True

                elif event_type == "error":
                    if tracker.error_is_fresh(data or ""):
                        err = data or ""
                        if "EmbeddedAttempt" in err or "Takeover" in err:
                            msg = "Session conflict detected — retrying automatically."
                        elif "timed out" in err.lower() or "FailoverError" in err:
                            msg = "LLM timeout — trying next model in fallback chain..."
                        elif "rate" in err.lower() and "limit" in err.lower():
                            msg = "Rate limit hit — switching to fallback model..."
                        elif "invalid slug" in err.lower():
                            msg = f"Config issue: {err[:120]}"
                        else:
                            msg = f"Error: {err[:180]}"
                        tg_send(f"\u26a0\ufe0f {msg}", silent=True)
                        log.warning("Error alert sent: %s", err[:80])

            # ── 2. OpenClaw API: detect reply delivered ────────────────────
            if poll_count % API_EVERY == 0:
                ok, _ = gw_rpc("health", timeout=3)
                tracker.api_ok = ok

                if tracker.api_ok and tracker.is_busy and not tracker.api_outbound_used:
                    inbound_ts, outbound_ts = gw_channel_activity()
                    if (inbound_ts is not None and outbound_ts is not None
                            and outbound_ts > tracker.became_busy_at_ms
                            and outbound_ts > (inbound_ts - 1000)):
                        log.info("API: outboundAt(%d) > became_busy(%d) → reply delivered",
                                 outbound_ts, tracker.became_busy_at_ms)
                        tracker.api_outbound_used = True
                        tracker.set_idle("api:outbound-detected")

            # ── 3. Auto-reset safety net ────────────────────────────────────
            if tracker.should_auto_reset():
                busy_min = int((time.time() - tracker.busy_since) / 60)
                log.info("Auto-reset: busy for %dmin without completion — forcing idle", busy_min)
                tracker.set_idle("auto-reset:timeout")

            # ── 4. Typing indicator ─────────────────────────────────────────
            if tracker.should_typing():
                tg_typing()
                tracker.last_typing = time.time()

            # ── 5. Stall alert ──────────────────────────────────────────────
            if tracker.should_stall():
                idle      = int(time.time() - tracker.last_activity)
                busy_min  = int((time.time() - tracker.busy_since) / 60) if tracker.busy_since else "?"
                task_desc = tracker.current_task[:60] or "processing"
                n         = tracker.stall_count + 1

                lines = [f"\u23f3 Still working... ({busy_min}min)", f"Task: {task_desc}"]
                if n >= MAX_STALL_ALERTS:
                    lines.append(f"Alert {n}/{MAX_STALL_ALERTS} — no further stall alerts for this task.")
                else:
                    lines.append(f"Alert {n}/{MAX_STALL_ALERTS}")

                if tg_send("\n".join(lines), silent=True):
                    tracker.last_stall  = time.time()
                    tracker.stall_count = n
                    log.info("Stall alert %d/%d sent (idle %ds)", n, MAX_STALL_ALERTS, idle)

            # ── 6. Activity pulse ───────────────────────────────────────────
            if tracker.should_pulse():
                busy_min  = int((time.time() - tracker.busy_since) / 60) if tracker.busy_since else "?"
                task_desc = tracker.current_task[:60] or "processing"
                pulse_msg = (
                    f"\u25b6\ufe0f Still processing ({busy_min}min)...\n"
                    f"Task: {task_desc} | Run #{GITHUB_RUN_ID[-6:]}"
                )
                tg_send(pulse_msg, silent=True)
                tracker.last_pulse = time.time()
                log.info("Pulse sent (busy %smin)", busy_min)

        except KeyboardInterrupt:
            log.info("Status agent shutting down")
            break
        except Exception as e:
            log.error("Loop error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
