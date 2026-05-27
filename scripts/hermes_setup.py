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

# Cerebras models available on free tier (validated against /v1/models)
# Primary: Qwen3 235B MoE — largest, best reasoning
# Fallbacks: proven stable Cerebras model IDs
PRIMARY_MODEL   = "qwen-3-235b-a22b-instruct-2507"
FALLBACK_MODELS = ["llama3.3-70b", "llama3.1-8b"]

# Named custom provider — used in config.yaml custom_providers block.
# This is the ONLY correct way to use a custom base_url in Hermes.
# provider: openrouter / provider: openai both ignore base_url and hit
# their hardcoded upstream endpoints (confirmed: Hermes Issue #12146).
CUSTOM_PROVIDER_NAME = "cerebras-proxy"


def ensure_dirs():
    for d in [HD, MEMDIR, WS, SK]:
        os.makedirs(d, exist_ok=True)


def write_config():
    ensure_dirs()
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    uid = os.environ.get("TELEGRAM_USER_ID", "6317345496")
    agents_md = os.path.join(HD, "AGENTS.md")

    # ROOT-CAUSE FIX (Hermes Issue #12146):
    # ─────────────────────────────────────
    # `provider: openrouter` and `provider: openai` BOTH ignore `base_url`
    # in Hermes's PROVIDER_REGISTRY — they always route to their hardcoded
    # upstream endpoints (openrouter.ai, api.openai.com). The local
    # key-rotation proxy was never being called, so requests arrived at
    # openrouter.ai with no OPENROUTER_API_KEY → HTTP 401.
    #
    # The correct approach is a NAMED custom provider in `custom_providers:`
    # with `key_env` pointing to any env var that holds a non-empty value.
    # Hermes reads `key_env` and sets Authorization: Bearer <value>.
    # Our proxy IGNORES the incoming auth and injects a real Cerebras key,
    # so `key_env` just needs to be non-empty — `OPENAI_API_KEY=proxy-placeholder`
    # serves this purpose perfectly.
    cfg = f"""# ~/.hermes/config.yaml — Zypher Agent (auto-generated)

# ── Named custom provider ── (THE FIX for Issue #12146 / 401 auth errors)
# Hermes only respects base_url when a custom_providers entry is used.
# provider: openrouter / provider: openai ignore base_url entirely.
custom_providers:
  - name: {CUSTOM_PROVIDER_NAME}
    base_url: {PROXY_BASE_URL}
    key_env: OPENAI_API_KEY

model:
  provider: {CUSTOM_PROVIDER_NAME}
  name: {PRIMARY_MODEL}
  fallbacks:
    - {FALLBACK_MODELS[0]}
    - {FALLBACK_MODELS[1]}
  max_tokens: 8192
  temperature: 0.7
  streaming: true

agent:
  name: Zypher
  memoryFile: MEMORY.md
  userMemoryFile: USER.md
  systemPromptFile: {agents_md}
  workspace: {WS}
  skillsDir: {SK}

gateway:
  platforms:
    telegram:
      enabled: true
      botToken: "{tok}"
      dmPolicy: open
      streamMode: block
      pollingStallThresholdMs: 120000
      gateway_restart_notification: true

tools:
  bash:
    enabled: true
    timeoutSec: 300
  web_search:
    provider: tavily
    enabled: true
  web_fetch:
    enabled: true
    maxChars: 60000
  browser:
    enabled: true
    headless: true
    executablePath: /usr/bin/google-chrome-stable

memory:
  maxCharsMemory: 8000
  maxCharsUser: 4000
  autoSaveIntervalSec: 60
  directory: {MEMDIR}

skills:
  autoLoad: true
  directory: {SK}
"""
    p = os.path.join(HD, "config.yaml")
    open(p, "w").write(cfg)
    print(f"config.yaml written → {p}")
    print(f"  provider: {CUSTOM_PROVIDER_NAME} (named custom provider → proxy)")
    print(f"  base_url: {PROXY_BASE_URL}")
    print(f"  model   : {PRIMARY_MODEL}")
    print(f"  fallbacks: {', '.join(FALLBACK_MODELS)}")


def write_env():
    ensure_dirs()
    tok    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    k1     = os.environ.get("CEREBRAS_API_KEY", "")
    tav    = os.environ.get("TAVILY_API_KEY", "") or os.environ.get("TAVILY_API_KEY_2", "")
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    gh     = os.environ.get("GITHUB_TOKEN", "")
    uid    = os.environ.get("TELEGRAM_USER_ID", "6317345496")

    # OPENAI_API_KEY  — referenced by key_env in the custom provider block.
    #                   The proxy ignores this value and injects a real key,
    #                   but Hermes needs a non-empty string or it won't send
    #                   any Authorization header at all.
    # OPENROUTER_API_KEY — belt-and-suspenders: if Hermes ever tries the
    #                   openrouter provider as a fallback (e.g. via provider:auto
    #                   in an auxiliary task), this prevents a second 401.
    env = (
        f"CEREBRAS_API_KEY={k1}\n"
        f"OPENAI_API_KEY=proxy-placeholder\n"
        f"OPENROUTER_API_KEY=proxy-placeholder\n"
        f"TELEGRAM_HOME_CHANNEL={uid}\n"
        f"TELEGRAM_HOME_CHANNEL_NAME=Zypher Home\n"
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
    print(f"~/.hermes/.env written")
    print(f"  OPENAI_API_KEY        : proxy-placeholder (key_env for custom provider)")
    print(f"  OPENROUTER_API_KEY    : proxy-placeholder (auxiliary task fallback guard)")
    print(f"  TELEGRAM_HOME_CHANNEL : {uid}")
    print(f"  TELEGRAM_ALLOWED_USERS: {uid}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("write-config", "all"):
        write_config()
    if cmd in ("write-env", "all"):
        write_env()
    print("hermes_setup.py complete")
