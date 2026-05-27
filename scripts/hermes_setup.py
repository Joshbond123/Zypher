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
      tok      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
      uid      = os.environ.get("TELEGRAM_USER_ID", "6317345496")
      agents_md = os.path.join(HD, "AGENTS.md")

      # ROOT-CAUSE FIX (verified against Hermes source):
      # ─────────────────────────────────────────────────
      # The original config used:
      #   custom_providers: [{name: cerebras-proxy, ...}]
      #   model: {provider: cerebras-proxy, ...}
      #
      # PROBLEM: get_provider_profile("cerebras-proxy") returns None — "cerebras-proxy"
      # is NOT a registered Hermes plugin. custom_providers: is only used by the picker
      # UI (get_compatible_custom_providers), NOT by the runtime provider resolver.
      # Hermes fell back to provider:auto → OpenRouter → 401 → bot consumed messages
      # but sent zero replies.
      #
      # FIX: Use provider:"custom" — the verified registered Hermes plugin
      # (plugins/model-providers/custom/__init__.py, env_vars=()). Set base_url
      # directly under model: so the runtime CustomProfile resolves it correctly.
      # Set fallback_providers at TOP LEVEL (the key get_fallback_chain() reads).
      cfg = f"""# ~/.hermes/config.yaml — Zypher Agent (auto-generated)

  model:
    provider: "custom"
    base_url: {PROXY_BASE_URL}
    default: {PRIMARY_MODEL}
    api_key: "proxy-placeholder"
    max_tokens: 8192
    temperature: 0.7
    streaming: true

  # Fallback chain — top-level key read by get_fallback_chain()
  # Each entry MUST have both 'provider' and 'model' keys.
  fallback_providers:
    - provider: "custom"
      model: {FALLBACK_MODELS[0]}
      base_url: {PROXY_BASE_URL}
    - provider: "custom"
      model: {FALLBACK_MODELS[1]}
      base_url: {PROXY_BASE_URL}

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
      print(f"  provider : custom (registered Hermes plugin, routes to local proxy)")
      print(f"  base_url : {PROXY_BASE_URL}")
      print(f"  model    : {PRIMARY_MODEL}")
      print(f"  fallbacks: {', '.join(FALLBACK_MODELS)} (via fallback_providers: top-level)")


  def write_env():
      ensure_dirs()
      tok    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
      k1     = os.environ.get("CEREBRAS_API_KEY", "")
      tav    = os.environ.get("TAVILY_API_KEY", "") or os.environ.get("TAVILY_API_KEY_2", "")
      sb_url = os.environ.get("SUPABASE_URL", "")
      sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
      gh     = os.environ.get("GITHUB_TOKEN", "")
      uid    = os.environ.get("TELEGRAM_USER_ID", "6317345496")

      # OPENAI_API_KEY — safety net: custom provider falls back to this env var
      #                  for auth if model.api_key is not used. Value doesn't
      #                  matter since our proxy injects the real Cerebras key.
      # OPENROUTER_API_KEY — prevents 401 if any auxiliary task falls through
      #                  to the openrouter provider by mistake.
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
      print(f"  OPENAI_API_KEY        : proxy-placeholder (custom provider auth fallback)")
      print(f"  OPENROUTER_API_KEY    : proxy-placeholder (auxiliary task guard)")
      print(f"  TELEGRAM_HOME_CHANNEL : {uid}")
      print(f"  TELEGRAM_ALLOWED_USERS: {uid}")


  if __name__ == "__main__":
      cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
      if cmd in ("write-config", "all"):
          write_config()
      if cmd in ("write-env", "all"):
          write_env()
      print("hermes_setup.py complete")
  