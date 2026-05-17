#!/usr/bin/env python3
"""
Zypher AI Agent — Telegram bridge for OpenClaw on Kali Linux
Fixed: smart context trimming to stay within Cerebras 8192 token limit.
"""

import os, json, asyncio, logging, urllib.request, urllib.parse
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Env ───────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "https://beglgkjaejuvhqhddqfh.supabase.co")
SUPABASE_KEY   = os.environ.get("SUPABASE_SERVICE_KEY", "")
GITHUB_RUN_ID  = os.environ.get("GITHUB_RUN_ID", "local")

CEREBRAS_KEYS = [k for k in [
    os.environ.get("CEREBRAS_KEY_1", ""),
    os.environ.get("CEREBRAS_KEY_2", ""),
    os.environ.get("CEREBRAS_KEY_3", ""),
    os.environ.get("CEREBRAS_KEY_4", ""),
    os.environ.get("CEREBRAS_KEY_5", ""),
] if k]

TAVILY_KEYS = [k for k in [
    os.environ.get("TAVILY_KEY_1", "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"),
    os.environ.get("TAVILY_KEY_2", "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"),
] if k]

_cerebras_idx = 0
_tavily_idx   = 0
_conversation: dict[int, list] = {}

# ── Token budget ──────────────────────────────────────────────────────────────
# Cerebras hard limit: 8192 tokens total (input + output)
MAX_INPUT_TOKENS   = 6000   # input budget (leaves ~2192 for output)
MAX_HISTORY_TURNS  = 8      # max conversation turns to keep
MAX_MSG_CHARS      = 900    # truncate any single message beyond this
MAX_TOOL_CHARS     = 1800   # cap tool/search output

