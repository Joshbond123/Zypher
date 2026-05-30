#!/usr/bin/env python3
"""
hermes_key_rotator.py -- Round-robin Cerebras key rotation with real chat-completion
                         health checks, quarantine tracking, and circuit breaker.

ROOT CAUSE FIX (Jul 2026):
  The old test_key() called GET /v1/models to check key health. This endpoint
  has a separate, more generous quota than /v1/chat/completions and does NOT
  return 429 during chat rate-limiting. So a key fully rate-limited for
  completions would still pass the health check, be selected as "healthy", and
  immediately fail when Hermes tried to use it. This was the primary cause of
  the watchdog restart loop -- all keys appeared healthy but all failed for chat.

  FIX: test_key() now issues a real 1-token llama3.1-8b chat completion request.
  A 429 response (rate-limited) is correctly detected and the key is quarantined.
  A 401/403 response marks the key as invalid/revoked.
  5xx errors are treated as transient -- key may still be usable.

QUARANTINE FILE:
  Failed keys are written to QUARANTINE_FILE (/tmp/hermes-key-quarantine.json)
  so that the quarantine survives a process restart. This prevents re-selecting
  a key that just failed during the previous session.
"""
import os
import json
import sys
import time
from urllib.request import urlopen, Request
from urllib.error import HTTPError

# Keys loaded from environment in order of priority
KEYS = [k for k in [
    os.environ.get("CEREBRAS_API_KEY",   ""),
    os.environ.get("CEREBRAS_API_KEY_2", ""),
    os.environ.get("CEREBRAS_API_KEY_3", ""),
    os.environ.get("CEREBRAS_API_KEY_4", ""),
    os.environ.get("CEREBRAS_API_KEY_5", ""),
] if k.strip()]

RUN_ID         = os.environ.get("GITHUB_RUN_ID", "1")
ENV_FILE       = os.path.expanduser("~/.hermes/.env")
QUARANTINE_FILE = "/tmp/hermes-key-quarantine.json"

# Time (seconds) to quarantine a rate-limited key. Short enough to
# allow recovery within the same run, long enough to avoid immediate retry.
QUARANTINE_TTL = 300  # 5 minutes

CEREBRAS_CHAT_URL  = "https://api.cerebras.ai/v1/chat/completions"
CEREBRAS_MODEL_URL = "https://api.cerebras.ai/v1/models"

# Use the smallest/cheapest model for health checks to minimise quota burn.
HEALTH_CHECK_MODEL = "llama3.1-8b"


def _key_label(key):
    return ("..." + key[-4:]) if len(key) >= 4 else "????"


def load_quarantine():
    """Load persisted quarantine map: {key -> expires_epoch}."""
    try:
        if os.path.exists(QUARANTINE_FILE):
            data = json.loads(open(QUARANTINE_FILE).read())
            # Remove expired entries
            now = time.time()
            active = {k: v for k, v in data.items() if v > now}
            return active
    except Exception as e:
        print("WARN: could not load quarantine file: {}".format(e))
    return {}


def save_quarantine(q):
    """Persist quarantine map to disk."""
    try:
        with open(QUARANTINE_FILE, "w") as f:
            json.dump(q, f)
    except Exception as e:
        print("WARN: could not save quarantine file: {}".format(e))


def quarantine_key(q, key, reason=""):
    """Add key to quarantine map and persist."""
    expires = time.time() + QUARANTINE_TTL
    q[key] = expires
    save_quarantine(q)
    print("QUARANTINE: key={} reason={} expires_in={:.0f}s".format(
        _key_label(key), reason or "unknown", QUARANTINE_TTL))


