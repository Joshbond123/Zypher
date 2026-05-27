#!/usr/bin/env python3
"""hermes_setup.py — Write Hermes config.yaml and .env from environment variables."""
import os, sys

HOME   = os.path.expanduser("~")
HD     = os.path.join(HOME, ".hermes")
MEMDIR = os.path.join(HD, "memories")
WS     = os.path.join(HD, "workspace")
SK     = os.path.join(HD, "skills")

# Key-rotation proxy (started before gateway in workflow)
PROXY_BASE_URL = "http://127.0.0.1:7860/v1"

# Cerebras model IDs (validated against /v1/models)
PRIMARY_MODEL   = "qwen-3-235b-a22b-instruct-2507"
FALLBACK_MODELS = ["llama3.3-70b", "llama3.1-8b"]


def ensure_dirs():
    for d in [HD, MEMDIR, WS, SK]:
        os.makedirs(d, exist_ok=True)


def write_config():
    ensure_dirs()
    tok       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    uid       = os.environ.get("TELEGRAM_USER_ID", "6317345496")
    agents_md = os.path.join(HD, "AGENTS.md")

    # ROOT-CAUSE FIX (verified against Hermes source — May 2026):
    # ------------------------------------------------------------------
    # Old config used custom_providers:[{name:cerebras-proxy}] + provider:cerebras-proxy.
    # get_provider_profile("cerebras-proxy") returns None — that name is NOT a
    # registered Hermes plugin. custom_providers: is picker-UI only, not runtime.
    # Hermes fell back to auto->OpenRouter->401 -> bot consumed msgs, sent zero replies.
    #
    # Fix: provider:"custom" is a real registered plugin (env_vars=(), user-sets base_url).
    # base_url goes directly under model: so CustomProfile resolves it at runtime.
    # fallback_providers: is TOP-LEVEL — that is the key get_fallback_chain() reads.
    # ------------------------------------------------------------------
    cfg = (
        "# ~/.hermes/config.yaml — Zypher Agent (auto-generated)\n"
        "\n"
        "model:\n"
        '  provider: "custom"\n'
        f"  base_url: {PROXY_BASE_URL}\n"
        f"  default: {PRIMARY_MODEL}\n"
        '  api_key: "proxy-placeholder"\n'
        "  max_tokens: 8192\n"
        "  temperature: 0.7\n"
        "  streaming: true\n"
        "\n"
        "# Fallback chain — top-level key read by get_fallback_chain()\n"
        "# Each entry MUST have provider: and model: keys\n"
        "fallback_providers:\n"
        '  - provider: "custom"\n'
        f"    model: {FALLBACK_MODELS[0]}\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        '  - provider: "custom"\n'
        f"    model: {FALLBACK_MODELS[1]}\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        "\n"
        "agent:\n"
        "  name: Zypher\n"
        "  memoryFile: MEMORY.md\n"
        "  userMemoryFile: USER.md\n"
        f"  systemPromptFile: {agents_md}\n"
        f"  workspace: {WS}\n"
        f"  skillsDir: {SK}\n"
        "\n"
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
    print(f"  provider : custom (registered plugin, routes to local proxy)")
    print(f"  base_url : {PROXY_BASE_URL}")
    print(f"  model    : {PRIMARY_MODEL}")
    print(f"  fallbacks: {FALLBACK_MODELS[0]}, {FALLBACK_MODELS[1]} (via fallback_providers: top-level)")


def write_env():
    ensure_dirs()
    tok    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    k1     = os.environ.get("CEREBRAS_API_KEY", "")
    tav    = os.environ.get("TAVILY_API_KEY", "") or os.environ.get("TAVILY_API_KEY_2", "")
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    gh     = os.environ.get("GITHUB_TOKEN", "")
    uid    = os.environ.get("TELEGRAM_USER_ID", "6317345496")
    # OPENAI_API_KEY: custom provider falls back to this for auth.
    # Value irrelevant — proxy always injects the real Cerebras key.
    # OPENROUTER_API_KEY: guard against any auxiliary task using openrouter.
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
    print(f"  OPENAI_API_KEY        : proxy-placeholder")
    print(f"  OPENROUTER_API_KEY    : proxy-placeholder")
    print(f"  TELEGRAM_HOME_CHANNEL : {uid}")
    print(f"  TELEGRAM_ALLOWED_USERS: {uid}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("write-config", "all"):
        write_config()
    if cmd in ("write-env", "all"):
        write_env()
    print("hermes_setup.py complete")
