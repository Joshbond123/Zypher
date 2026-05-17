#!/usr/bin/env python3
"""
Zypher AI Agent - Telegram bridge for OpenClaw on Kali Linux
Connects Telegram users to the Zypher AI (powered by Cerebras)
with Tavily web search and full Kali Linux tool access.
"""

import os
import sys
import json
import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Environment ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL     = os.environ.get("SUPABASE_URL", "https://beglgkjaejuvhqhddqfh.supabase.co")
SUPABASE_KEY     = os.environ.get("SUPABASE_SERVICE_KEY", "")
GITHUB_RUN_ID    = os.environ.get("GITHUB_RUN_ID", "local")

CEREBRAS_KEYS = [
    os.environ.get("CEREBRAS_KEY_1", ""),
    os.environ.get("CEREBRAS_KEY_2", ""),
    os.environ.get("CEREBRAS_KEY_3", ""),
    os.environ.get("CEREBRAS_KEY_4", ""),
    os.environ.get("CEREBRAS_KEY_5", ""),
]
CEREBRAS_KEYS = [k for k in CEREBRAS_KEYS if k]

TAVILY_KEYS = [
    os.environ.get("TAVILY_KEY_1", "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"),
    os.environ.get("TAVILY_KEY_2", "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"),
]

# ── Key rotation state ────────────────────────────────────────────────────────
_cerebras_idx = 0
_tavily_idx   = 0
_conversation: dict[int, list] = {}   # telegram_user_id -> message history

AGENTS_MD_PATH = os.path.join(os.path.dirname(__file__), "..", "AGENTS.md")

def load_system_prompt() -> str:
    try:
        with open(AGENTS_MD_PATH) as f:
            return f.read()
    except Exception:
        return (
            "You are Zypher, the best hacker that has ever existed. "
            "You are installed on a Kali Linux machine. "
            "Follow the rules in AGENTS.md at all times."
        )

def get_cerebras_key(user_keys: list = None) -> str:
    global _cerebras_idx
    pool = list(user_keys or []) + CEREBRAS_KEYS
    pool = [k for k in pool if k]
    if not pool:
        raise RuntimeError("No Cerebras API keys available")
    key = pool[_cerebras_idx % len(pool)]
    _cerebras_idx += 1
    return key

def get_tavily_key() -> str:
    global _tavily_idx
    pool = [k for k in TAVILY_KEYS if k]
    if not pool:
        return ""
    key = pool[_tavily_idx % len(pool)]
    _tavily_idx += 1
    return key

# ── Supabase helpers ──────────────────────────────────────────────────────────
import urllib.request, urllib.parse

def sb_request(method: str, path: str, body: dict = None) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        logger.warning(f"Supabase {method} {path}: {e}")
        return {}

def get_user_by_telegram(telegram_id: int) -> Optional[dict]:
    try:
        result = sb_request("GET", f"telegram_connections?telegram_user_id=eq.{telegram_id}&select=*,profiles(*)")
        if isinstance(result, list) and result:
            return result[0]
    except Exception:
        pass
    return None

def get_user_cerebras_keys(user_id: str) -> list:
    try:
        result = sb_request("GET", f"cerebras_keys?user_id=eq.{user_id}&is_active=eq.true&select=api_key")
        if isinstance(result, list):
            return [r["api_key"] for r in result]
    except Exception:
        pass
    return []

def save_message(user_id: str, role: str, content: str, session_id: str):
    sb_request("POST", "chat_messages", {
        "user_id": user_id,
        "role": role,
        "content": content,
        "session_id": session_id,
    })

