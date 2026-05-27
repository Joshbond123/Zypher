#!/usr/bin/env python3
"""hermes_setup.py — Write Hermes config.yaml and .env from environment variables."""
import os, sys

HOME   = os.path.expanduser("~")
HD     = os.path.join(HOME, ".hermes")
MEMDIR = os.path.join(HD, "memories")
WS     = os.path.join(HD, "workspace")
SK     = os.path.join(HD, "skills")


def dirs():
    for d in [HD, MEMDIR, WS, SK]:
        os.makedirs(d, exist_ok=True)


def write_config():
    dirs()
    tok    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    uid    = os.environ.get("TELEGRAM_USER_ID", "6317345496")
    run_id = os.environ.get("GITHUB_RUN_ID", "0")

    # Use only models confirmed present in the Cerebras API
    models = [
        "llama3.1-8b",
        "qwen-3-235b-a22b-instruct-2507",
        "llama3.1-8b",
        "qwen-3-235b-a22b-instruct-2507",
        "llama3.1-8b",
    ]
    idx     = int(run_id[-1]) % len(models) if run_id and run_id[-1].isdigit() else 0
    primary = models[idx]
    agents_md = os.path.join(HD, "AGENTS.md")

    cfg = f"""# ~/.hermes/config.yaml — Zypher Hermes Agent v0.14.0
model:
  provider: custom
  baseURL: https://api.cerebras.ai/v1
  name: {primary}
  fallbacks:
    - qwen-3-235b-a22b-instruct-2507
    - llama3.1-8b
  maxTokens: 8192
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
      home_chat_id: "{uid}"

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
    print(f"config.yaml written: {p}")


def write_env():
    dirs()
    tok    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    k1     = os.environ.get("CEREBRAS_API_KEY", "")
    tav    = os.environ.get("TAVILY_API_KEY", "") or os.environ.get("TAVILY_API_KEY_2", "")
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    gh     = os.environ.get("GITHUB_TOKEN", "")
    uid    = os.environ.get("TELEGRAM_USER_ID", "6317345496")

    env = (
        f"CEREBRAS_API_KEY={k1}\n"
        f"TELEGRAM_BOT_TOKEN={tok}\n"
        f"TAVILY_API_KEY={tav}\n"
        f"SUPABASE_URL={sb_url}\n"
        f"SUPABASE_SERVICE_KEY={sb_key}\n"
        f"GITHUB_TOKEN={gh}\n"
        # Critical: tells hermes gateway to allow this user without pairing
        f"TELEGRAM_ALLOWED_USERS={uid}\n"
    )
    p = os.path.join(HD, ".env")
    open(p, "w").write(env)
    os.chmod(p, 0o600)
    print(f"~/.hermes/.env written (TELEGRAM_ALLOWED_USERS={uid})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("write-config", "all"):
        write_config()
    if cmd in ("write-env", "all"):
        write_env()
    print("hermes_setup.py done")
