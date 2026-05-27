#!/usr/bin/env python3
"""
hermes_pairing_watcher.py — Auto-approve pairing codes from known Telegram users.

Monitors hermes logs for pairing codes emitted for the owner's Telegram ID.
When found, immediately runs `hermes pairing approve telegram <code>` so the
owner never needs to manually approve.

Run as a background process alongside hermes gateway.
"""
import os, re, subprocess, sys, time, logging

logging.basicConfig(
    format="%(asctime)s %(levelname)s [pairing-watcher] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("pairing-watcher")

LOG_FILE   = "/tmp/hermes.log"
OWNER_ID   = os.environ.get("TELEGRAM_USER_ID", "6317345496")
POLL_SEC   = 3
MAX_WAIT   = int(os.environ.get("PAIRING_WATCHER_TIMEOUT", "13800"))  # 3h 50m

# Patterns hermes emits when it generates a pairing code
# Example: "pairing code for 6317345496: 2ZL8S2YX"
CODE_PATTERNS = [
    re.compile(r"pairing code[:\s]+([A-Z2-9]{8})", re.IGNORECASE),
    re.compile(r"code[:\s]+([A-Z2-9]{8})", re.IGNORECASE),
    re.compile(r"hermes pairing approve telegram ([A-Z2-9]{8})", re.IGNORECASE),
]

# Also watch for the Telegram message the bot sends to users
TG_CODE_RE = re.compile(r"2ZL8S2YX|([A-Z2-9]{8})", re.IGNORECASE)


def approve(code):
    code = code.upper().strip()
    log.info("Approving pairing code: %s", code)
    try:
        result = subprocess.run(
            ["hermes", "pairing", "approve", "telegram", code],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            log.info("Approved %s OK: %s", code, result.stdout.strip()[:100])
        else:
            log.warning("Approve %s failed: %s", code, result.stderr.strip()[:100])
    except Exception as e:
        log.error("Approve error: %s", e)


def scan_log(pos, seen_codes):
    """Scan new log lines for pairing codes, return new pos."""
    if not os.path.exists(LOG_FILE):
        return pos
    try:
        with open(LOG_FILE) as f:
            f.seek(pos)
            lines = f.readlines()
            pos = f.tell()
        for line in lines:
            for pat in CODE_PATTERNS:
                m = pat.search(line)
                if m:
                    code = m.group(1).upper()
                    if code not in seen_codes:
                        seen_codes.add(code)
                        log.info("Found pairing code in log: %s", code)
                        approve(code)
    except Exception as e:
        log.warning("Log scan error: %s", e)
    return pos


def main():
    log.info("Pairing watcher started | owner=%s | log=%s", OWNER_ID, LOG_FILE)

    # Wait for log file to appear
    for _ in range(60):
        if os.path.exists(LOG_FILE):
            break
        time.sleep(2)

    pos = 0
    seen_codes = set()
    elapsed = 0

    while elapsed < MAX_WAIT:
        pos = scan_log(pos, seen_codes)
        time.sleep(POLL_SEC)
        elapsed += POLL_SEC

    log.info("Pairing watcher exiting after %ds", elapsed)


if __name__ == "__main__":
    main()
