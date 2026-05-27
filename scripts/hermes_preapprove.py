#!/usr/bin/env python3
"""
hermes_preapprove.py — Pre-approve the bot owner in hermes pairing system.

Writes telegram-approved.json to ALL possible pairing directory locations
so Joshbond is never shown a pairing prompt, regardless of hermes install layout.

Also writes the TELEGRAM_ALLOWED_USERS env var into ~/.hermes/.env so the
gateway env-var allowlist is always populated too.
"""
import json, os, time

HOME = os.path.expanduser("~")
USER_ID = os.environ.get("TELEGRAM_USER_ID", "6317345496")
BOT_OWNER = "Joshbond"

APPROVED_DATA = {
    USER_ID: {
        "user_name": BOT_OWNER,
        "approved_at": time.time(),
    }
}

# Write to both old and new pairing directory layouts
PAIRING_DIRS = [
    os.path.join(HOME, ".hermes", "pairing"),
    os.path.join(HOME, ".hermes", "platforms", "pairing"),
]

for d in PAIRING_DIRS:
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "telegram-approved.json")
    with open(path, "w") as f:
        json.dump(APPROVED_DATA, f, indent=2)
    os.chmod(path, 0o600)
    print(f"Pre-approved {USER_ID} ({BOT_OWNER}) → {path}")

# Also patch ~/.hermes/.env to include TELEGRAM_ALLOWED_USERS
env_path = os.path.join(HOME, ".hermes", ".env")
if os.path.exists(env_path):
    lines = open(env_path).readlines()
    key = "TELEGRAM_ALLOWED_USERS"
    patched = [l for l in lines if not l.startswith(key + "=")]
    patched.append(f"{key}={USER_ID}\n")
    open(env_path, "w").writelines(patched)
    print(f"Patched ~/.hermes/.env: {key}={USER_ID}")
else:
    print("WARN: ~/.hermes/.env not found — skipping env patch")

print(f"Pre-approval complete. {USER_ID} will NOT see a pairing prompt.")