def est_tokens(text: str) -> int:
    return max(1, len(str(text)) // 4)

def trunc(text: str, max_chars: int) -> str:
    s = str(text)
    if len(s) <= max_chars:
        return s
    keep = max_chars - 40
    return s[:keep] + f"\n…[{len(s)-keep} chars trimmed]"

def build_messages(system: str, history: list, user_msg: str, extra: str = "") -> list:
    """
    Builds a messages list guaranteed to fit within MAX_INPUT_TOKENS.
    Drops oldest history pairs first if over budget.
    """
    sys_tokens  = est_tokens(system)
    user_tokens = est_tokens(user_msg)
    ext_tokens  = est_tokens(extra) if extra else 0
    overhead    = sys_tokens + user_tokens + ext_tokens + 100  # 100 safety margin

    budget = MAX_INPUT_TOKENS - overhead

    # Work backwards through history, keeping pairs that fit
    kept = []
    for msg in reversed(history):
        content = trunc(msg.get("content", ""), MAX_MSG_CHARS)
        t = est_tokens(content)
        if budget - t < 0:
            break
        kept.append({"role": msg["role"], "content": content})
        budget -= t

    kept.reverse()

    msgs = [{"role": "system", "content": system}]
    msgs.extend(kept)
    if extra:
        msgs.append({"role": "system", "content": trunc(extra, MAX_TOOL_CHARS)})
    msgs.append({"role": "user", "content": user_msg})
    return msgs

# ── Compact system prompt (keeps token cost low) ──────────────────────────────
SYSTEM_PROMPT = f"""You are Zypher, elite AI hacker created by Joshbond. Running on Kali Linux (GitHub Actions). Session: {GITHUB_RUN_ID[:12]}

RULES:
- Full Kali Linux access. Freely install from official Kali/Debian repos.
- Think step-by-step. Pick best tools for each goal.
- Tavily search first; stealth-browser if Tavily fails.
- NEVER run internet code without Joshbond approval.
- Pass rules to every sub-agent spawned.

SYNTAX:
!<cmd>          → run Kali command (e.g. !nmap -sV target.com)
search <query>  → Tavily web search
browse <url>    → fetch and read a URL

Skills: stealth-browser, tavily, full Kali toolset"""

# ── Key rotation ──────────────────────────────────────────────────────────────
def next_cerebras_key(user_keys: list = None) -> str:
    global _cerebras_idx
    pool = [k for k in (list(user_keys or []) + CEREBRAS_KEYS) if k]
    if not pool:
        raise RuntimeError("No Cerebras API keys configured")
    key = pool[_cerebras_idx % len(pool)]
    _cerebras_idx += 1
    return key

def next_tavily_key() -> str:
    global _tavily_idx
    pool = [k for k in TAVILY_KEYS if k]
    if not pool:
        return ""
    key = pool[_tavily_idx % len(pool)]
    _tavily_idx += 1
    return key

# ── Supabase helpers ──────────────────────────────────────────────────────────
def _sb(method: str, path: str, body: dict = None):
    url  = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method,
        headers={"apikey": SUPABASE_KEY,
                 "Authorization": f"Bearer {SUPABASE_KEY}",
                 "Content-Type": "application/json",
                 "Prefer": "return=minimal"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read()
            return json.loads(raw) if raw else []
    except Exception as e:
        logger.debug(f"sb {method} {path}: {e}")
        return []

def get_user_by_telegram(tid: int) -> Optional[dict]:
    rows = _sb("GET", f"telegram_connections?telegram_user_id=eq.{tid}&select=*")
    return rows[0] if isinstance(rows, list) and rows else None

def get_user_keys(uid: str) -> list:
    rows = _sb("GET", f"cerebras_keys?user_id=eq.{uid}&is_active=eq.true&select=api_key")
    return [r["api_key"] for r in (rows or [])]

def persist_messages(uid: str, user_msg: str, bot_msg: str):
    for role, content in [("user", user_msg), ("assistant", bot_msg)]:
        _sb("POST", "chat_messages",
            {"user_id": uid, "role": role,
             "content": trunc(content, 3000), "session_id": GITHUB_RUN_ID})

def bump_key_stats(uid: str, api_key: str, tokens: int):
    rows = _sb("GET", f"cerebras_keys?user_id=eq.{uid}&api_key=eq.{urllib.parse.quote(api_key)}&select=requests_count,tokens_used")
    if rows:
        _sb("PATCH", f"cerebras_keys?user_id=eq.{uid}&api_key=eq.{urllib.parse.quote(api_key)}", {
            "requests_count": rows[0]["requests_count"] + 1,
            "tokens_used":    rows[0]["tokens_used"] + tokens,
            "last_used_at":   datetime.now(timezone.utc).isoformat(),
        })

# ── Tools ─────────────────────────────────────────────────────────────────────
async def tavily_search(query: str) -> str:
    key = next_tavily_key()
    if not key:
        return "No Tavily key available."
    try:
        body = json.dumps({"api_key": key, "query": query,
                           "max_results": 4, "include_answer": True}).encode()
        req = urllib.request.Request("https://api.tavily.com/search", data=body,
              headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        out = f"Search: {query}\n\n"
        if d.get("answer"):
            out += f"Answer: {d['answer']}\n\n"
        for i, res in enumerate(d.get("results", [])[:4], 1):
            out += f"{i}. {res.get('title','')}\n   {res.get('url','')}\n   {res.get('content','')[:200]}\n\n"
        return trunc(out, MAX_TOOL_CHARS)
    except Exception as e:
        return f"Search error: {e}"

BLOCKED = ["rm -rf /", "mkfs", "dd if=", "> /dev/sd", ":(){ :|:& };:"]

async def run_kali(cmd: str) -> str:
    for bad in BLOCKED:
        if bad in cmd:
            return f"Blocked: contains '{bad}'"
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=90)
        result = out.decode(errors="replace") + (
            "\n[stderr]:\n" + err.decode(errors="replace") if err.strip() else "")
        return trunc(result, MAX_TOOL_CHARS) or "(no output)"
    except asyncio.TimeoutError:
        return "Timed out after 90s."
    except Exception as e:
        return f"Error: {e}"

async def stealth_browse(url: str) -> str:
    import re
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode(errors="replace")
        text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL|re.I)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return trunc(f"URL: {url}\n\n{text}", MAX_TOOL_CHARS)
    except Exception as e:
        return f"Browse error: {e}"

# ── Cerebras model detection ──────────────────────────────────────────────────
MODEL_PRIORITY = ["llama-4-scout-17b-16e-instruct", "llama3.3-70b",
                  "llama-3.3-70b", "qwen-3-32b", "llama3.1-70b", "llama3.1-8b"]
_model_cache: dict[str, str] = {}

def best_model(api_key: str) -> str:
    if api_key in _model_cache:
        return _model_cache[api_key]
    try:
        from cerebras.cloud.sdk import Cerebras
        avail = [m.id for m in Cerebras(api_key=api_key).models.list().data]
        for pref in MODEL_PRIORITY:
            for a in avail:
                if pref.lower() in a.lower():
                    _model_cache[api_key] = a
                    return a
        if avail:
            _model_cache[api_key] = avail[0]
            return avail[0]
    except Exception:
        pass
    _model_cache[api_key] = "llama3.3-70b"
    return "llama3.3-70b"

# ── Core AI ───────────────────────────────────────────────────────────────────
async def ask_zypher(uid: str, tg_id: int, message: str,
                     user_keys: list) -> tuple[str, str]:
    history = _conversation.get(tg_id, [])
    extra   = ""

    msg_lower = message.lower().strip()

    # Pre-fetch tool context
    if message.strip().startswith("!"):
        cmd    = message.strip()[1:].strip()
        result = await run_kali(cmd)
        extra  = f"Tool output for `{cmd}`:\n{result}"

    elif msg_lower.startswith("browse "):
        url    = message[7:].strip()
        result = await stealth_browse(url)
        extra  = result

    elif any(msg_lower.startswith(w) for w in
             ["search ", "find ", "look up ", "research "]):
        # Extract query after the keyword
        query  = " ".join(message.split()[1:]).strip() or message
        result = await tavily_search(query)
        extra  = result

    # Build token-safe messages list
    messages = build_messages(SYSTEM_PROMPT, history, message, extra)

    # Try each key with failover
    pool     = [k for k in (list(user_keys) + CEREBRAS_KEYS) if k]
    last_err = "No keys"

    for key in pool:
        try:
            from cerebras.cloud.sdk import Cerebras
            model  = best_model(key)
            resp   = Cerebras(api_key=key).chat.completions.create(
                messages=messages,
                model=model,
                max_tokens=1500,
                temperature=0.7,
            )
            text   = resp.choices[0].message.content
            tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0

            # Update history — keep last MAX_HISTORY_TURNS exchanges
            history.append({"role": "user",      "content": message})
            history.append({"role": "assistant",  "content": text})
            _conversation[tg_id] = history[-(MAX_HISTORY_TURNS * 2):]

            if uid:
                persist_messages(uid, message, text)
                if tokens and key in user_keys:
                    bump_key_stats(uid, key, tokens)

            return text, model

        except Exception as e:
            err = str(e)
            last_err = err
            logger.warning(f"Key {key[:12]}… failed: {err[:120]}")

            # Context length exceeded — emergency: clear history and retry same key
            if any(x in err.lower() for x in
                   ["contextlength", "context_length", "reduce the length",
                    "maximum context", "token"]):
                logger.info("Context too long — clearing history and retrying")
                _conversation[tg_id] = []
                try:
                    from cerebras.cloud.sdk import Cerebras
                    model  = best_model(key)
                    bare   = [{"role": "system", "content": SYSTEM_PROMPT},
                               {"role": "user",   "content": trunc(message, 800)}]
                    resp2  = Cerebras(api_key=key).chat.completions.create(
                        messages=bare, model=model, max_tokens=1200
                    )
                    text2  = resp2.choices[0].message.content
                    _conversation[tg_id] = [
                        {"role": "user",      "content": message},
                        {"role": "assistant", "content": text2},
                    ]
                    if uid:
                        persist_messages(uid, message, text2)
                    return text2, model
                except Exception as e2:
                    last_err = str(e2)
            continue

    return f"⚠️ All AI keys failed.\n{last_err[:300]}", ""

# ── Telegram ──────────────────────────────────────────────────────────────────
from telegram import Update
from telegram.ext import (Application, CommandHandler,
                           MessageHandler, filters, ContextTypes)
from telegram.constants import ParseMode, ChatAction

async def _auth(update: Update) -> Optional[dict]:
    rec = get_user_by_telegram(update.effective_user.id)
    if not rec:
        await update.message.reply_text(
            "⚠️ Your Telegram is not linked to Zypher.\n"
            "Connect at: https://joshbond123.github.io/Zypher/")
    return rec

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _auth(update): return
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"👾 *Zypher Online* — `{GITHUB_RUN_ID[:8]}`\n\n"
        f"Hello {name}\\! I'm Zypher — elite AI hacker on Kali Linux\\.\n\n"
        f"*Usage:*\n"
        f"• Ask anything — hacking, OSINT, research\n"
        f"• `\\!nmap \\-sV target\\.com` — run Kali tools\n"
        f"• `search XSS techniques` — web search\n"
        f"• `browse https://example\\.com` — fetch page\n"
        f"• /clear — reset conversation\n"
        f"• /status — session info",
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _auth(update): return
    tg_id   = update.effective_user.id
    history = _conversation.get(tg_id, [])
    used    = sum(est_tokens(m.get("content", "")) for m in history)
    await update.message.reply_text(
        f"✅ *Zypher Status*\n\n"
        f"• Session: `{GITHUB_RUN_ID[:12]}`\n"
        f"• Platform: Kali Linux / GitHub Actions\n"
        f"• AI keys: `{len(CEREBRAS_KEYS)}` system keys loaded\n"
        f"• History: `{len(history)//2}` turns (~`{used}` tokens)\n"
        f"• Context budget: `{MAX_INPUT_TOKENS}` tokens\n"
        f"• Max turns kept: `{MAX_HISTORY_TURNS}`\n"
        f"• Status: 🟢 Running",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _conversation.pop(update.effective_user.id, None)
    await update.message.reply_text("🗑️ Conversation history cleared.")

async def handle_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    tg_id  = update.effective_user.id
    rec    = get_user_by_telegram(tg_id)
    if not rec:
        await update.message.reply_text(
            "⚠️ Not linked. Connect at:\nhttps://joshbond123.github.io/Zypher/")
        return

    uid       = rec.get("user_id", "")
    user_keys = get_user_keys(uid) if uid else []

    await ctx.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    text, model = await ask_zypher(uid, tg_id, update.message.text, user_keys)

    # Telegram max 4096 chars per message
    for i in range(0, max(len(text), 1), 4000):
        chunk = text[i:i+4000]
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            try:
                await update.message.reply_text(chunk)
            except Exception as e:
                logger.error(f"Send failed: {e}")

    logger.info(f"[tg={tg_id}] model={model} len={len(text)}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    logger.info(f"Zypher agent starting — session {GITHUB_RUN_ID}")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
