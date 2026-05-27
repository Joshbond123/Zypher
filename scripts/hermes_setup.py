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

# Best Cerebras model for agents on free tier: Qwen3 235B MoE (largest, best reasoning)
PRIMARY_MODEL   = "qwen-3-235b-a22b-instruct-2507"
FALLBACK_MODELS = ["gpt-oss-120b", "llama3.1-8b"]


def ensure_dirs():
    for d in [HD, MEMDIR, WS, SK]:
        os.makedirs(d, exist_ok=True)


def write_config():
    ensure_dirs()
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    uid = os.environ.get("TELEGRAM_USER_ID", "6317345496")
    agents_md = os.path.join(HD, "AGENTS.md")

    # IMPORTANT: hermes reads snake_case keys from config.yaml
    #   base_url  (not baseURL) — points to local key-rotation proxy
    #   provider: openrouter       — required for OpenAI-compatible SDK path
    #   OPENAI_API_KEY         — hermes reads this when provider=openai + custom base_url
    cfg = f"""# ~/.hermes/config.yaml — Zypher Agent (auto-generated)
model:
  provider: openrouter
  base_url: {PROXY_BASE_URL}
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
    print(f"  model   : {PRIMARY_MODEL}")
    print(f"  base_url: {PROXY_BASE_URL}")
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

    # OPENAI_API_KEY: hermes reads this for custom base_url + provider=openai
    # The actual auth is done by the proxy; we just need a non-empty value here.
    env = (
        f"CEREBRAS_API_KEY={k1}\n"
        f"OPENAI_API_KEY=proxy-placeholder\n"
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
    print(f"  OPENAI_API_KEY     : proxy-placeholder (real auth via proxy)")
    print(f"  TELEGRAM_HOME_CHANNEL    : {uid}")
    print(f"  TELEGRAM_ALLOWED_USERS   : {uid}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("write-config", "all"):
        write_config()
    if cmd in ("write-env", "all"):
        write_env()
    print("hermes_setup.py complete")
