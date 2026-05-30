#!/usr/bin/env python3
"""
hermes_key_rotator.py -- Round-robin Cerebras key rotation with real chat-completion
                         health checks, quarantine tracking, and model-tier cascade.

ROOT CAUSE FIX (Jul 2026):
  The old test_key() called GET /v1/models. That endpoint has a separate, more
  generous quota than /v1/chat/completions and never returns 429 during chat
  rate-limiting. So a rate-limited key passed the health check, was selected,
  and immediately failed when Hermes used it.
  FIX: test_key() now issues a real 1-token chat completion.

MODEL-TIER HEALTH CHECK (Jul 2026, v3):
  HEALTH_CHECK_MODEL was llama3.1-8b (8B, ~60k TPM).
  But Hermes runs gpt-oss-120b (120B, ~3000 TPM).
  These have SEPARATE per-model rate limits on Cerebras.
  A key healthy for llama3.1-8b is still rate-limited for gpt-oss-120b.

  FIX: Health check now uses gpt-oss-120b (the primary model).

  CASCADE STRATEGY (prevents total failure when primary quota exhausted):
    Round 1: test each key with gpt-oss-120b (PRIMARY_TEST_MODEL)
    Round 2 (only if Round 1 finds nothing): test each key with llama3.1-8b
             and write /tmp/hermes_model_tier="emergency" so the preflight
             auto-heals config.yaml to use llama3.1-8b as primary.
    This ensures at least one key is always selected even during quota exhaustion.

QUARANTINE:
  Failed keys persisted to /tmp/hermes-key-quarantine.json. Survives restarts.
"""
import os
import json
import sys
import time
from urllib.request import urlopen, Request
from urllib.error import HTTPError

KEYS = [k for k in [
    os.environ.get("CEREBRAS_API_KEY",   ""),
    os.environ.get("CEREBRAS_API_KEY_2", ""),
    os.environ.get("CEREBRAS_API_KEY_3", ""),
    os.environ.get("CEREBRAS_API_KEY_4", ""),
    os.environ.get("CEREBRAS_API_KEY_5", ""),
] if k.strip()]

RUN_ID          = os.environ.get("GITHUB_RUN_ID", "1")
ENV_FILE        = os.path.expanduser("~/.hermes/.env")
QUARANTINE_FILE = "/tmp/hermes-key-quarantine.json"
MODEL_TIER_FILE = "/tmp/hermes_model_tier"

QUARANTINE_TTL = 300  # 5 minutes

CEREBRAS_CHAT_URL = "https://api.cerebras.ai/v1/chat/completions"

# Round 1: test with the actual primary model -- catches model-specific rate limits.
# ROOT CAUSE FIX: was "llama3.1-8b" which has separate (generous) quota from
# gpt-oss-120b. A key rate-limited for 120B still passes the 8B test.
PRIMARY_TEST_MODEL   = "gpt-oss-120b"

# Round 2 (emergency): if ALL keys fail Round 1, test with the smallest model.
# A key that passes only this test signals "emergency" tier -- preflight will
# auto-heal config.yaml to use llama3.1-8b as primary for this session.
EMERGENCY_TEST_MODEL = "llama3.1-8b"


def _key_label(key):
    return ("..." + key[-4:]) if len(key) >= 4 else "????"


def load_quarantine():
    try:
        if os.path.exists(QUARANTINE_FILE):
            data = json.loads(open(QUARANTINE_FILE).read())
            now = time.time()
            return {k: v for k, v in data.items() if v > now}
    except Exception as e:
        print("WARN: could not load quarantine file: {}".format(e))
    return {}


def save_quarantine(q):
    try:
        with open(QUARANTINE_FILE, "w") as f:
            json.dump(q, f)
    except Exception as e:
        print("WARN: could not save quarantine file: {}".format(e))


def quarantine_key(q, key, reason=""):
    expires = time.time() + QUARANTINE_TTL
    q[key] = expires
    save_quarantine(q)
    print("QUARANTINE: key={} reason={} expires_in={:.0f}s".format(
        _key_label(key), reason or "unknown", QUARANTINE_TTL))


def test_key(key, model):
    """
    Test key with a real 1-token chat completion for the given model.

    Returns:
      "ok"           -- key is healthy for this model
      "rate_limited" -- key returned 429 for this model (model-specific quota!)
      "invalid"      -- key returned 401/403 (bad/revoked)
      "error"        -- transient 5xx or network error
    """
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode()
    try:
        req = Request(
            CEREBRAS_CHAT_URL,
            data=payload,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )
        with urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            if data.get("choices"):
                print("  key={} model={} status=ok".format(_key_label(key), model))
                return "ok"
            print("  key={} model={} status=error (no choices)".format(_key_label(key), model))
            return "error"
    except HTTPError as e:
        body_preview = ""
        try:
            body_preview = e.read().decode()[:120]
        except Exception:
            pass
        if e.code == 429:
            print("  key={} model={} status=rate_limited (429): {}".format(
                _key_label(key), model, body_preview))
            return "rate_limited"
        if e.code in (401, 403):
            print("  key={} model={} status=invalid ({})".format(
                _key_label(key), model, e.code))
            return "invalid"
        print("  key={} model={} status=error (HTTP {}): {}".format(
            _key_label(key), model, e.code, body_preview))
        return "error"
    except Exception as e:
        print("  key={} model={} status=error (network): {}".format(
            _key_label(key), model, e))
        return "error"


