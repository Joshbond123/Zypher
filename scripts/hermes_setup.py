#!/usr/bin/env python3
"""hermes_setup.py — Write Hermes config.yaml and .env from environment variables.

FULL BUG HISTORY (all issues fixed as of May 2026):

  [FIXED] PROBLEM 1 — api_key vs key_env → HTTP 401 Missing Authentication header
    providers: dict needs key_env: ENVVAR_NAME not a literal api_key value.

  [FIXED] PROBLEM 2 — missing type: openai → auth skipped in some builds

  [FIXED] PROBLEM 3 — fallback_providers missing api_key → 401 on fallbacks

  [FIXED] PROBLEM 4 — wrong primary model ID (llama3.3-70b vs llama-3.3-70b)

  [FIXED] PROBLEM 5 — no context_length → compression at ~1,024 tokens every turn

  [FIXED] PROBLEM 6 — auxiliary compression provider:main not supported

  [FIXED] PROBLEM 7 — ALL models 404 (llama-3.3-70b, qwen-3-32b removed from Cerebras)
    Confirmed via live /public/v1/models: only 4 models remain.

  [FIXED] PROBLEM 8 — HTTP 400 reasoning_content from gpt-oss-120b / zai-glm-4.7
    Both are reasoning models. Their reasoning_content gets rejected on next turn.

  [FIXED] PROBLEM 9 — Rate limit 429 on qwen-3-235b-a22b-instruct-2507:
    qwen-3-235b generates at ~1,400 tok/s. Free tier = 60K TPM.
    With max_tokens: 16384, Cerebras RESERVES 16K tokens per request against
    the TPM budget even if the actual response is 50 tokens. This burns the
    quota in < 4 seconds of steady state. Fix: set max_tokens: 4096 (75% less
    TPM pressure). The proxy also retries 429 with backoff (see hermes_key_proxy.py)
    so transient rate-limit blips never reach Hermes as a failure.

  [FIXED] PROBLEM 10 — Context compression loop at ~1,317 tokens on llama3.1-8b:
    Hermes REQUIRES at least 64K context (system prompt + tool schemas exhaust
    smaller windows). llama3.1-8b only has 32K — Hermes does not apply the
    context_length from fallback_providers entries (known issue #12977).
    Hermes falls back to an internal default of ~1,550 tokens for unrecognised
    model/endpoint combinations. Gateway hygiene fires at 85% × 1,550 = 1,317
    tokens — exactly the token count seen in the error.
    Fix: remove llama3.1-8b from fallbacks entirely. No suitable 64K+ non-
    reasoning fallback exists on free-tier Cerebras (gpt-oss-120b and zai-glm-4.7
    are reasoning models and cause HTTP 400 on message history — see Problem 8).
    The proxy handles transient rate limits with backoff retries, so qwen-3-235b
    alone is sufficient for reliable operation.

  CONFIRMED LIVE CEREBRAS MODEL CATALOG (api.cerebras.ai/public/v1/models, May 2026):
    ID                              ctx    reasoning  safe-for-hermes
    qwen-3-235b-a22b-instruct-2507  131K   no         ✅ PRIMARY
    llama3.1-8b                     32K    no         ❌ too small (<64K required)
    gpt-oss-120b                    131K   yes        ❌ reasoning_content → HTTP 400
    zai-glm-4.7                     131K   yes        ❌ reasoning_content → HTTP 400
"""
import os, sys

HOME   = os.path.expanduser("~")
HD     = os.path.join(HOME, ".hermes")
MEMDIR = os.path.join(HD, "memories")
WS     = os.path.join(HD, "workspace")
SK     = os.path.join(HD, "skills")

PROXY_BASE_URL = "http://127.0.0.1:7860/v1"

# Only non-reasoning model with ≥64K context on Cerebras free tier (May 2026).
# Rate-limit mitigation: proxy retries 429 with backoff (hermes_key_proxy.py)
# + max_tokens reduced to 4096 to lower TPM consumption per request.
PRIMARY_MODEL   = "qwen-3-235b-a22b-instruct-2507"
PRIMARY_CONTEXT = 131072

PROVIDER_NAME = "cerebras-proxy"


def ensure_dirs():
    for d in [HD, MEMDIR, WS, SK]:
        os.makedirs(d, exist_ok=True)


