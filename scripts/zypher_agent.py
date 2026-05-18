#!/usr/bin/env python3
"""
Zypher Supabase Bridge
Syncs OpenClaw session state to Supabase for the frontend dashboard.
OpenClaw handles all AI/Telegram/terminal logic — this is just a bridge.
"""

import os, json, time, logging, urllib.request
from datetime import datetime, timezone

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
LOG_FILE = "/tmp/openclaw.log"

def sb(method, path, body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        })
    try:
        with urllib.request.urlopen(req, timeout=8): pass
    except Exception as e:
        logger.debug(f"sb {method} {path}: {e}")

def heartbeat():
    """Update session last-seen timestamp."""
    sb("PATCH",
       f"active_sessions?github_run_id=eq.{GITHUB_RUN_ID}",
       {"is_active": True, "updated_at": datetime.now(timezone.utc).isoformat()})

def tail_log(last_pos=0):
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
        logger.info("No Supabase credentials — running in no-op mode")
        while True:
            time.sleep(60)

    logger.info(f"Supabase bridge started — session {GITHUB_RUN_ID}")
    log_pos = 0
    tick = 0

    while True:
        time.sleep(30)
        tick += 1

        # Heartbeat every 2 minutes
        if tick % 4 == 0:
            heartbeat()
            logger.info(f"Heartbeat sent (tick {tick})")

        # Tail OpenClaw log for notable events
        lines, log_pos = tail_log(log_pos)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Log interesting events
            if any(k in line.lower() for k in
                   ["error", "warn", "telegram", "message", "cerebras",
                    "exec", "tool", "agent"]):
                logger.info(f"[openclaw] {line[:200]}")

if __name__ == "__main__":
    main()
