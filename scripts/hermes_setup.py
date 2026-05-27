#!/usr/bin/env python3
"""hermes_setup.py — Write Hermes config.yaml and .env from environment variables.

ROOT-CAUSE ANALYSIS (confirmed against Hermes runtime_provider.py source +
official Hermes docs at hermes-agent.nousresearch.com/docs/integrations/providers):

  PROBLEM 1 — api_key vs key_env (Hermes Issue #12146):
    The providers: dict entry must use `key_env: ENVVAR_NAME` (an env var reference)
    not `api_key: "literal-value"`. Without key_env, Hermes cannot resolve the
    auth token and sends requests with NO Authorization header → HTTP 401
    "Missing Authentication header".

    WRONG (old broken config):
      providers:
        cerebras-proxy:
          base_url: http://127.0.0.1:7860/v1
          api_key: "proxy-placeholder"   ← Hermes ignores literal api_key here

    CORRECT (this file):
      providers:
        cerebras-proxy:
          base_url: http://127.0.0.1:7860/v1
          key_env: OPENAI_API_KEY        ← Hermes reads env var OPENAI_API_KEY
          type: openai                   ← required to identify the API format

  PROBLEM 2 — missing type: openai field:
    Without `type: openai`, some Hermes versions fail to identify the endpoint
    as OpenAI-compatible and fall through to auto-detection, which may skip auth.

  PROBLEM 3 — fallback_providers missing auth:
    The fallback entries also need an auth field. Using api_key: "proxy-placeholder"
    in each fallback entry works because _try_resolve_fallback_provider passes
    explicit_api_key=entry.get("api_key") to the resolver.

  PROBLEM 4 — invalid primary model ID:
    "qwen-3-235b-a22b-instruct-2507" uses an unofficial suffix. The correct
    Cerebras model IDs are: llama3.3-70b, llama3.1-8b, qwen-3-32b.
    Using llama3.3-70b as primary (confirmed available on Cerebras).

  FLOW (after fix):
    Hermes reads OPENAI_API_KEY="proxy-placeholder" from env
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

# Key-rotation proxy (started before gateway in workflow)
PROXY_BASE_URL = "http://127.0.0.1:7860/v1"

# Cerebras model IDs — confirmed available on api.cerebras.ai
PRIMARY_MODEL   = "llama3.3-70b"
FALLBACK_MODELS = ["llama3.1-8b", "qwen-3-32b"]

# Named entry in providers: dict — referenced by model.provider
# Must match exactly (model.provider: cerebras-proxy → providers.cerebras-proxy)
PROVIDER_NAME = "cerebras-proxy"


def ensure_dirs():
    for d in [HD, MEMDIR, WS, SK]:
        os.makedirs(d, exist_ok=True)


def write_config():
    ensure_dirs()
    tok       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    uid       = os.environ.get("TELEGRAM_USER_ID", "6317345496")
    agents_md = os.path.join(HD, "AGENTS.md")

    # KEY FIX: use key_env: OPENAI_API_KEY (not api_key: literal).
    # Hermes reads the named env var at runtime to build the Authorization header.
    # OPENAI_API_KEY is set to "proxy-placeholder" in the workflow env — the proxy
    # accepts any Bearer token and injects a real Cerebras key before forwarding.
    # type: openai is required so Hermes knows the endpoint is OpenAI-compatible.
    cfg = (
        "# ~/.hermes/config.yaml — Zypher Agent (auto-generated)\n"
        "\n"
        "# providers: DICT — named custom provider referenced by model.provider.\n"
        "# key_env tells Hermes which env var holds the Bearer token.\n"
        "# OPENAI_API_KEY=proxy-placeholder in workflow env → proxy injects real key.\n"
        "providers:\n"
        f"  {PROVIDER_NAME}:\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        "    key_env: OPENAI_API_KEY\n"
        "    type: openai\n"
        "\n"
        "model:\n"
        f"  provider: {PROVIDER_NAME}\n"
        f"  default: {PRIMARY_MODEL}\n"
        "  max_tokens: 8192\n"
        "  temperature: 0.7\n"
        "  streaming: true\n"
        "\n"
        "# fallback_providers — tried in order when primary fails.\n"
        "# api_key: literal value works here because _try_resolve_fallback_provider\n"
        "# passes explicit_api_key=entry.get('api_key') directly to the resolver.\n"
        "fallback_providers:\n"
        '  - provider: "custom"\n'
        f"    model: {FALLBACK_MODELS[0]}\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        '    api_key: "proxy-placeholder"\n'
        '  - provider: "custom"\n'
        f"    model: {FALLBACK_MODELS[1]}\n"
        f"    base_url: {PROXY_BASE_URL}\n"
        '    api_key: "proxy-placeholder"\n'
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
    print(f"  provider entry  : {PROVIDER_NAME}")
    print(f"    base_url      : {PROXY_BASE_URL}")
    print(f"    key_env       : OPENAI_API_KEY  (reads Bearer token from env)")
    print(f"    type          : openai")
    print(f"  model.provider  : {PROVIDER_NAME}")
    print(f"  model.default   : {PRIMARY_MODEL}")
    print(f"  fallbacks       : {FALLBACK_MODELS[0]}, {FALLBACK_MODELS[1]} (api_key: proxy-placeholder)")


def write_env():
    ensure_dirs()
    tok    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    k1     = os.environ.get("CEREBRAS_API_KEY", "")
    tav    = os.environ.get("TAVILY_API_KEY", "") or os.environ.get("TAVILY_API_KEY_2", "")
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    gh     = os.environ.get("GITHUB_TOKEN", "")
    uid    = os.environ.get("TELEGRAM_USER_ID", "6317345496")
    # OPENAI_API_KEY: referenced by key_env: OPENAI_API_KEY in the providers dict.
    # The proxy ignores incoming auth and injects a real Cerebras key — but Hermes
    # needs a non-empty value here or it sends no Authorization header (Issue #12146).
    # OPENROUTER_API_KEY: guard against auxiliary tasks hitting openrouter with no key.
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