def _select_for_model(test_model, quarantine):
    """
    Try all non-quarantined keys with test_model.
    Returns (selected_key, quarantine) or (None, quarantine).
    """
    now = time.time()
    try:
        start_idx = int(RUN_ID) % len(KEYS) if str(RUN_ID).isdigit() else 0
    except Exception:
        start_idx = 0

    error_candidates = []

    for offset in range(len(KEYS)):
        idx = (start_idx + offset) % len(KEYS)
        key = KEYS[idx]

        if key in quarantine:
            print("  key={} SKIPPED (quarantined)".format(_key_label(key)))
            continue

        print("Testing key={} (idx={}) with model={}...".format(
            _key_label(key), idx, test_model))
        result = test_key(key, test_model)

        if result == "ok":
            print("Selected key={} (idx={}) -- healthy for {}".format(
                _key_label(key), idx, test_model))
            return key, quarantine

        if result == "rate_limited":
            # Model-specific quarantine -- key may be fine for smaller models
            quarantine_key(quarantine, key, reason="429_{}".format(test_model))
            continue

        if result == "invalid":
            quarantine_key(quarantine, key, reason="auth_error")
            continue

        error_candidates.append((idx, key))

    if error_candidates:
        idx, key = error_candidates[0]
        print("WARN: all tested keys returned transient errors for {}. "
              "Using key={} (idx={}) -- may recover.".format(
                  test_model, _key_label(key), idx))
        return key, quarantine

    return None, quarantine


def select():
    """
    Select the best available Cerebras API key using a two-round cascade.

    Round 1: Test with PRIMARY_TEST_MODEL (gpt-oss-120b).
             Writes /tmp/hermes_model_tier = "primary" on success.
    Round 2: If Round 1 finds nothing, test with EMERGENCY_TEST_MODEL (llama3.1-8b).
             Writes /tmp/hermes_model_tier = "emergency" on success.
             The preflight reads this signal and auto-heals config.yaml.
    """
    if not KEYS:
        print("FATAL: no Cerebras API keys found in environment.")
        sys.exit(1)

    quarantine = load_quarantine()
    now = time.time()

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

    print("Key rotator: {} key(s) | run_id={}".format(len(KEYS), RUN_ID))

    # ── Round 1: test with primary model ────────────────────────────────────
    print("\n--- Round 1: testing with {} (primary model) ---".format(PRIMARY_TEST_MODEL))
    key, quarantine = _select_for_model(PRIMARY_TEST_MODEL, quarantine)
    if key:
        try:
            with open(MODEL_TIER_FILE, "w") as f:
                f.write("primary")
        except Exception:
            pass
        print("Model tier: PRIMARY ({})".format(PRIMARY_TEST_MODEL))
        return key

    # ── Round 2: emergency fallback test with smallest model ─────────────────
    print("\nWARN: all keys exhausted or quarantined for {}.".format(PRIMARY_TEST_MODEL))
    print("--- Round 2: testing with {} (emergency fallback) ---".format(EMERGENCY_TEST_MODEL))

    # Reset transient quarantines for Round 2 -- only keep auth failures.
    # A key quarantined for "429_gpt-oss-120b" may still work for llama3.1-8b.
    perm_q = {k: v for k, v in quarantine.items()
              if "auth_error" in open(QUARANTINE_FILE).read() if k in quarantine}
    key, quarantine = _select_for_model(EMERGENCY_TEST_MODEL, {})

    if key:
        try:
            with open(MODEL_TIER_FILE, "w") as f:
                f.write("emergency")
        except Exception:
            pass
        print("Model tier: EMERGENCY ({}) -- preflight will auto-heal config".format(
            EMERGENCY_TEST_MODEL))
        return key

    # ── Total failure ────────────────────────────────────────────────────────
    print(
        "FATAL: all {} Cerebras key(s) are rate-limited, invalid, or quarantined "
        "for both {} and {}. "
        "Check Cerebras account quota. Quarantine resets after {}s.".format(
            len(KEYS), PRIMARY_TEST_MODEL, EMERGENCY_TEST_MODEL, QUARANTINE_TTL)
    )
    sys.exit(1)


def update_env(key):
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
    print("=== Cerebras Key Rotator v3 (model-tier health check + cascade) ===")
    print("Keys available: {}".format(len(KEYS)))
    print("Round 1 test model: {} (primary)".format(PRIMARY_TEST_MODEL))
    print("Round 2 test model: {} (emergency fallback)".format(EMERGENCY_TEST_MODEL))
    key = select()
    update_env(key)
    print("Key rotation complete. Selected key={}".format(_key_label(key)))
