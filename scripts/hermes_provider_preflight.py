#!/usr/bin/env python3
"""hermes_provider_preflight.py — Cerebras API + proxy validation.

PRIMARY:  gpt-oss-120b (120B, 3000 TPM, pure OpenAI-compatible, no thinking mode)
FALLBACK: llama3.3-70b  (70B, reliable, no thinking issues — replaces zai-glm-4.7)

AUTO-HEAL: If the configured primary model returns HTTP 404 (model deprecated/removed),
this script detects that and patches ~/.hermes/config.yaml with a working model
BEFORE the gateway starts. This prevents "model provider failed after retries" errors
that would otherwise persist for the entire 4-hour session.

NOTE: This script runs after the key-rotation proxy starts (workflow ordering fix).
      Previously it ran before the proxy, so the proxy health check always silently
      failed. Now it can validate the full end-to-end path including the proxy.
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
# Fast/lower-param models first for reliability; thinking models last.
PREFLIGHT_MODELS = [
    "gpt-oss-120b",                           # primary: 120B, 3000 TPM, pure OpenAI-compat
    "llama3.3-70b",                           # reliable fallback, well-tested
    "llama-4-scout-17b-16e-instruct",         # Llama 4 Scout (fast, low latency)
    "llama-4-maverick-17b-128e-instruct",     # Llama 4 Maverick
    "llama3.1-8b",                            # smallest, always available
    "qwen-3-32b",                             # Qwen 3 32B
    "deepseek-r1-distill-llama-70b",          # DeepSeek R1 distill (thinking disabled by proxy)
    "zai-glm-4.7",                            # last resort: 355B thinking model
]

# Models that need thinking:{type:disabled} in direct test calls
_THINKING_MODELS = ("zai-glm-4.7", "deepseek-r1", "qwq")


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
    # Disable thinking for thinking models so we get a standard response
    if any(tm in model for tm in _THINKING_MODELS):
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


def patch_config_model(primary_model: str, fallback_model: str = None):
    """Patch ~/.hermes/config.yaml to use a confirmed-working primary model.

    Also updates the fallback_model section if a fallback is provided.
    Uses regex so we don't need PyYAML (avoids import dependency issues).
    Writes the working model to /tmp/hermes_best_model for diagnostics.
    """
    try:
        with open(CONFIG_PATH) as f:
            content = f.read()

        patched = re.sub(
            r'(^\s*default:\s*)(\S+)',
            rf'\g<1>{primary_model}',
            content,
            flags=re.MULTILINE
        )

        if fallback_model:
            # Replace 'model: <anything>' line inside the fallback_model: block
            # We need to be careful not to replace the primary model: default line.
            # The fallback section looks like:
            #   fallback_model:
            #     provider: ...
            #     model: <fallback>
            # We target '    model: <x>' lines (indented, not 'default:' lines)
            patched = re.sub(
                r'(fallback_model:[\s\S]*?\n\s*model:\s*)(\S+)',
                rf'\g<1>{fallback_model}',
                patched,
                count=1,
            )

        if patched == content:
            print(f"  Config already uses primary={primary_model} (or pattern not found)")
        else:
            with open(CONFIG_PATH, "w") as f:
                f.write(patched)
            print(f"  Config patched: primary→{primary_model}" +
                  (f", fallback→{fallback_model}" if fallback_model else ""))

        with open("/tmp/hermes_best_model", "w") as f:
            f.write(primary_model)

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
        "stream": False,
    }).encode()
    try:
        req = Request(
            f"{PROXY_BASE}/chat/completions",
            data=payload,
            headers={"Authorization": "Bearer proxy-placeholder", "Content-Type": "application/json"},
        )
        with urlopen(req, timeout=30) as r:
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

    # Build deduplicated test list: priority models first, then discovered models
    test_candidates = list(PREFLIGHT_MODELS)
    for m in available_models:
        if m not in test_candidates:
            test_candidates.append(m)

    # Filter to only available models if the API returned a list
    if available_models:
        ordered = [m for m in test_candidates if m in available_models]
        # Ensure priority models are included even if not listed (API may be cached)
        for m in PREFLIGHT_MODELS:
            if m not in ordered:
                ordered.insert(0, m)
        test_candidates = ordered

    working_model = None
    for m in test_candidates:
        if test_chat_direct(m):
            working_model = m
            break

    # Determine the best fallback model (different from primary)
    fallback_model = None
    if working_model:
        fallback_candidates = [m for m in PREFLIGHT_MODELS if m != working_model]
        for m in fallback_candidates:
            if test_chat_direct(m):
                fallback_model = m
                break

    if working_model:
        print(f"\n  Working primary model : {working_model}")
        print(f"  Working fallback model: {fallback_model or 'none confirmed'}")
        # Patch config if needed
        patch_config_model(working_model, fallback_model)
        print(f"Direct API: PASSED (primary={working_model}, fallback={fallback_model})")
    else:
        print("\n  WARN: No working model found via direct API")
        print("  (May be IP-restricted on GitHub Actions — proxy path will confirm)")

    # ── Phase 2: Proxy path check (full end-to-end validation) ───────────────
    print("\n[2/2] Key-rotation proxy check (critical path for Hermes)...")
    proxy_alive = False
    for attempt in range(5):
        if test_proxy_health():
            proxy_alive = True
            break
        print(f"  Proxy not ready (attempt {attempt+1}/5)...")
        time.sleep(3)

    if not proxy_alive:
        print("  WARN: Proxy is not responding after 5 attempts.")
        print("  Check /tmp/hermes-proxy.log for startup errors.")
    else:
        probe_model = working_model or "gpt-oss-120b"
        proxy_ok = False
        for m in ([probe_model] + [x for x in PREFLIGHT_MODELS if x != probe_model]):
            if test_chat_via_proxy(m):
                proxy_ok = True
                if not working_model or m != working_model:
                    print(f"  Proxy confirmed model: {m} — patching config...")
                    patch_config_model(m)
                break
        print("  Proxy:", "PASSED — end-to-end OK" if proxy_ok else "WARN: chat completions failed via proxy")

    print("\n" + "=" * 60)
    print("Preflight complete.")
    if os.path.exists("/tmp/hermes_best_model"):
        with open("/tmp/hermes_best_model") as f:
            best = f.read().strip()
        print(f"Confirmed working model: {best}")
    print("=" * 60)


if __name__ == "__main__":
    check()
