#!/usr/bin/env python3
"""hermes_provider_preflight.py — Cerebras API + proxy validation.

Model IDs confirmed against live Cerebras public API (May 2026):
  llama-3.3-70b  — non-reasoning, 131K ctx
  llama3.1-8b                     — non-reasoning, 32K ctx
  gpt-oss-120b                    — REASONING model (excluded from agent config)
  zai-glm-4.7                     — REASONING model (excluded from agent config)
"""
import os, json, sys, time
from urllib.request import urlopen, Request
from urllib.error import HTTPError

KEY          = os.environ.get("CEREBRAS_API_KEY", "")
PROXY_PORT   = int(os.environ.get("KEY_PROXY_PORT", "7860"))
PROXY_BASE   = f"http://127.0.0.1:{PROXY_PORT}/v1"
CEREBRAS_BASE = "https://api.cerebras.ai/v1"

# Confirmed non-reasoning models on Cerebras (May 2026)
# These are the only models safe to use in Hermes (no reasoning_content blowback)
PREFLIGHT_MODELS = [
    "llama-3.3-70b",
    "llama3.1-8b",
]


def list_models_direct():
    if not KEY:
        print("WARN: CEREBRAS_API_KEY not set — skipping direct API check")
        return []
    try:
        req = Request(
            f"{CEREBRAS_BASE}/models",
            headers={"Authorization": f"Bearer {KEY}"},
        )
        with urlopen(req, timeout=15) as r:
            models = [m["id"] for m in json.loads(r.read()).get("data", [])]
            print(f"Cerebras models available: {', '.join(models)}")
            return models
    except HTTPError as e:
        print(f"WARN: direct model list failed: HTTP {e.code}: {e.read().decode()[:100]}")
        return []
    except Exception as e:
        print(f"WARN: direct model list error (possible IP restriction): {e}")
        return []


def test_chat_direct(model):
    if not KEY:
        return False
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    }).encode()
    try:
        req = Request(
            f"{CEREBRAS_BASE}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {KEY}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(req, timeout=20) as r:
            reply = json.loads(r.read())["choices"][0]["message"]["content"]
            print(f"Direct chat OK [{model}]: {repr(reply[:30])}")
            return True
    except HTTPError as e:
        print(f"WARN: direct chat [{model}]: HTTP {e.code} {e.read().decode()[:80]}")
        return False
    except Exception as e:
        print(f"WARN: direct chat [{model}]: {e}")
        return False


def test_proxy_health():
    try:
        req = Request(
            f"{PROXY_BASE}/models",
            headers={"Authorization": "Bearer proxy-placeholder"},
        )
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            models = [m["id"] for m in data.get("data", [])]
            print(f"Proxy health OK — models via proxy: {', '.join(models[:4])}")
            return True
    except Exception as e:
        print(f"WARN: proxy health check failed: {e}")
        return False


def test_chat_via_proxy(model):
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    }).encode()
    try:
        req = Request(
            f"{PROXY_BASE}/chat/completions",
            data=payload,
            headers={
                "Authorization": "Bearer proxy-placeholder",
                "Content-Type": "application/json",
            },
        )
        with urlopen(req, timeout=25) as r:
            reply = json.loads(r.read())["choices"][0]["message"]["content"]
            print(f"Proxy chat OK [{model}]: {repr(reply[:30])}")
            return True
    except HTTPError as e:
        print(f"WARN: proxy chat [{model}]: HTTP {e.code} {e.read().decode()[:80]}")
        return False
    except Exception as e:
        print(f"WARN: proxy chat [{model}]: {e}")
        return False


def check():
    print("=" * 60)
    print("Hermes provider preflight (May 2026 model IDs)")
    print("=" * 60)

    print("\n[1/2] Direct Cerebras API check...")
    models = list_models_direct()
    direct_ok = False
    if models:
        for m in PREFLIGHT_MODELS:
            if m in models:
                if test_chat_direct(m):
                    direct_ok = True
                    break
        if not direct_ok:
            for m in models:
                if test_chat_direct(m):
                    direct_ok = True
                    break
    if direct_ok:
        print("Direct Cerebras API: PASSED")
    else:
        print("WARN: Direct Cerebras API check failed (may be IP-restricted on GitHub Actions)")

    print("\n[2/2] Key-rotation proxy check (critical path for Hermes)...")
    proxy_alive = False
    for attempt in range(3):
        if test_proxy_health():
            proxy_alive = True
            break
        print(f"  Proxy not ready yet (attempt {attempt+1}/3)...")
        time.sleep(3)

    if not proxy_alive:
        print("WARN: Proxy is not running — Hermes WILL fail with HTTP 401.")
    else:
        proxy_chat_ok = False
        for m in PREFLIGHT_MODELS:
            if test_chat_via_proxy(m):
                proxy_chat_ok = True
                break
        if proxy_chat_ok:
            print("Key-rotation proxy: PASSED — end-to-end auth working correctly")
        else:
            print("WARN: Proxy is up but chat completions failed (check Cerebras keys)")

    print("\n" + "=" * 60)
    print("Preflight complete.")
    print("=" * 60)


if __name__ == "__main__":
    check()
