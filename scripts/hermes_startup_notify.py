#!/usr/bin/env python3
import os
import urllib.parse
import urllib.request

BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_USER_ID", "")
RUN = os.environ.get("GITHUB_RUN_ID", "")
MODEL = "Qwen3.5-9B-Uncensored-Q4_K_M"


def send(msg):
    if not BOT or not CHAT:
        return
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": msg, "parse_mode": "Markdown"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{BOT}/sendMessage", data=data, timeout=15)


send(
    f"*Zypher is online* (Run #{RUN})\n\n"
    "Framework: Hermes Agent\n"
    f"Provider: local Ollama OpenAI-compatible server\n"
    f"Model: `{MODEL}`\n"
    "Auth: local placeholder key ✓\n"
    "Tools: bash, web_search, browser\n\n"
    "Ready. Send me anything."
)
