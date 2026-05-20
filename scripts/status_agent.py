#!/usr/bin/env python3
"""
Zypher Status Agent v5 — Silent Monitor
=========================================
Design goal: NEVER send internal runtime noise to Telegram.
The AI agent sends its own replies. This agent only:
  - Sends typing indicator while the AI is actively working (every 30s)
  - Logs gateway health/activity internally
  - DOES NOT forward errors, stall alerts, acks, or progress spam to Telegram

Removed from v4:
  - Startup notification (send_startup_notification.py is the sole sender)
  - Error forwarding to Telegram (EmbeddedAttempt, FailoverError, lane errors, etc.)
  - "Got it. Working on..." ack messages (AI handles its own acks)
  - Stall alerts ("Still working..." / "Alert 1/3" spam)
  - Pulse messages ("Still processing...")
  - Aggressive 4s typing indicator loop (now 30s, and only while clearly busy)
"""
import os, re, json, time, logging, hashlib
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

logging.basicConfig(format="%(asctime)s [STATUS] %(message)s", level=logging.INFO)
log = logging.getLogger("status-agent")

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID         = "6317345496"
GITHUB_RUN_ID   = os.environ.get("GITHUB_RUN_ID", "?")
INSTANCE_NUM    = os.environ.get("INSTANCE_NUMBER", "1")
LOG_FILE        = "/tmp/openclaw.log"
GW_RPC_URL      = "http://127.0.0.1:18789/api/v1/admin/rpc"

POLL_INTERVAL   = 5           # seconds between main loop iterations
TYPING_INTERVAL = 30          # send typing indicator every 30s while busy (not 4s)
MAX_BUSY_S      = 900         # 15 min hard safety net — force idle
API_EVERY       = 6           # call gateway API every Nth poll (~30s)
STARTUP_DELAY   = 20          # wait before starting (give gateway time to start)
GATEWAY_WAIT_S  = 90          # max wait for gateway health check


# ─── Telegram helpers ────────────────────────────────────────────────────────

def tg_typing():
    """Send typing chat action. Silent — never raises."""
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
    try:
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


# ─── Log parsing ─────────────────────────────────────────────────────────────

INBOUND_KEYS = ("inbound", "telegram message", "from telegram", "user message",
                "received message", "incoming message", "dm from", "new message")


def scan_new_lines(log_pos):
    """Read new lines from the gateway log file.
    Returns (new_pos, list_of_event_type strings).
    event_type: 'user' only — errors are intentionally ignored (no Telegram forwarding).
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

            # JSON structured log
            try:
                obj = json.loads(s)
                msg = str(obj.get("msg") or obj.get("message") or "")
                msg_l = msg.lower()
                if any(k in msg_l for k in INBOUND_KEYS):
                    events.append("user")
                    continue
            except (json.JSONDecodeError, ValueError):
                pass

            # Plain text fallback
            s_l = s.lower()
            if any(k in s_l for k in INBOUND_KEYS):
                events.append("user")

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
        self.busy_since        = 0.0
        self.became_busy_at_ms = 0
        self.last_typing       = 0.0
        self.api_ok            = False
        self.api_outbound_used = False

    def set_busy(self):
        if not self.is_busy:
            self.busy_since        = time.time()
            self.became_busy_at_ms = int(time.time() * 1000)
            self.api_outbound_used = False
            log.info("→ busy (user message detected)")
        self.is_busy = True

    def set_idle(self, reason=""):
        if self.is_busy:
            elapsed = int(time.time() - self.busy_since)
            log.info("→ idle (%s, was busy %ds)", reason or "task done", elapsed)
        self.is_busy           = False
        self.busy_since        = 0.0
        self.became_busy_at_ms = 0

    def should_auto_reset(self):
        return self.is_busy and self.busy_since > 0 and (time.time() - self.busy_since > MAX_BUSY_S)

    def should_typing(self):
        return self.is_busy and (time.time() - self.last_typing > TYPING_INTERVAL)


# ─── Main loop ───────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN — status agent running silently")
        while True:
            time.sleep(60)

    tracker = Tracker()

    log.info("Status agent v5 started — waiting %ds for gateway...", STARTUP_DELAY)
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

    # No startup notification here — send_startup_notification.py is the sole sender.

    log.info("Monitoring | poll=%ds typing_interval=%ds auto-reset=%ds",
             POLL_INTERVAL, TYPING_INTERVAL, MAX_BUSY_S)

    poll_count = 0

    while True:
        try:
            time.sleep(POLL_INTERVAL)
            poll_count += 1

            # ── 1. Scan new log lines (only detect user messages for busy state) ──
            tracker.log_pos, events = scan_new_lines(tracker.log_pos)
            for event_type in events:
                if event_type == "user":
                    log.info("User message detected — marking busy")
                    tracker.set_busy()
                # NOTE: 'error' events are intentionally NOT forwarded to Telegram.
                # Internal runtime errors (EmbeddedAttempt, FailoverError, lane errors,
                # session lock conflicts, rate limits) are handled internally by OpenClaw
                # and must never appear in user-facing Telegram chats.

            # ── 2. OpenClaw API: detect reply delivered → mark idle ──────────
            if poll_count % API_EVERY == 0:
                ok, _ = gw_rpc("health", timeout=3)
                tracker.api_ok = ok

                if tracker.api_ok and tracker.is_busy and not tracker.api_outbound_used:
                    inbound_ts, outbound_ts = gw_channel_activity()
                    if (inbound_ts is not None and outbound_ts is not None
                            and outbound_ts > tracker.became_busy_at_ms
                            and outbound_ts > (inbound_ts - 1000)):
                        log.info("API: reply delivered (outboundAt=%d) → idle", outbound_ts)
                        tracker.api_outbound_used = True
                        tracker.set_idle("api:outbound-detected")

            # ── 3. Auto-reset safety net ─────────────────────────────────────
            if tracker.should_auto_reset():
                busy_min = int((time.time() - tracker.busy_since) / 60)
                log.info("Auto-reset: busy for %dmin — forcing idle", busy_min)
                tracker.set_idle("auto-reset:timeout")

            # ── 4. Typing indicator (conservative — every 30s) ───────────────
            # This is the only signal sent while busy. It's a standard UX indicator
            # and does not constitute internal log leakage.
            if tracker.should_typing():
                tg_typing()
                tracker.last_typing = time.time()
                log.debug("Typing indicator sent")

            # NOTE: The following have been intentionally removed and must NOT be re-added:
            #   - Stall alerts ("Still working... Alert 1/3")
            #   - Pulse messages ("Still processing...")
            #   - Error messages ("⚠️ LLM timeout", "⚠️ Session conflict", etc.)
            #   - Ack messages ("Got it. Working on...")
            # The AI agent handles all user-facing communication itself.

        except KeyboardInterrupt:
            log.info("Status agent shutting down")
            break
        except Exception as e:
            log.error("Loop error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
