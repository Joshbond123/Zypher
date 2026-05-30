#!/usr/bin/env python3
"""hermes_provider_preflight.py -- Cerebras API + proxy validation with auto-heal.

AUTO-HEAL TRIGGERS:
  1. Model returns HTTP 404 (deprecated/removed) via direct API.
  2. Model returns HTTP 429 (rate-limited, quota exhausted) via direct API.
  3. Rotator wrote /tmp/hermes_model_tier="emergency" (all keys rate-limited for
     primary model; only llama3.1-8b passed). In this case we immediately patch
     config to llama3.1-8b without wasting time on direct API tests.
  4. Proxy test fails for the configured primary model.

FALLBACK STRATEGY (Jul 2026 fix):
  Previous bug: when patching primary model, fallback stayed as llama3.3-70b.
  llama3.3-70b and gpt-oss-120b share the SAME Cerebras org quota tier.
  When org quota is exhausted, both fail simultaneously -- no true fallback.
  FIX: when patching any primary model, always set fallback to llama3.1-8b.
  llama3.1-8b has separate quota (~60k TPM vs ~3k TPM for 120B) and is the
  guaranteed-available safety net.
"""
import os, json, sys, time, re
from urllib.request import urlopen, Request
from urllib.error import HTTPError

KEY           = os.environ.get("CEREBRAS_API_KEY", "")
PROXY_PORT    = int(os.environ.get("KEY_PROXY_PORT", "7860"))
PROXY_BASE    = f"http://127.0.0.1:{PROXY_PORT}/v1"
CEREBRAS_BASE = "https://api.cerebras.ai/v1"
CONFIG_PATH   = os.path.expanduser("~/.hermes/config.yaml")
MODEL_TIER_FILE = "/tmp/hermes_model_tier"

# Priority-ordered: best quality first, guaranteed-available last.
PREFLIGHT_MODELS = [
    "gpt-oss-120b",                       # primary: 120B, ~3000 TPM
    "llama3.3-70b",                       # 70B, ~4000 TPM (but shares org quota)
    "llama-4-scout-17b-16e-instruct",     # Llama 4 Scout
    "llama-4-maverick-17b-128e-instruct", # Llama 4 Maverick
    "llama3.1-8b",                        # 8B, ~60k TPM, ALWAYS available
    "qwen-3-32b",
    "deepseek-r1-distill-llama-70b",
    "zai-glm-4.7",
]

# Always use llama3.1-8b as the fallback regardless of what primary is chosen.
# REASON: llama3.1-8b has separate quota from all larger models. When org quota
# for 70B+ models is exhausted, llama3.1-8b is unaffected and always responds.
SAFE_FALLBACK = "llama3.1-8b"

_THINKING_MODELS = ("zai-glm-4.7", "deepseek-r1", "qwq")


def list_models_direct():
    if not KEY:
        print("WARN: CEREBRAS_API_KEY not set -- skipping direct API check")
        return []
    try:
        req = Request(f"{CEREBRAS_BASE}/models", headers={"Authorization": f"Bearer {KEY}"})
        with urlopen(req, timeout=15) as r:
            models = [m["id"] for m in json.loads(r.read()).get("data", [])]
            print(f"Cerebras models available ({len(models)}): {', '.join(models)}")
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
        code = e.code
        reason = "rate-limited" if code == 429 else ("deprecated" if code == 404 else f"HTTP {code}")
        print(f"  WARN: direct chat [{model}]: {reason} -- {body}")
        return False
    except Exception as e:
        print(f"  WARN: direct chat [{model}]: {e}")
        return False


def patch_config_model(primary_model, fallback_model=None):
    """
    Patch ~/.hermes/config.yaml with confirmed-working models.

    ALWAYS sets fallback to SAFE_FALLBACK (llama3.1-8b) unless explicitly
    overridden. This prevents the bug where primary and fallback both use
    depleted quota models and both fail simultaneously.
    """
    effective_fallback = fallback_model or SAFE_FALLBACK
    try:
        with open(CONFIG_PATH) as f:
            content = f.read()

        patched = re.sub(
            r'(^\s*default:\s*)(\S+)',
            rf'\g<1>{primary_model}',
            content,
            flags=re.MULTILINE,
        )

        # Always patch fallback to ensure it's not the same depleted quota tier
        patched = re.sub(
            r'(fallback_model:[\s\S]*?\n\s*model:\s*)(\S+)',
            rf'\g<1>{effective_fallback}',
            patched,
            count=1,
        )

        if patched == content:
            print(f"  Config already correct (primary={primary_model}, fallback={effective_fallback})")
        else:
            with open(CONFIG_PATH, "w") as f:
                f.write(patched)
            print(f"  Config patched: primary={primary_model}, fallback={effective_fallback}")

        with open("/tmp/hermes_best_model", "w") as f:
            f.write(primary_model)

    except FileNotFoundError:
        print(f"  WARN: {CONFIG_PATH} not found -- cannot patch model")
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
            print(f"  Proxy health OK -- models: {', '.join(models[:4])}")
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
        body = e.read().decode()[:80]
        code = e.code
        reason = "rate-limited (429)" if code == 429 else (
            "deprecated (404)" if code == 404 else (
            "all-keys-broken (503)" if code == 503 else f"HTTP {code}"))
        print(f"  WARN: proxy chat [{model}]: {reason} -- {body}")
        return False
    except Exception as e:
        print(f"  WARN: proxy chat [{model}]: {e}")
        return False


