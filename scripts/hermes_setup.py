#!/usr/bin/env python3
"""hermes_setup.py — Write Hermes config.yaml and .env from environment variables.

ROOT-CAUSE ANALYSIS (confirmed against Hermes runtime_provider.py source):

  Call chain for primary provider (gateway → _resolve_runtime_agent_kwargs):
    resolve_runtime_provider()           ← no args (explicit_base_url=None)
    → _resolve_named_custom_runtime("custom", None, None)
       condition: requested_norm=="custom" AND explicit_base_url  ← explicit_base_url is None!
       Falls to _get_named_custom_provider("custom") → None (no entry named "custom")
       Returns None
    → _resolve_explicit_runtime(explicit_api_key=None, explicit_base_url=None)
       if not explicit_api_key and not explicit_base_url: return None
       Returns None
    → No custom/pool match → falls through to auto (openrouter placeholder) → 401

  model.base_url is SILENTLY IGNORED for provider:"custom" on primary calls.

  THE ONLY WORKING PATH is the providers: DICT (v0.12+ format):
    _get_named_custom_provider("cerebras-proxy") looks up key in providers: dict
    Found → builds runtime with base_url + api_key from the dict entry ✓

  Fallback_providers DO work with provider:"custom" + base_url because
  _try_resolve_fallback_provider passes explicit_base_url=entry.get("base_url"),
  which triggers the explicit_base_url branch in _resolve_named_custom_runtime.
"""
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

    # providers: dict (v0.12+ format) — read by _get_named_custom_provider() at runtime.
    # This is what makes the proxy base_url actually take effect.
    # model.provider must match the dict key exactly.
    cfg = (
        "# ~/.hermes/config.yaml — Zypher Agent (auto-generated)\n"
        "\n"
        "# providers: DICT (v0.12+ format) — the ONLY way to get a custom base_url\n"
        "# applied at runtime for the primary provider.\n"
        "# _get_named_custom_provider(model.provider) looks up by key in this dict.\n"
        "providers:\n"
        f"  {PROVIDER_NAME}:\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        '    api_key: "proxy-placeholder"\n'
        "\n"
        "model:\n"
        f"  provider: {PROVIDER_NAME}\n"
        f"  default: {PRIMARY_MODEL}\n"
        "  max_tokens: 8192\n"
        "  temperature: 0.7\n"
        "  streaming: true\n"
        "\n"
        "# Fallback chain — top-level key read by get_fallback_chain().\n"
        "# provider:custom + explicit base_url works here because\n"
        "# _try_resolve_fallback_provider passes explicit_base_url to\n"
        "# resolve_runtime_provider, triggering the explicit_base_url branch.\n"
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
    print(f"  providers dict  : {PROVIDER_NAME} -> {PROXY_BASE_URL}")
    print(f"  model.provider  : {PROVIDER_NAME}")
    print(f"  model.default   : {PRIMARY_MODEL}")
    print(f"  fallbacks       : {FALLBACK_MODELS[0]}, {FALLBACK_MODELS[1]} (provider:custom + explicit base_url)")


def write_env():
    ensure_dirs()
    tok    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    k1     = os.environ.get("CEREBRAS_API_KEY", "")
    tav    = os.environ.get("TAVILY_API_KEY", "") or os.environ.get("TAVILY_API_KEY_2", "")
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    gh     = os.environ.get("GITHUB_TOKEN", "")
    uid    = os.environ.get("TELEGRAM_USER_ID", "6317345496")
    # OPENAI_API_KEY: guard for any auxiliary task that might fall back
    # to an openai-compatible path. Value irrelevant — proxy injects real key.
    # OPENROUTER_API_KEY: guard against auxiliary tasks hitting openrouter.
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
    print(f"  TELEGRAM_HOME_CHANNEL : {uid}")
    print(f"  TELEGRAM_ALLOWED_USERS: {uid}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("write-config", "all"):
        write_config()
    if cmd in ("write-env", "all"):
        write_env()
    print("hermes_setup.py complete")