def write_config():
    ensure_dirs()
    tok       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    uid       = os.environ.get("TELEGRAM_USER_ID", "6317345496")
    agents_md = os.path.join(HD, "AGENTS.md")

    cfg = (
        "# ~/.hermes/config.yaml — Zypher Agent (auto-generated)\n"
        "# Model confirmed against live Cerebras API — May 2026\n"
        "\n"
        "# ── PROVIDER ───────────────────────────────────────────────────────────\n"
        "providers:\n"
        f"  {PROVIDER_NAME}:\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        "    key_env: OPENAI_API_KEY\n"
        "    type: openai\n"
        "\n"
        "# ── PRIMARY MODEL ───────────────────────────────────────────────────────\n"
        "# qwen-3-235b-a22b-instruct-2507: only non-reasoning ≥64K model on Cerebras.\n"
        "# max_tokens capped at 4096 to reduce TPM usage per request.\n"
        "# (Cerebras reserves max_tokens against TPM quota even for short replies.\n"
        "#  16384 exhausted the 60K TPM free-tier limit in under 4 seconds.)\n"
        "model:\n"
        f"  provider: {PROVIDER_NAME}\n"
        f"  default: {PRIMARY_MODEL}\n"
        "  max_tokens: 4096\n"
        f"  context_length: {PRIMARY_CONTEXT}\n"
        "  temperature: 0.7\n"
        "  streaming: true\n"
        "\n"
        "# ── FALLBACKS ───────────────────────────────────────────────────────────\n"
        "# No fallback providers. All alternatives are either:\n"
        "#   - Too small (<64K ctx): llama3.1-8b (32K) → Hermes internal default\n"
        "#     ~1550 tokens → gateway hygiene fires at 85%×1550=1317 → loop crash\n"
        "#   - Reasoning models: gpt-oss-120b, zai-glm-4.7 → HTTP 400 on next turn\n"
        "# Transient 429 rate limits are handled by the proxy retry loop instead.\n"
        "\n"
        "# ── COMPRESSION ─────────────────────────────────────────────────────────\n"
        "# Disabled at both layers:\n"
        "#   compression.enabled: false  — disables agent ContextCompressor (50%)\n"
        "#   threshold: 0.99             — belt-and-suspenders for gateway hygiene\n"
        "# qwen-3-235b has 131K context — compression is not needed in practice.\n"
        "compression:\n"
        "  enabled: false\n"
        "  threshold: 0.99\n"
        "\n"
        "# ── AGENT ───────────────────────────────────────────────────────────────\n"
        "agent:\n"
        "  name: Zypher\n"
        "  memoryFile: MEMORY.md\n"
        "  userMemoryFile: USER.md\n"
        f"  systemPromptFile: {agents_md}\n"
        f"  workspace: {WS}\n"
        f"  skillsDir: {SK}\n"
        "\n"
        "# ── GATEWAY ─────────────────────────────────────────────────────────────\n"
        "gateway:\n"
        "  platforms:\n"
        "    telegram:\n"
        "      enabled: true\n"
        f'      botToken: "{tok}"\n'
        "      dmPolicy: open\n"
        "      streamMode: block\n"
        "      pollingStallThresholdMs: 120000\n"
        "      gateway_restart_notification: true\n"
        "\n"
        "# ── TOOLS ───────────────────────────────────────────────────────────────\n"
        "tools:\n"
        "  bash:\n"
        "    enabled: true\n"
        "    timeoutSec: 300\n"
        "  web_search:\n"
        "    provider: tavily\n"
        "    enabled: true\n"
        "  web_fetch:\n"
        "    enabled: true\n"
        "    maxChars: 60000\n"
        "  browser:\n"
        "    enabled: true\n"
        "    headless: true\n"
        "    executablePath: /usr/bin/google-chrome-stable\n"
        "\n"
        "# ── MEMORY ──────────────────────────────────────────────────────────────\n"
        "memory:\n"
        "  maxCharsMemory: 8000\n"
        "  maxCharsUser: 4000\n"
        "  autoSaveIntervalSec: 60\n"
        f"  directory: {MEMDIR}\n"
        "\n"
        "skills:\n"
        "  autoLoad: true\n"
        f"  directory: {SK}\n"
    )
    p = os.path.join(HD, "config.yaml")
    open(p, "w").write(cfg)
    print(f"config.yaml written -> {p}")
    print(f"  provider        : {PROVIDER_NAME} (key_env: OPENAI_API_KEY, type: openai)")
    print(f"  primary         : {PRIMARY_MODEL}")
    print(f"  max_tokens      : 4096  (reduced from 16384 — saves 75%% TPM per request)")
    print(f"  context_length  : {PRIMARY_CONTEXT:,}")
    print(f"  fallbacks       : NONE (no safe ≥64K non-reasoning model on free Cerebras)")
    print(f"  compression     : DISABLED (threshold: 0.99 as gateway hygiene guard)")


def write_env():
    ensure_dirs()
    tok    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    k1     = os.environ.get("CEREBRAS_API_KEY", "")
    tav    = os.environ.get("TAVILY_API_KEY", "") or os.environ.get("TAVILY_API_KEY_2", "")
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    gh     = os.environ.get("GITHUB_TOKEN", "")
    uid    = os.environ.get("TELEGRAM_USER_ID", "6317345496")
    env = (
        f"CEREBRAS_API_KEY={k1}\n"
        "OPENAI_API_KEY=proxy-placeholder\n"
        "OPENROUTER_API_KEY=proxy-placeholder\n"
        f"TELEGRAM_HOME_CHANNEL={uid}\n"
        "TELEGRAM_HOME_CHANNEL_NAME=Zypher Home\n"
        f"TELEGRAM_ALLOWED_USERS={uid}\n"
        f"TELEGRAM_BOT_TOKEN={tok}\n"
        f"TAVILY_API_KEY={tav}\n"
        f"SUPABASE_URL={sb_url}\n"
        f"SUPABASE_SERVICE_KEY={sb_key}\n"
        f"GITHUB_TOKEN={gh}\n"
    )
    p = os.path.join(HD, ".env")
    open(p, "w").write(env)
    os.chmod(p, 0o600)
    print("~/.hermes/.env written")
    print(f"  OPENAI_API_KEY         : proxy-placeholder")
    print(f"  TELEGRAM_HOME_CHANNEL  : {uid}")
    print(f"  TELEGRAM_ALLOWED_USERS : {uid}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("write-config", "all"):
        write_config()
    if cmd in ("write-env", "all"):
        write_env()
    print("hermes_setup.py complete")