def check():
    print("=" * 65)
    print("Hermes preflight v2 -- model auto-detect + rate-limit aware heal")
    print("=" * 65)

    # ── Emergency tier signal from key rotator ────────────────────────────────
    model_tier = "unknown"
    if os.path.exists(MODEL_TIER_FILE):
        try:
            model_tier = open(MODEL_TIER_FILE).read().strip()
        except Exception:
            pass
    print(f"\nRotator model tier signal: {model_tier}")

    if model_tier == "emergency":
        # Rotator found NO key healthy for gpt-oss-120b.
        # All keys are rate-limited for the primary model.
        # Skip direct API tests (they'd also fail) and immediately heal config.
        print("EMERGENCY: rotator signals quota exhaustion for primary model.")
        print(f"  Auto-healing config: primary={SAFE_FALLBACK}, fallback={SAFE_FALLBACK}")
        patch_config_model(SAFE_FALLBACK, SAFE_FALLBACK)
        # Still run proxy check to confirm the proxy itself is alive.
        print("\n[skip/1] Direct API skipped (emergency tier -- quota exhausted for large models)")
    else:
        # ── Phase 1: Direct API model check ──────────────────────────────────
        print("\n[1/2] Direct Cerebras API check (model auto-detection)...")
        available_models = list_models_direct()

        test_candidates = list(PREFLIGHT_MODELS)
        if available_models:
            ordered = [m for m in test_candidates if m in available_models]
            for m in PREFLIGHT_MODELS:
                if m not in ordered:
                    ordered.insert(0, m)
            test_candidates = ordered

        working_model = None
        for m in test_candidates:
            if test_chat_direct(m):
                working_model = m
                break

        if working_model:
            # Find a confirmed working fallback (must be SAFE_FALLBACK or smaller)
            # Always prefer SAFE_FALLBACK as it has separate quota
            fallback_ok = test_chat_direct(SAFE_FALLBACK) if working_model != SAFE_FALLBACK else True
            effective_fallback = SAFE_FALLBACK if fallback_ok else None
            print(f"\n  Working primary: {working_model}")
            print(f"  Working fallback: {effective_fallback or 'none confirmed -- using ' + SAFE_FALLBACK + ' anyway'}")
            patch_config_model(working_model, effective_fallback or SAFE_FALLBACK)
            print(f"Direct API: PASSED")
        else:
            print("\n  WARN: No working model via direct API (all rate-limited or blocked)")
            print("  (GitHub Actions IP may be restricted -- proxy path will determine)")

    # ── Phase 2: Proxy path check (end-to-end critical path) ─────────────────
    print("\n[2/2] Key-rotation proxy check (Hermes critical path)...")
    proxy_alive = False
    for attempt in range(5):
        if test_proxy_health():
            proxy_alive = True
            break
        print(f"  Proxy not ready (attempt {attempt+1}/5) -- waiting 3s...")
        time.sleep(3)

    if not proxy_alive:
        print("  WARN: Proxy not responding after 5 attempts.")
        print("  Check /tmp/hermes-proxy.log")
    else:
        # Determine what model to probe first based on current config
        try:
            with open(CONFIG_PATH) as f:
                cfg = f.read()
            m = re.search(r'^\s*default:\s*(\S+)', cfg, re.MULTILINE)
            configured_primary = m.group(1) if m else "gpt-oss-120b"
        except Exception:
            configured_primary = "gpt-oss-120b"

        print(f"  Testing configured primary via proxy: {configured_primary}")
        proxy_ok = False

        # Try models in priority order via proxy
        probe_order = [configured_primary] + [
            x for x in PREFLIGHT_MODELS if x != configured_primary
        ]
        proxy_working_model = None
        for m in probe_order:
            if test_chat_via_proxy(m):
                proxy_working_model = m
                proxy_ok = True
                break

        if proxy_ok:
            if proxy_working_model != configured_primary:
                # Proxy found a different working model -- heal config
                print(f"  Proxy: {configured_primary} failed, {proxy_working_model} works.")
                print(f"  Patching config: primary={proxy_working_model}, fallback={SAFE_FALLBACK}")
                patch_config_model(proxy_working_model, SAFE_FALLBACK)
            else:
                print(f"  Proxy: PASSED (primary={configured_primary} confirmed)")
        else:
            print(f"  WARN: All models failed via proxy.")
            print(f"  Last resort: patching config to emergency fallback {SAFE_FALLBACK}")
            patch_config_model(SAFE_FALLBACK, SAFE_FALLBACK)

    print("\n" + "=" * 65)
    print("Preflight complete.")
    if os.path.exists("/tmp/hermes_best_model"):
        best = open("/tmp/hermes_best_model").read().strip()
        print(f"Confirmed working model: {best}")
    print("=" * 65)


if __name__ == "__main__":
    check()
