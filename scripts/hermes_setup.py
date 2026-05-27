#!/usr/bin/env python3
"""hermes_setup.py — Write Hermes config.yaml and .env from environment variables.

ROOT-CAUSE ANALYSIS (confirmed against Hermes runtime_provider.py source +
official Hermes docs at hermes-agent.nousresearch.com/docs/integrations/providers):

  PROBLEM 1 — api_key vs key_env (Hermes Issue #12146): [FIXED]
    The providers: dict entry must use `key_env: ENVVAR_NAME` (an env var reference)
    not `api_key: "literal-value"`. Without key_env, Hermes cannot resolve the
    auth token and sends requests with NO Authorization header → HTTP 401
    "Missing Authentication header".

  PROBLEM 2 — missing type: openai field: [FIXED]
    Without `type: openai`, some Hermes versions fail to identify the endpoint
    as OpenAI-compatible and fall through to auto-detection, which may skip auth.

  PROBLEM 3 — fallback_providers missing auth: [FIXED]
    The fallback entries also need an auth field. Using api_key: "proxy-placeholder"
    in each fallback entry works because _try_resolve_fallback_provider passes
    explicit_api_key=entry.get("api_key") to the resolver.

  PROBLEM 4 — wrong primary model ID → HTTP 404: [FIXED]
    "llama3.3-70b" is missing a dash. The correct Cerebras ID is "llama-3.3-70b".
    Also removed "llama3.1-8b" — deprecated on Cerebras as of May 27, 2026.

  PROBLEM 5 — context compression loop at ~1,320 tokens: [FIXED]
    Without context_length set, Hermes uses a tiny internal default (~2048 tokens),
    so the 50% compression threshold fires at ~1,024 tokens. Every response triggers
    compression, which itself needs tokens → runaway loop → session reset.
    Fix: set context_length: 131072 (Cerebras supports 128K for all current models)
    and raise the compression threshold to 0.80 (fire at 80%, not 50%).

  PROBLEM 6 — auxiliary compression uses OpenRouter → 401 on compression calls:
    When the agent runs its compression LLM call, it uses provider: auto which
    falls back to OpenRouter. OpenRouter gets "proxy-placeholder" → 401.
    Fix: set auxiliary.compression.provider: main (reuse the same cerebras-proxy).

  FLOW (after all fixes):
    Hermes reads OPENAI_API_KEY="proxy-placeholder" from env
    → sends Authorization: Bearer proxy-placeholder to http://127.0.0.1:7860/v1
    → key-rotation proxy replaces header with real Cerebras key
    → proxy forwards to api.cerebras.ai/v1 with valid auth ✓
    context_length: 131072 → compression only fires at 104,858 tokens (80%)
    auxiliary compression reuses same proxy → no secondary 401 ✓
"""
import os, sys

HOME   = os.path.expanduser("~")
HD     = os.path.join(HOME, ".hermes")
MEMDIR = os.path.join(HD, "memories")
WS     = os.path.join(HD, "workspace")
SK     = os.path.join(HD, "skills")

# Key-rotation proxy (started before gateway in workflow)
PROXY_BASE_URL = "http://127.0.0.1:7860/v1"

# Cerebras model IDs — confirmed valid as of May 2026
# IMPORTANT: llama-3.3-70b uses a dash before the version number
# llama3.1-8b is DEPRECATED as of May 27, 2026 — do NOT use it
PRIMARY_MODEL   = "llama-3.3-70b"
FALLBACK_MODELS = ["qwen-3-32b", "gpt-oss-120b"]

# Context window for all current Cerebras models
CONTEXT_LENGTH = 131072   # 128K tokens — prevents premature compression

