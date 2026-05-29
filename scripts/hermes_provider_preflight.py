#!/usr/bin/env python3
"""hermes_provider_preflight.py — Cerebras API + proxy validation.

PRIMARY:  gpt-oss-120b (120B, 3000 TPM, pure OpenAI-compatible, no thinking mode)
FALLBACK: zai-glm-4.7  (355B, thinking disabled by proxy sanitizer)

Changed from zai-glm-4.7 primary because it returned HTTP 502 under agent load:
  - 355B model with Interleaved Thinking enabled by default
  - Long agent conversations exhaust ~1000 TPM throughput -> Cerebras returns 502
  - gpt-oss-120b has 3000 TPM and is used in Cerebras's own agent examples

AUTO-HEAL: If the configured primary model returns HTTP 404 (model deprecated/removed),
this script detects that and patches ~/.hermes/config.yaml with a working model
BEFORE the gateway starts. This prevents "model provider failed after retries" errors
that would otherwise persist for the entire 4-hour session.
"""
import os, json, sys, time, re
from urllib.request import urlopen, Request
from urllib.error import HTTPError

KEY           = os.environ.get("CEREBRAS_API_KEY", "")
PROXY_PORT    = int(os.environ.get("KEY_PROXY_PORT", "7860"))
PROXY_BASE    = f"http://127.0.0.1:{PROXY_PORT}/v1"
CEREBRAS_BASE = "https://api.cerebras.ai/v1"
CONFIG_PATH   = os.path.expanduser("~/.hermes/config.yaml")

