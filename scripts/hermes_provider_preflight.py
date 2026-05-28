#!/usr/bin/env python3
"""hermes_provider_preflight.py — Cerebras API + proxy validation.

PRIMARY:  gpt-oss-120b (120B, 3000 TPM, pure OpenAI-compatible, no thinking mode)
FALLBACK: zai-glm-4.7  (355B, thinking disabled by proxy sanitizer)

Changed from zai-glm-4.7 primary because it returned HTTP 502 under agent load:
  - 355B model with Interleaved Thinking enabled by default
  - Long agent conversations exhaust ~1000 TPM throughput -> Cerebras returns 502
  - gpt-oss-120b has 3000 TPM and is used in Cerebras's own agent examples
"""
import os, json, sys, time
from urllib.request import urlopen, Request
from urllib.error import HTTPError

KEY           = os.environ.get("CEREBRAS_API_KEY", "")
PROXY_PORT    = int(os.environ.get("KEY_PROXY_PORT", "7860"))
PROXY_BASE    = f"http://127.0.0.1:{PROXY_PORT}/v1"
CEREBRAS_BASE = "https://api.cerebras.ai/v1"

# gpt-oss-120b first — primary model, no thinking mode, 3000 TPM
PREFLIGHT_MODELS = [
    "gpt-oss-120b",
    "zai-glm-4.7",
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
        print(f"WARN: direct model list HTTP {e.code}: {e.read().decode()[:100]}")
        return []
    except Exception as e:
        print(f"WARN: direct model list error: {e}")
        return []


def test_chat_direct(model):
    if not KEY:
        return False
    # For zai-glm-4.7, disable thinking so we get a standard response
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    if model == "zai-glm-4.7":
        payload["thinking"] = {"type": "disabled"}
    try:
        req = Request(
            f"{CEREBRAS_BASE}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
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
            print(f"Proxy health OK — models: {', '.join(models[:4])}")
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
            headers={"Authorization": "Bearer proxy-placeholder", "Content-Type": "application/json"},
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
    print("Hermes preflight — gpt-oss-120b (primary) / zai-glm-4.7 (fallback)")
    print("=" * 60)

    print("\n[1/2] Direct Cerebras API check...")
    models = list_models_direct()
    direct_ok = False
    for m in PREFLIGHT_MODELS:
        if not models or m in models:
            if test_chat_direct(m):
                direct_ok = True
                break
    if not direct_ok and models:
        for m in models[:3]:
            if test_chat_direct(m):
                direct_ok = True
                break
    print("Direct API:", "PASSED" if direct_ok else "WARN (may be IP-restricted on GitHub Actions)")

    print("\n[2/2] Key-rotation proxy check (critical path for Hermes)...")
    proxy_alive = False
    for attempt in range(3):
        if test_proxy_health():
            proxy_alive = True
            break
        print(f"  Proxy not ready (attempt {attempt+1}/3)...")
        time.sleep(3)

    if not proxy_alive:
        print("WARN: Proxy is not running — Hermes will fail.")
    else:
        proxy_ok = False
        for m in PREFLIGHT_MODELS:
            if test_chat_via_proxy(m):
                proxy_ok = True
                break
        print("Proxy:", "PASSED — end-to-end auth working" if proxy_ok else "WARN: chat completions failed")

    print("\n" + "=" * 60)
    print("Preflight complete.")
    print("=" * 60)


if __name__ == "__main__":
    check()