def test_key(key):
    """
    Test key with a real 1-token chat completion request.

    ROOT CAUSE: The old implementation called GET /v1/models. That endpoint
    has a separate quota and NEVER returns 429 during chat rate-limiting.
    So rate-limited keys passed the health check and caused downstream failures.

    Returns:
      "ok"          -- key is healthy and working
      "rate_limited" -- key returned 429 (rate-limited for chat)
      "invalid"     -- key returned 401 or 403 (bad/revoked key)
      "error"       -- transient error (5xx, network) -- key may still work
    """
    payload = json.dumps({
        "model": HEALTH_CHECK_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode()
    try:
        req = Request(
            CEREBRAS_CHAT_URL,
            data=payload,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            },
        )
        with urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            if data.get("choices"):
                print("  key={} status=ok (chat completion succeeded)".format(_key_label(key)))
                return "ok"
            print("  key={} status=error (no choices in response)".format(_key_label(key)))
            return "error"
    except HTTPError as e:
        body_preview = ""
        try:
            body_preview = e.read().decode()[:120]
        except Exception:
            pass
        if e.code == 429:
            print("  key={} status=rate_limited (429): {}".format(_key_label(key), body_preview))
            return "rate_limited"
        if e.code in (401, 403):
            print("  key={} status=invalid ({})".format(_key_label(key), e.code))
            return "invalid"
        # 5xx errors are likely transient -- don't quarantine
        print("  key={} status=error (HTTP {}): {}".format(_key_label(key), e.code, body_preview))
        return "error"
    except Exception as e:
        print("  key={} status=error (network): {}".format(_key_label(key), e))
        return "error"


def select():
    """
    Select the best available Cerebras API key.

    Strategy:
      1. Load persisted quarantine list (keys that recently failed).
      2. Compute round-robin starting index from GITHUB_RUN_ID.
      3. Starting from that index, test each non-quarantined key.
      4. Return the first key that passes the chat-completion health check.
      5. If all tests return "error" (transient), use the round-robin key
         without quarantining (transient failures shouldn't block the key).
      6. If all keys are quarantined or rate-limited, log a FATAL and exit 1.
         NEVER silently fall back to a known-bad key (old bug).
    """
    if not KEYS:
        print("FATAL: no Cerebras API keys found in environment.")
        sys.exit(1)

    quarantine = load_quarantine()
    now = time.time()

    # Compute round-robin starting index
    try:
        start_idx = int(RUN_ID) % len(KEYS) if str(RUN_ID).isdigit() else 0
    except Exception:
        start_idx = 0

    print("Key rotator: {} key(s) | run_id={} | start_idx={}".format(
        len(KEYS), RUN_ID, start_idx))

    # Remove expired quarantine entries
    active_q = {k: v for k, v in quarantine.items() if v > now}
    if len(active_q) < len(quarantine):
        save_quarantine(active_q)
        print("Quarantine: {} expired entries removed".format(
            len(quarantine) - len(active_q)))
    quarantine = active_q

    if quarantine:
        for k, exp in quarantine.items():
            print("  quarantined key={} expires_in={:.0f}s".format(
                _key_label(k), exp - now))

    # Try keys in round-robin order, skipping quarantined ones
    error_candidates = []  # keys with transient errors (may still work)
    for offset in range(len(KEYS)):
        idx = (start_idx + offset) % len(KEYS)
        key = KEYS[idx]

        if key in quarantine:
            print("  key={} SKIPPED (quarantined)".format(_key_label(key)))
            continue

        print("Testing key={} (idx={})...".format(_key_label(key), idx))
        result = test_key(key)

        if result == "ok":
            print("Selected key={} (idx={}) -- healthy".format(_key_label(key), idx))
            return key

        if result == "rate_limited":
            quarantine_key(quarantine, key, reason="429_chat")
            continue

        if result == "invalid":
            quarantine_key(quarantine, key, reason="auth_error")
            continue

        # result == "error" (transient): keep as fallback candidate
        error_candidates.append((idx, key))

    # If we have transient-error candidates, use the first one (round-robin preferred)
    if error_candidates:
        idx, key = error_candidates[0]
        print(
            "WARN: all tested keys returned transient errors. "
            "Using key={} (idx={}) as best available -- may recover.".format(
                _key_label(key), idx)
        )
        return key

    # All keys are quarantined or failed definitively
    # NEVER silently return KEYS[0] -- that was the old bug.
    print(
        "FATAL: all {} Cerebras key(s) are rate-limited, invalid, or quarantined. "
        "No healthy key available. Check your Cerebras account quota and ensure "
        "keys are valid. Quarantine resets after {}s.".format(len(KEYS), QUARANTINE_TTL)
    )
    sys.exit(1)


def update_env(key):
    """Write selected key to ~/.hermes/.env."""
    if not os.path.exists(ENV_FILE):
        return
    lines = open(ENV_FILE).readlines()
    updated = [
        "CEREBRAS_API_KEY={}\n".format(key) if l.startswith("CEREBRAS_API_KEY=") else l
        for l in lines
    ]
    if not any(l.startswith("CEREBRAS_API_KEY=") for l in lines):
        updated.append("CEREBRAS_API_KEY={}\n".format(key))
    open(ENV_FILE, "w").writelines(updated)
    print("Key updated in ~/.hermes/.env")


if __name__ == "__main__":
    print("=== Cerebras Key Rotator (v2 -- chat-completion health check) ===")
    print("Keys available: {}".format(len(KEYS)))
    key = select()
    update_env(key)
    print("Key rotation complete. Selected key={}".format(_key_label(key)))