# Named entry in providers: dict — referenced by model.provider
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
        "\n"
        "# ── PROVIDER ──────────────────────────────────────────────────────────\n"
        "# key_env: Hermes reads OPENAI_API_KEY from env to build the Bearer token.\n"
        "# The proxy accepts any Bearer value and injects the real Cerebras key.\n"
        "# type: openai is required for OpenAI-compatible endpoint detection.\n"
        "providers:\n"
        f"  {PROVIDER_NAME}:\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        "    key_env: OPENAI_API_KEY\n"
        "    type: openai\n"
        "\n"
        "# ── MODEL ─────────────────────────────────────────────────────────────\n"
        "# context_length MUST be set explicitly. Without it Hermes uses a tiny\n"
        "# internal default (~2 048 tokens) and compression fires at every turn.\n"
        "# Cerebras supports 128 K context for all current production models.\n"
        "model:\n"
        f"  provider: {PROVIDER_NAME}\n"
        f"  default: {PRIMARY_MODEL}\n"
        "  max_tokens: 16384\n"
        f"  context_length: {CONTEXT_LENGTH}\n"
        "  temperature: 0.7\n"
        "  streaming: true\n"
        "\n"
        "# ── FALLBACKS ─────────────────────────────────────────────────────────\n"
        "# api_key literal works here — fallback resolver passes it as explicit_api_key.\n"
        "# llama3.1-8b intentionally omitted: deprecated on Cerebras May 27, 2026.\n"
        "fallback_providers:\n"
        '  - provider: "custom"\n'
        f"    model: {FALLBACK_MODELS[0]}\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        '    api_key: "proxy-placeholder"\n'
        f"    context_length: {CONTEXT_LENGTH}\n"
        '  - provider: "custom"\n'
        f"    model: {FALLBACK_MODELS[1]}\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        '    api_key: "proxy-placeholder"\n'
        f"    context_length: {CONTEXT_LENGTH}\n"
        "\n"
        "# ── COMPRESSION ───────────────────────────────────────────────────────\n"
        "# threshold: 0.80 — fire at 80% of context window (104 858 tokens).\n"
        "# Default 0.50 (50%) would fire at ~65 K tokens which is fine, but\n"
        "# without context_length set Hermes used a ~2 048 token default causing\n"
        "# compression to trigger at ~1 024 tokens on every single turn.\n"
        "compression:\n"
        "  enabled: true\n"
        "  threshold: 0.80\n"
        "\n"
        "# auxiliary.compression.provider: main — reuse the cerebras-proxy for\n"
        "# the summarisation LLM call. Without this, Hermes falls back to\n"
        "# provider:auto → OpenRouter with proxy-placeholder key → 401 on every\n"
        "# compression call, causing the 3-attempt loop and session reset.\n"
        "auxiliary:\n"
        "  compression:\n"
        "    provider: main\n"
        "\n"
        "# ── AGENT ─────────────────────────────────────────────────────────────\n"
        "agent:\n"
        "  name: Zypher\n"
        "  memoryFile: MEMORY.md\n"
        "  userMemoryFile: USER.md\n"
        f"  systemPromptFile: {agents_md}\n"
        f"  workspace: {WS}\n"
        f"  skillsDir: {SK}\n"
        "\n"
        "# ── GATEWAY ───────────────────────────────────────────────────────────\n"
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
        "# ── TOOLS ─────────────────────────────────────────────────────────────\n"
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
        "# ── MEMORY ────────────────────────────────────────────────────────────\n"
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
    print(f"  model.default   : {PRIMARY_MODEL}")
    print(f"  context_length  : {CONTEXT_LENGTH:,} tokens  (compression fires at {int(CONTEXT_LENGTH*0.80):,})")
    print(f"  fallbacks       : {FALLBACK_MODELS[0]}, {FALLBACK_MODELS[1]}")
    print(f"  compression.aux : provider: main  (no secondary OpenRouter 401)")


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
    print(f"  OPENAI_API_KEY          : proxy-placeholder  (Bearer token for proxy)")
    print(f"  TELEGRAM_HOME_CHANNEL   : {uid}")
    print(f"  TELEGRAM_ALLOWED_USERS  : {uid}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("write-config", "all"):
        write_config()
    if cmd in ("write-env", "all"):
        write_env()
    print("hermes_setup.py complete")
