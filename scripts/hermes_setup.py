#!/usr/bin/env python3
  """
  hermes_setup.py — Hermes Agent configuration writer.
  Generates ~/.hermes/config.yaml and ~/.hermes/.env from environment variables.
  """
  import os, sys

  HOME        = os.path.expanduser("~")
  HERMES_DIR  = os.path.join(HOME, ".hermes")
  MEMORY_DIR  = os.path.join(HERMES_DIR, "memories")
  WORKSPACE   = os.path.join(HERMES_DIR, "workspace")
  SKILLS_DIR  = os.path.join(HERMES_DIR, "skills")


  def ensure_dirs():
      for d in [HERMES_DIR, MEMORY_DIR, WORKSPACE, SKILLS_DIR]:
          os.makedirs(d, exist_ok=True)


  def write_config():
      ensure_dirs()
      tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
      uid = os.environ.get("TELEGRAM_USER_ID", "6317345496")
      run_id = os.environ.get("GITHUB_RUN_ID", "0")

      # Round-robin primary model based on run_id to distribute load across keys
      models = [
          "cerebras/qwen3-32b",
          "cerebras/llama-3.3-70b",
          "cerebras/qwen3-32b",
          "cerebras/llama-3.3-70b",
          "cerebras/llama3.1-8b",
      ]
      idx = int(run_id[-1]) % len(models) if run_id and run_id[-1].isdigit() else 0
      primary   = models[idx]
      agents_md = os.path.join(HERMES_DIR, "AGENTS.md")

      config = f"""# ~/.hermes/config.yaml — Zypher Hermes Agent
  # Auto-generated — do not edit manually

  model:
    provider: custom
    baseURL: https://api.cerebras.ai/v1
    name: {primary}
    fallbacks:
      - cerebras/llama-3.3-70b
      - cerebras/llama3.1-8b
      - cerebras/qwen3-32b
    maxTokens: 8192
    temperature: 0.7
    streaming: true

  agent:
    name: Zypher
    memoryFile: MEMORY.md
    userMemoryFile: USER.md
    systemPromptFile: {agents_md}
    workspace: {WORKSPACE}
    skillsDir: {SKILLS_DIR}

  gateway:
    platforms:
      telegram:
        enabled: true
        botToken: "{tok}"
        dmPolicy: allowlist
        allowFrom:
          - {uid}
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
    directory: {MEMORY_DIR}

  skills:
    autoLoad: true
    directory: {SKILLS_DIR}
  """
      cfg_path = os.path.join(HERMES_DIR, "config.yaml")
      with open(cfg_path, "w") as f:
          f.write(config)
      print(f"config.yaml written to {cfg_path}")


  def write_env():
      ensure_dirs()
      tok    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
      k1     = os.environ.get("CEREBRAS_API_KEY", "")
      tav1   = os.environ.get("TAVILY_API_KEY", "")
      tav2   = os.environ.get("TAVILY_API_KEY_2", "")
      sb_url = os.environ.get("SUPABASE_URL", "")
      sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
      gh_tok = os.environ.get("GITHUB_TOKEN", "")
      tavily = tav1 if tav1 else tav2

      env_content = f"""# ~/.hermes/.env — Hermes Agent secrets (auto-generated)
  CEREBRAS_API_KEY={k1}
  TELEGRAM_BOT_TOKEN={tok}
  TAVILY_API_KEY={tavily}
  SUPABASE_URL={sb_url}
  SUPABASE_SERVICE_KEY={sb_key}
  GITHUB_TOKEN={gh_tok}
  """
      env_path = os.path.join(HERMES_DIR, ".env")
      with open(env_path, "w") as f:
          f.write(env_content)
      os.chmod(env_path, 0o600)
      print("~/.hermes/.env written")


  if __name__ == "__main__":
      cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
      if cmd in ("write-config", "all"):
          write_config()
      if cmd in ("write-env", "all"):
          write_env()
      print("hermes_setup.py done")
  