#!/usr/bin/env python3
"""hermes_provider_preflight.py — Cerebras API + proxy validation with robust fallback."""
import os, json, sys, time
from urllib.request import urlopen, Request
from urllib.error import HTTPError

KEY          = os.environ.get("CEREBRAS_API_KEY", "")
PROXY_PORT   = int(os.environ.get("KEY_PROXY_PORT", "7860"))
PROXY_BASE   = f"http://127.0.0.1:{PROXY_PORT}/v1"
CEREBRAS_BASE = "https://api.cerebras.ai/v1"

# Ordered list of models to try (most capable first)
PREFLIGHT_MODELS = ["qwen-3-235b-a22b-instruct-2507", "llama3.3-70b", "llama3.1-8b"]


def list_models_direct():
    """List models directly from Cerebras using the API key."""
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
    """Test a chat completion directly against Cerebras."""
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
    """
    Verify the local key-rotation proxy is alive and correctly injects auth.

    The proxy accepts requests with ANY Authorization header (or none) and
    replaces it with a real Cerebras key before forwarding. A /v1/models
    request through the proxy should succeed with HTTP 200.
    """
    try:
        req = Request(
            f"{PROXY_BASE}/models",
            headers={"Authorization": "Bearer proxy-placeholder"},
        )
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            models = [m["id"] for m in data.get("data", [])]
            print(f"Proxy health OK — models via proxy: {', '.join(models[:3])}")
            return True
    except Exception as e:
        print(f"WARN: proxy health check failed: {e}")
        return False


def test_chat_via_proxy(model):
    """Test a chat completion routed through the local key-rotation proxy."""
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
    print("Hermes provider preflight")
    print("=" * 60)

    # 1. Direct Cerebras API check
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
            # Try first available model
            for m in models:
                if test_chat_direct(m):
                    direct_ok = True
                    break
    if direct_ok:
        print("Direct Cerebras API: PASSED")
    else:
        print("WARN: Direct Cerebras API check failed (may be IP-restricted on GitHub Actions)")

    # 2. Proxy health + end-to-end check
    print("\n[2/2] Key-rotation proxy check (this is the critical path for Hermes)...")

    # Wait a moment for proxy to be fully up if it's still starting
    proxy_alive = False
    for attempt in range(3):
        if test_proxy_health():
            proxy_alive = True
            break
        print(f"  Proxy not ready yet (attempt {attempt+1}/3)...")
        time.sleep(3)

    if not proxy_alive:
        print("WARN: Proxy is not running — Hermes WILL fail with HTTP 401.")
        print("      The proxy must be started before running `hermes gateway run`.")
        # Don't exit(1) — workflow has continue-on-error; the proxy start step
        # already does a hard-fail if the proxy never comes up.
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
