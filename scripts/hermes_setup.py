#!/usr/bin/env python3
"""hermes_setup.py — Write Hermes config.yaml and .env from environment variables.

ROOT-CAUSE ANALYSIS — full bug history:

  [FIXED] PROBLEM 1 — api_key vs key_env (Hermes Issue #12146):
    providers: dict entry must use key_env: ENVVAR_NAME, not literal api_key.
    Without key_env, Hermes sends requests with no Authorization header → 401.

  [FIXED] PROBLEM 2 — missing type: openai in provider entry:
    Without type: openai, Hermes cannot identify the OpenAI-compatible format.

  [FIXED] PROBLEM 3 — fallback_providers missing auth:
    Fallbacks need api_key field so _try_resolve_fallback_provider passes it.

  [FIXED] PROBLEM 4 — wrong primary model ID (llama3.3-70b → llama-3.3-70b):
    Corrected by adding the missing dash. But llama-3.3-70b has since been
    removed from Cerebras entirely — see Problem 7.

  [FIXED] PROBLEM 5 — context compression loop at ~1,320 tokens:
    context_length not set → Hermes used a tiny internal default (~2,048 tokens).
    Compression threshold 50% of 2048 = 1,024 tokens → fired every turn.
    Fix: context_length: 131072, compression.threshold: 0.80

  [FIXED] PROBLEM 6 — auxiliary compression provider:main unavailable:
    provider: main is not supported in the installed hermes-agent version.
    Fix: disable compression entirely (131K context makes it unnecessary).

  [FIXED] PROBLEM 7 — ALL three models return HTTP 404:
    Confirmed via live Cerebras public API (api.cerebras.ai/public/v1/models).
    As of May 2026, the ONLY available Cerebras models are:
      ✅ qwen-3-235b-a22b-instruct-2507  (non-reasoning, 131K ctx, tools)
      ✅ llama3.1-8b                      (non-reasoning, 32K ctx, tools)
      ⚠️ gpt-oss-120b                     (reasoning model — causes Bug 8)
      ⚠️ zai-glm-4.7                      (reasoning model — causes Bug 8)
    llama-3.3-70b, qwen-3-32b etc. are GONE from the Cerebras lineup.

  [FIXED] PROBLEM 8 — HTTP 400 reasoning_content unsupported:
    gpt-oss-120b and zai-glm-4.7 are reasoning models. They return
    reasoning_content in their assistant messages. When Hermes feeds those
    messages back in the next turn, Cerebras rejects the whole request:
      'messages.2.assistant.reasoning_content: property is unsupported'
    Fix: only use non-reasoning models (qwen-3-235b, llama3.1-8b).

  FINAL WORKING MODEL LINEUP (May 2026, confirmed against live API):
    Primary  : qwen-3-235b-a22b-instruct-2507  (131K ctx, tool-calling, non-reasoning)
    Fallback : llama3.1-8b                      (32K ctx, tool-calling, non-reasoning)
    EXCLUDED : gpt-oss-120b, zai-glm-4.7        (reasoning models — break message history)

  AUTH FLOW:
    Hermes reads OPENAI_API_KEY="proxy-placeholder" from env (via key_env)
    → sends Authorization: Bearer proxy-placeholder to http://127.0.0.1:7860/v1
    → key-rotation proxy replaces header with real Cerebras key
    → proxy forwards to api.cerebras.ai with valid auth ✓
"""
import os, sys

HOME   = os.path.expanduser("~")
HD     = os.path.join(HOME, ".hermes")
MEMDIR = os.path.join(HD, "memories")
WS     = os.path.join(HD, "workspace")
SK     = os.path.join(HD, "skills")

PROXY_BASE_URL = "http://127.0.0.1:7860/v1"

# CONFIRMED AGAINST LIVE CEREBRAS API (api.cerebras.ai/public/v1/models, May 2026)
# ONLY non-reasoning models — reasoning models (gpt-oss-120b, zai-glm-4.7) are
# excluded because they return reasoning_content which Cerebras then rejects on
# the next turn with HTTP 400 "property is unsupported".
PRIMARY_MODEL  = "qwen-3-235b-a22b-instruct-2507"   # 131K ctx, tools, non-reasoning
FALLBACK_MODEL = "llama3.1-8b"                       # 32K ctx, tools, non-reasoning

PRIMARY_CONTEXT  = 131072   # qwen-3-235b confirmed via /public/v1/models
FALLBACK_CONTEXT = 32768    # llama3.1-8b confirmed via /public/v1/models

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
        "# Models confirmed against live Cerebras API — May 2026\n"
        "\n"
        "# ── PROVIDER ───────────────────────────────────────────────────────────\n"
        "# key_env: Hermes reads OPENAI_API_KEY from env → Authorization header.\n"
        "# The proxy accepts any Bearer value and injects the real Cerebras key.\n"
        "providers:\n"
        f"  {PROVIDER_NAME}:\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        "    key_env: OPENAI_API_KEY\n"
        "    type: openai\n"
        "\n"
        "# ── PRIMARY MODEL ───────────────────────────────────────────────────────\n"
        "# qwen-3-235b-a22b-instruct-2507:\n"
        "#   - Only full non-reasoning model with 131K context on Cerebras (May 2026)\n"
        "#   - Tool calling supported, streaming supported\n"
        "#   - reasoning: false → no reasoning_content in responses (safe for history)\n"
        "model:\n"
        f"  provider: {PROVIDER_NAME}\n"
        f"  default: {PRIMARY_MODEL}\n"
        "  max_tokens: 16384\n"
        f"  context_length: {PRIMARY_CONTEXT}\n"
        "  temperature: 0.7\n"
        "  streaming: true\n"
        "\n"
        "# ── FALLBACK ────────────────────────────────────────────────────────────\n"
        "# llama3.1-8b only — 32K context, non-reasoning, tool-calling supported.\n"
        "# gpt-oss-120b and zai-glm-4.7 intentionally excluded: both are reasoning\n"
        "# models that return reasoning_content → HTTP 400 on next request.\n"
        "fallback_providers:\n"
        '  - provider: "custom"\n'
        f"    model: {FALLBACK_MODEL}\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        '    api_key: "proxy-placeholder"\n'
        f"    context_length: {FALLBACK_CONTEXT}\n"
        "\n"
        "# ── COMPRESSION ─────────────────────────────────────────────────────────\n"
        "# Disabled: qwen-3-235b has 131K context — compression is unnecessary for\n"
        "# normal usage and the auxiliary provider support varies by hermes version.\n"
        "# Without this disabled, Hermes tries provider:main which is unsupported\n"
        "# in some builds, logs a warning, and drops turns without summarizing.\n"
        "compression:\n"
        "  enabled: false\n"
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
    print(f"  provider       : {PROVIDER_NAME} (key_env: OPENAI_API_KEY, type: openai)")
    print(f"  primary        : {PRIMARY_MODEL} ({PRIMARY_CONTEXT:,} ctx)")
    print(f"  fallback       : {FALLBACK_MODEL} ({FALLBACK_CONTEXT:,} ctx)")
    print(f"  excluded       : gpt-oss-120b, zai-glm-4.7 (reasoning models)")
    print(f"  compression    : DISABLED (131K context, no auxiliary provider needed)")


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
    print(f"  OPENAI_API_KEY         : proxy-placeholder (Bearer token for proxy)")
    print(f"  TELEGRAM_HOME_CHANNEL  : {uid}")
    print(f"  TELEGRAM_ALLOWED_USERS : {uid}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("write-config", "all"):
        write_config()
    if cmd in ("write-env", "all"):
        write_env()
    print("hermes_setup.py complete")
