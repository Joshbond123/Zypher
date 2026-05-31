#!/usr/bin/env python3
"""
hermes_startup_notify.py — Send Zypher startup notification via Telegram.

ROOT CAUSE FIX (2026-05-30):
  Previous version used parse_mode="Markdown" (Telegram MarkdownV1).
  The model name "Qwen3.5-9B-Uncensored-Q4_K_M" contains underscores, which
  Telegram MarkdownV1 treats as italic markers, producing malformed markdown.
  Telegram returned HTTP 400 Bad Request, crashing the step and killing the
  entire workflow before the agent ever reached the "Keep agent alive" step.

  Fix: Send as plain text (no parse_mode). No formatting → no 400 errors.
  The try/except also catches HTTPError explicitly for clear diagnostics.
"""
import os
import urllib.error
import urllib.parse
import urllib.request

BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_USER_ID", "")
RUN = os.environ.get("GITHUB_RUN_ID", "")
MODEL = os.environ.get("HERMES_MODEL_NAME", "llama-3.3-70b-versatile")
CTX = os.environ.get("LOCAL_CTX_SIZE", "65536")


def send(msg):
    if not BOT or not CHAT:
        print("SKIP: TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID not set")
        return
    data = urllib.parse.urlencode({
        "chat_id": CHAT,
        "text": msg,
    }).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data=data,
            timeout=15,
        )
        print("Startup notification sent OK")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        print(f"WARN: startup notification HTTP {exc.code}: {body}")
    except Exception as exc:
        print(f"WARN: startup notification failed: {exc}")


send(
    f"Zypher is online (Run #{RUN})\n\n"
    f"Framework: Hermes Agent\n"
    f"Provider: Groq via local key-rotation proxy\n"
    f"Model: {MODEL}\n"
    f"Context: {CTX} tokens\n"
    f"Auth: local proxy bearer + GROQ_KEY_N rotation\n"
    f"Tools: bash, web_search, browser\n\n"
    f"Ready. Send me anything."
)