def update_key_usage(user_id: str, api_key: str, tokens: int):
    try:
        current = sb_request("GET", f"cerebras_keys?user_id=eq.{user_id}&api_key=eq.{urllib.parse.quote(api_key)}&select=requests_count,tokens_used")
        if isinstance(current, list) and current:
            sb_request("PATCH", f"cerebras_keys?user_id=eq.{user_id}&api_key=eq.{urllib.parse.quote(api_key)}", {
                "requests_count": current[0]["requests_count"] + 1,
                "tokens_used": current[0]["tokens_used"] + tokens,
                "last_used_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        logger.debug(f"Usage update failed: {e}")

# ── Tavily search ─────────────────────────────────────────────────────────────
async def web_search(query: str) -> str:
    key = get_tavily_key()
    if not key:
        return "No search API key configured."
    try:
        import urllib.request as ur
        body = json.dumps({
            "api_key": key,
            "query": query,
            "max_results": 5,
            "include_answer": True,
        }).encode()
        req = ur.Request("https://api.tavily.com/search", data=body,
                          headers={"Content-Type": "application/json"})
        with ur.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        out = f"**Search: {query}**\n\n"
        if data.get("answer"):
            out += f"**Answer:** {data['answer']}\n\n"
        for i, res in enumerate(data.get("results", [])[:5], 1):
            out += f"{i}. **{res.get('title','')}**\n   {res.get('url','')}\n   {res.get('content','')[:300]}\n\n"
        return out
    except Exception as e:
        return f"Search error: {e}"

# ── Kali tool execution ───────────────────────────────────────────────────────
BLOCKED = ["rm -rf /", "mkfs", "dd if=", "> /dev/sd", ":(){ :|:& };:"]

async def run_tool(command: str) -> str:
    for bad in BLOCKED:
        if bad in command:
            return f"⚠️ Blocked: {bad}"
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        result = out + (f"\n[stderr]: {err}" if err else "")
        return result[:4000] if result else "(no output)"
    except asyncio.TimeoutError:
        return "⏱️ Command timed out (60s)"
    except Exception as e:
        return f"Error: {e}"

# ── Cerebras AI ───────────────────────────────────────────────────────────────
BEST_MODELS = [
    "llama-4-scout-17b-16e-instruct",
    "llama3.3-70b",
    "llama3.1-70b",
    "qwen-3-32b",
    "llama3.1-8b",
]
_model_cache: dict[str, str] = {}

def detect_best_model(api_key: str) -> str:
    if api_key in _model_cache:
        return _model_cache[api_key]
    try:
        from cerebras.cloud.sdk import Cerebras
        c = Cerebras(api_key=api_key)
        models = c.models.list()
        available = [m.id for m in models.data]
        for preferred in BEST_MODELS:
            for avail in available:
                if preferred.lower() in avail.lower():
                    _model_cache[api_key] = avail
                    return avail
        if available:
            _model_cache[api_key] = available[0]
            return available[0]
    except Exception as e:
        logger.debug(f"Model detection: {e}")
    _model_cache[api_key] = "llama3.3-70b"
    return "llama3.3-70b"

async def ask_ai(user_id_str: str, telegram_id: int, message: str, user_cerebras_keys: list) -> tuple[str, str]:
    """Returns (response_text, model_used)"""
    key = get_cerebras_key(user_cerebras_keys)
    model = detect_best_model(key)

    history = _conversation.get(telegram_id, [])
    history.append({"role": "user", "content": message})

    messages = [{"role": "system", "content": load_system_prompt()}]
    messages += history[-30:]

    # Check if message contains search intent
    search_result = ""
    if any(w in message.lower() for w in ["search", "find", "look up", "research", "browse", "google"]):
        query = message.replace("search", "").replace("find", "").replace("look up", "").replace("research", "").strip()
        if query:
            search_result = await web_search(query)
            messages.append({"role": "system", "content": f"Search results:\n{search_result}"})

    # Check for tool execution request
    if message.strip().startswith("!"):
        cmd = message.strip()[1:]
        tool_output = await run_tool(cmd)
        messages.append({"role": "system", "content": f"Tool output:\n{tool_output}"})

    try:
        from cerebras.cloud.sdk import Cerebras
        client = Cerebras(api_key=key)
        resp = client.chat.completions.create(
            messages=messages,
            model=model,
            max_tokens=4096,
            temperature=0.7,
        )
        text = resp.choices[0].message.content
        tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0

        history.append({"role": "assistant", "content": text})
        _conversation[telegram_id] = history[-40:]

        # Save to Supabase
        if user_id_str:
            save_message(user_id_str, "user", message, GITHUB_RUN_ID)
            save_message(user_id_str, "assistant", text, GITHUB_RUN_ID)
            if tokens:
                update_key_usage(user_id_str, key, tokens)

        return text, model
    except Exception as e:
        logger.error(f"Cerebras error: {e}")
        # Try next key
        try:
            key2 = get_cerebras_key(user_cerebras_keys)
            if key2 != key:
                from cerebras.cloud.sdk import Cerebras
                client2 = Cerebras(api_key=key2)
                model2 = detect_best_model(key2)
                resp2 = client2.chat.completions.create(
                    messages=messages, model=model2, max_tokens=4096
                )
                text2 = resp2.choices[0].message.content
                history.append({"role": "assistant", "content": text2})
                _conversation[telegram_id] = history[-40:]
                return text2, model2
        except Exception as e2:
            logger.error(f"Fallback key also failed: {e2}")
        return f"⚠️ AI error: {e}", ""

# ── Telegram bot ──────────────────────────────────────────────────────────────
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode, ChatAction

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_telegram(update.effective_user.id)
    if not user:
        await update.message.reply_text(
            "⚠️ Your Telegram account is not linked to Zypher.\n\n"
            "Please visit the Zypher dashboard to connect your account:\n"
            "https://joshbond123.github.io/Zypher/"
        )
        return
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"👾 *Zypher Online* — Session `{GITHUB_RUN_ID[:8]}`\n\n"
        f"Hello {name}! I'm Zypher, the best hacker that has ever existed.\n"
        f"I'm running on a Kali Linux machine in the cloud.\n\n"
        f"You can:\n"
        f"• Ask me anything — hacking, OSINT, security, research\n"
        f"• Run Kali tools: `!nmap -sV target.com`\n"
        f"• Search the web: `search XSS techniques`\n"
        f"• `/clear` — clear conversation history\n"
        f"• `/status` — check session status\n\n"
        f"🔍 Skills: stealth-browser + tavily loaded\n"
        f"🤖 Model: auto-detected (most powerful available)",
        parse_mode=ParseMode.MARKDOWN
    )

async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_telegram(update.effective_user.id)
    if not user:
        await update.message.reply_text("❌ Not linked. Visit the dashboard.")
        return
    await update.message.reply_text(
        f"✅ *Zypher Status*\n\n"
        f"• Session: `{GITHUB_RUN_ID[:12]}`\n"
        f"• Platform: Kali Linux (GitHub Actions)\n"
        f"• Cerebras keys: `{len(CEREBRAS_KEYS)}` system keys loaded\n"
        f"• Tavily: `{len([k for k in TAVILY_KEYS if k])}` keys\n"
        f"• Status: 🟢 Running",
        parse_mode=ParseMode.MARKDOWN
    )

async def clear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _conversation.pop(update.effective_user.id, None)
    await update.message.reply_text("🗑️ Conversation cleared.")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    telegram_id = update.effective_user.id
    user_record = get_user_by_telegram(telegram_id)

    if not user_record:
        await update.message.reply_text(
            "⚠️ Your Telegram is not linked to Zypher.\n"
            "Please connect at: https://joshbond123.github.io/Zypher/"
        )
        return

    user_id = user_record.get("user_id", "")
    user_cerebras_keys = get_user_cerebras_keys(user_id) if user_id else []

    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    text, model = await ask_ai(user_id, telegram_id, update.message.text, user_cerebras_keys)

    # Split long messages
    if len(text) > 4000:
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await update.message.reply_text(chunk)
    else:
        try:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_text(text)

    if model:
        logger.info(f"[{telegram_id}] model={model} len={len(text)}")

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"Zypher agent starting — session {GITHUB_RUN_ID}")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