# Priority-ordered model list to try if the primary fails.
# Fast/cheap models first; thinking models last (proxy disables thinking).
PREFLIGHT_MODELS = [
    "gpt-oss-120b",           # primary: 120B, 3000 TPM, pure OpenAI-compat
    "llama3.3-70b",           # reliable fallback, well-tested
    "llama-4-scout-17b-16e-instruct",   # Llama 4 Scout (fast)
    "llama-4-maverick-17b-128e-instruct",  # Llama 4 Maverick
    "qwen-3-32b",             # Qwen 3 32B
    "deepseek-r1-distill-llama-70b",  # DeepSeek R1 distill
    "llama3.1-8b",            # small but always available
    "zai-glm-4.7",            # last resort: thinking model (proxy disables thinking)
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
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    # For thinking models, disable thinking so we get a standard response
    if "zai-glm" in model or "deepseek-r1" in model or "qwq" in model:
        payload["thinking"] = {"type": "disabled"}
    try:
        req = Request(
            f"{CEREBRAS_BASE}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        )
        with urlopen(req, timeout=20) as r:
            reply = json.loads(r.read())["choices"][0]["message"]["content"]
            print(f"  Direct chat OK [{model}]: {repr(str(reply or '')[:30])}")
            return True
    except HTTPError as e:
        body = e.read().decode()[:120]
        print(f"  WARN: direct chat [{model}]: HTTP {e.code} {body}")
        return False
    except Exception as e:
        print(f"  WARN: direct chat [{model}]: {e}")
        return False


def patch_config_model(model: str):
    """Patch ~/.hermes/config.yaml to use a confirmed-working model.

    Replaces the 'default: <model>' line inside the model: section.
    Uses regex so we don't need PyYAML (avoids import dependency issues).
    Also writes the working model to /tmp/hermes_best_model for diagnostics.
    """
    try:
        with open(CONFIG_PATH) as f:
            content = f.read()

        # Replace 'default: <anything>' with the confirmed model
        patched = re.sub(r'(^\s*default:\s*)(\S+)', rf'\g<1>{model}', content, flags=re.MULTILINE)

        if patched == content:
            print(f"  Config already set to {model} (or 'default:' line not found)")
        else:
            with open(CONFIG_PATH, "w") as f:
                f.write(patched)
            print(f"  Config patched: model.default -> {model}")

        # Write best model for diagnostics / future reference
        with open("/tmp/hermes_best_model", "w") as f:
            f.write(model)

    except FileNotFoundError:
        print(f"  WARN: {CONFIG_PATH} not found — cannot patch model")
    except Exception as e:
        print(f"  WARN: could not patch config: {e}")


def test_proxy_health():
    try:
        req = Request(
            f"{PROXY_BASE}/models",
            headers={"Authorization": "Bearer proxy-placeholder"},
        )
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            models = [m["id"] for m in data.get("data", [])]
            print(f"  Proxy health OK — models: {', '.join(models[:4])}")
            return True
    except Exception as e:
        print(f"  WARN: proxy health check failed: {e}")
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
            print(f"  Proxy chat OK [{model}]: {repr(str(reply or '')[:30])}")
            return True
    except HTTPError as e:
        print(f"  WARN: proxy chat [{model}]: HTTP {e.code} {e.read().decode()[:80]}")
        return False
    except Exception as e:
        print(f"  WARN: proxy chat [{model}]: {e}")
        return False


def check():
    print("=" * 60)
    print("Hermes preflight — model auto-detection + config patch")
    print("=" * 60)

    # ── Phase 1: Direct API — find a confirmed-working model ──────────────────
    print("\n[1/2] Direct Cerebras API check (model auto-detection)...")
    available_models = list_models_direct()

    # Build a deduplicated test list: priority models first, then any discovered
    # models from the API that aren't already in our list
    test_candidates = list(PREFLIGHT_MODELS)
    for m in available_models:
        if m not in test_candidates:
            test_candidates.append(m)

    # If the API listed models, only test ones actually available
    if available_models:
        test_candidates = [m for m in test_candidates if m in available_models]
        # Always include the full priority list even if not listed (API might be cached)
        for m in PREFLIGHT_MODELS:
            if m not in test_candidates:
                test_candidates.insert(0, m)

    working_model = None
    for m in test_candidates:
        if test_chat_direct(m):
            working_model = m
            break

    if working_model:
        print(f"\n  Working model found: {working_model}")
        # If the working model is different from the configured primary, patch config
        if working_model != "gpt-oss-120b":
            print(f"  Primary model (gpt-oss-120b) unavailable — patching config...")
            patch_config_model(working_model)
        else:
            # Primary works — write to /tmp for diagnostics
            try:
                with open("/tmp/hermes_best_model", "w") as f:
                    f.write(working_model)
            except Exception:
                pass
        print(f"Direct API: PASSED (model={working_model})")
    else:
        print("\n  WARN: No working model found via direct API")
        print("  Direct API: WARN (may be IP-restricted on GitHub Actions)")

    # ── Phase 2: Proxy path check ─────────────────────────────────────────────
    print("\n[2/2] Key-rotation proxy check (critical path for Hermes)...")
    proxy_alive = False
    for attempt in range(3):
        if test_proxy_health():
            proxy_alive = True
            break
        print(f"  Proxy not ready (attempt {attempt+1}/3)...")
        time.sleep(3)

    if not proxy_alive:
        print("  WARN: Proxy is not running — Hermes will fail at startup.")
        print("  (Proxy starts in the next workflow step; preflight runs before it.)")
    else:
        probe_model = working_model or "gpt-oss-120b"
        proxy_ok = False
        for m in ([probe_model] + [x for x in PREFLIGHT_MODELS if x != probe_model]):
            if test_chat_via_proxy(m):
                proxy_ok = True
                # Patch config to this model if it differs from what we found above
                if not working_model or m != working_model:
                    print(f"  Proxy confirmed model: {m} — patching config...")
                    patch_config_model(m)
                break
        print("  Proxy:", "PASSED — end-to-end auth working" if proxy_ok else "WARN: chat completions failed")

    print("\n" + "=" * 60)
    print("Preflight complete.")
    if os.path.exists("/tmp/hermes_best_model"):
        with open("/tmp/hermes_best_model") as f:
            print(f"Confirmed working model: {f.read().strip()}")
    print("=" * 60)


if __name__ == "__main__":
    check()
