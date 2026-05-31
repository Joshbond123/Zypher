#!/usr/bin/env python3
"""
Thread-safe Groq API key rotation proxy for Hermes agent.
413 Eradicator Edition — v5.

ROOT CAUSE ANALYSIS (definitive, May 2026)
==========================================

v1-v3: Various truncation bugs (message count only, skipped list-format content, etc.)
v4: Fixed content format handling (string + list). Still failing. Three remaining issues:

ISSUE 1 — Thinking tokens (primary killer):
  qwen/qwen3-32b is a "thinking" model. Every response contains a reasoning
  chain (5K-30K tokens) that Groq returns in a SEPARATE field `reasoning_content`
  alongside the regular `content` field. Hermes v0.15.x stores this field in
  conversation history. v4 only trimmed `content`; `reasoning_content` passed
  through intact. After 3-4 turns the assistant messages alone were 50-200 KB
  of stored thinking tokens.

ISSUE 2 — Tools array never trimmed:
  Hermes creates custom skills as tool definitions. Each new skill adds a JSON
  schema (description + parameters) to every request. After skill-heavy use,
  the tools array alone can be 30-80 KB. v4 only touched `messages`.

ISSUE 3 — Broken compression loop:
  When the proxy returned 413 after 3 passes, Hermes tried to compress
  (summarize) the conversation. That compression call ALSO went through the
  proxy, which trimmed it to only 2 messages. The compression model summarized
  a fragment. The summary was useless. Context stayed large. Next turn: 413
  again. Infinite loop → session reset.

v5 FIXES
--------
  FIX 1 — Disable thinking entirely:
    Inject reasoning_effort="none" into every chat completion request.
    Also strip reasoning_content already stored in conversation history.

  FIX 2 — Trim tools array:
    On every pass, truncate each tool description to MAX_TOOL_DESC_CHARS.
    On aggressive passes, drop tools beyond the top N.

  FIX 3 — Handle Groq 400 "context too large" like a 413:
    Groq sometimes returns HTTP 400 (not 413) with body containing
    "context_length_exceeded" or "Request entity too large".
    v5 detects these and applies the same trim-and-retry loop.

MODEL SWITCH (hermes_setup.py companion change):
  Primary model changed from qwen/qwen3-32b (thinking model) to
  llama-3.3-70b-versatile (no thinking, 131K context, fast responses).
  This eliminates ISSUE 1 at the model level. The proxy reasoning_effort
  injection stays as a safety net for any thinking model.
"""

import itertools
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

GROQ_BASE  = "https://api.groq.com/openai"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = int(os.environ.get("GROQ_PROXY_PORT", "18765"))

MAX_OUTPUT_TOKENS   = 768
PRE_TRIM_BYTES      = 18_000
MAX_MSG_CHARS       = 1_000
MAX_ARGS_CHARS      = 500
MAX_TOOL_DESC_CHARS = 80
MAX_TRIM_PASSES     = 4
THINKING_MODELS      = ("qwen/", "qwen3", "deepseek-r1")

MAX_SYS_CHARS_BY_PASS = {0: 2_500, 1: 1_500, 2: 900, 3: 500, 4: 300}
KEEP_MSGS_BY_PASS     = {0: 4,     1: 3,     2: 2,   3: 1,   4: 1}
KEEP_TOOLS_BY_PASS    = {0: 24,    1: 12,    2: 6,   3: 0,   4: 0}

_SOFT_413_PATTERNS = [
    "request entity too large",
    "payload too large",
    "context_length_exceeded",
    "maximum context length",
    "too many tokens",
    "tokens in your request exceeds",
    "input is too long",
    "reduce the length",
]

logging.basicConfig(
    level=logging.INFO,
    format="[groq-proxy %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("groq-proxy")


def _load_keys():
    keys = []
    for i in range(1, 20):
        k = os.environ.get(f"GROQ_KEY_{i}", "").strip()
        if k:
            keys.append(k)
    if not keys:
        raise RuntimeError("No Groq API keys. Set GROQ_KEY_1, GROQ_KEY_2, ...")
    return keys


_keys      = _load_keys()
_key_cycle = itertools.cycle(_keys)
_lock      = threading.Lock()
_req_count = 0
_req_lock  = threading.Lock()


def _next_key():
    with _lock:
        return next(_key_cycle)


_HOP_BY_HOP = frozenset([
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding",
    "upgrade", "authorization",
])


# ── Content helpers ────────────────────────────────────────────────────────────

def _trunc(text, limit):
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"...[{len(text)-limit}c cut]", True


def _content_size(c):
    if c is None:
        return 0
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        return sum(len((i.get("text") or "") if isinstance(i, dict) else "") for i in c)
    return len(str(c))


def _trunc_content(content, limit):
    if content is None:
        return content, False
    if isinstance(content, str):
        return _trunc(content, limit)
    if isinstance(content, list):
        parts, cut = [], False
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t, c = _trunc(item.get("text") or "", limit)
                cut = cut or c
                parts.append({**item, "text": t})
            else:
                parts.append(item)
        return parts, cut
    t, c = _trunc(str(content), limit)
    return t, c


def _trunc_tool_calls(tcs):
    if not isinstance(tcs, list):
        return tcs, False
    out, cut = [], False
    for tc in tcs:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        fn   = tc.get("function", {})
        args = fn.get("arguments", "")
        if isinstance(args, str) and len(args) > MAX_ARGS_CHARS:
            args = args[:MAX_ARGS_CHARS] + "...[args cut]"
            cut  = True
            fn   = {**fn, "arguments": args}
            tc   = {**tc, "function": fn}
        out.append(tc)
    return out, cut


def _strip_reasoning(msg):
    """Remove reasoning_content entirely — it is thinking-chain tokens, never needed in requests."""
    if "reasoning_content" not in msg:
        return msg, False
    rc = msg.get("reasoning_content")
    if not rc:
        return msg, False
    new_msg = {k: v for k, v in msg.items() if k != "reasoning_content"}
    return new_msg, True


def _trim_tools(tools, trim_pass):
    if not isinstance(tools, list) or not tools:
        return tools, False
    keep_n = KEEP_TOOLS_BY_PASS.get(trim_pass, 10)
    out, cut = [], False
    for t in tools:
        if not isinstance(t, dict):
            out.append(t)
            continue
        fn   = t.get("function") or t
        desc = fn.get("description") or ""
        if len(desc) > MAX_TOOL_DESC_CHARS:
            short = desc[:MAX_TOOL_DESC_CHARS] + "..."
            cut   = True
            if "function" in t:
                t = {**t, "function": {**fn, "description": short}}
            else:
                t = {**t, "description": short}
        out.append(t)
    if len(out) > keep_n:
        cut = True
        out = out[:keep_n]
    return out, cut


def _valid_tail_messages(conv_msgs, req_id, trim_pass):
    """Keep a valid OpenAI chat tail after aggressive trimming.

    Blindly keeping the last N messages can leave an orphan `tool` message with
    no preceding assistant tool call, which Groq rejects before it can answer.
    Drop leading orphan tool outputs; if nothing conversational remains, insert a
    tiny user nudge so Telegram still receives a response.
    """
    dropped = 0
    while conv_msgs and conv_msgs[0].get("role") == "tool":
        conv_msgs = conv_msgs[1:]
        dropped += 1
    if dropped:
        log.info("req#%d pass%d: dropped %d orphan leading tool msg(s)", req_id, trim_pass, dropped)
    if not conv_msgs:
        log.info("req#%d pass%d: inserted minimal user nudge after trimming", req_id, trim_pass)
        return [{"role": "user", "content": "Reply concisely to the latest Telegram message. Prior oversized tool output was omitted."}]
    return conv_msgs


# ── Trim payload ───────────────────────────────────────────────────────────────

def _trim(payload, trim_pass, req_id):
    msgs = payload.get("messages", [])
    if not isinstance(msgs, list):
        return payload

    sys_lim = MAX_SYS_CHARS_BY_PASS.get(trim_pass, 2_000)
    keep_n  = KEEP_MSGS_BY_PASS.get(trim_pass, 2)
    new_msgs = []
    rc_cut = cnt_cut = args_cut = sys_cut = 0

    for msg in msgs:
        role = msg.get("role", "?")

        # Strip reasoning_content (qwen3 / other thinking model output)
        msg, c = _strip_reasoning(msg)
        if c:
            rc_cut += 1

        if role == "system":
            nc, c = _trunc_content(msg.get("content"), sys_lim)
            if c:
                sys_cut += 1
            new_msgs.append({**msg, "content": nc})
        else:
            nc, c = _trunc_content(msg.get("content"), MAX_MSG_CHARS)
            if c:
                cnt_cut += 1
            nm = {**msg, "content": nc}
            if "tool_calls" in msg:
                ntc, c = _trunc_tool_calls(msg["tool_calls"])
                if c:
                    args_cut += 1
                nm = {**nm, "tool_calls": ntc}
            new_msgs.append(nm)

    if rc_cut or cnt_cut or args_cut or sys_cut:
        log.info(
            "req#%d pass%d: rc_stripped=%d content_cut=%d args_cut=%d sys_cut=%d",
            req_id, trim_pass, rc_cut, cnt_cut, args_cut, sys_cut,
        )

    sys_msgs  = [m for m in new_msgs if m.get("role") == "system"]
    conv_msgs = [m for m in new_msgs if m.get("role") != "system"]
    if len(conv_msgs) > keep_n:
        dropped   = len(conv_msgs) - keep_n
        conv_msgs = conv_msgs[-keep_n:]
        log.info("req#%d pass%d: dropped %d msgs, kept %d", req_id, trim_pass, dropped, keep_n)

    conv_msgs = _valid_tail_messages(conv_msgs, req_id, trim_pass)

    new_payload = {**payload, "messages": sys_msgs + conv_msgs}

    if "tools" in new_payload:
        nt, c = _trim_tools(new_payload["tools"], trim_pass)
        if c:
            log.info("req#%d pass%d: tools -> %d", req_id, trim_pass, len(nt))
        new_payload["tools"] = nt

    return new_payload


def _snapshot(payload, req_id, label):
    msgs  = payload.get("messages", [])
    tools = payload.get("tools", [])
    total = 0
    parts = []
    for m in msgs:
        role = m.get("role", "?")
        csz  = _content_size(m.get("content"))
        rcsz = len(str(m.get("reasoning_content") or ""))
        fmt  = "null" if m.get("content") is None else ("list" if isinstance(m.get("content"), list) else "str")
        tc   = f"+tc{len(m['tool_calls'])}" if m.get("tool_calls") else ""
        rc   = f"+RC{rcsz}" if rcsz else ""
        parts.append(f"{role}({fmt},{csz}{tc}{rc})")
        total += csz + rcsz
    tool_chars = sum(len(str((t.get("function") or t).get("description") or "")) for t in tools if isinstance(t, dict))
    log.info("req#%d %s: %d msgs %d tools ~%dc+%dc(tools) | %s",
             req_id, label, len(msgs), len(tools), total, tool_chars, " ".join(parts))


# ── Prepare request ────────────────────────────────────────────────────────────

def _is_thinking_model(model):
    m = str(model or "").lower()
    return any(marker in m for marker in THINKING_MODELS)


def _prepare(body_bytes, req_id):
    if not body_bytes:
        return body_bytes
    try:
        payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return body_bytes
    if "messages" not in payload:
        return body_bytes

    # Hermes adds Ollama-only `options` when ollama_num_ctx is configured. Groq's
    # OpenAI-compatible API rejects that field with HTTP 400 `property 'options'
    # is unsupported`, which Hermes treats as a provider failure and retries.
    if "options" in payload:
        payload.pop("options", None)
        log.info("req#%d: stripped unsupported Ollama options", req_id)

    # Only thinking models need reasoning suppression. Do not send
    # reasoning_effort to ordinary Groq chat models; some providers reject
    # unknown/unsupported top-level fields.
    if _is_thinking_model(payload.get("model")):
        if payload.get("reasoning_effort") != "none":
            payload["reasoning_effort"] = "none"
            log.info("req#%d: injected reasoning_effort=none (thinking disabled)", req_id)
    else:
        if "reasoning_effort" in payload:
            payload.pop("reasoning_effort", None)
            log.info("req#%d: removed reasoning_effort for non-reasoning model", req_id)

    # Cap max_tokens
    mt = payload.get("max_tokens")
    if mt is None or (isinstance(mt, int) and mt > MAX_OUTPUT_TOKENS):
        payload["max_tokens"] = MAX_OUTPUT_TOKENS
        log.info("req#%d: max_tokens %s->%d", req_id, repr(mt), MAX_OUTPUT_TOKENS)

    _snapshot(payload, req_id, "incoming")

    # Always run pass-0: strips reasoning_content + trims tools descriptions
    payload = _trim(payload, 0, req_id)

    body_out = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # Groq free-tier/org token-per-minute limits can be far below the model's
    # 128K context window.  Do not wait for an upstream 413 before shrinking:
    # keep the serialized request under a conservative byte budget so fallback
    # and compression calls do not receive the same oversized payload.
    trim_pass = 0
    while len(body_out) > PRE_TRIM_BYTES and trim_pass < MAX_TRIM_PASSES:
        trim_pass += 1
        log.warning(
            "req#%d: body %d bytes exceeds safe budget %d -> proactive trim pass %d/%d",
            req_id, len(body_out), PRE_TRIM_BYTES, trim_pass, MAX_TRIM_PASSES,
        )
        payload = _trim(payload, trim_pass, req_id)
        body_out = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        _snapshot(payload, req_id, f"after-proactive-trim{trim_pass}")

    if len(body_out) > PRE_TRIM_BYTES:
        log.warning("req#%d: body still %d bytes after proactive trims", req_id, len(body_out))
    return body_out


# ── Handler ────────────────────────────────────────────────────────────────────

class GroqProxyHandler(BaseHTTPRequestHandler):
    server_version = "HermesGroqProxy/5.0"

    def log_message(self, fmt, *args):
        pass

    def _is_too_large(self, status, body_text):
        if status == 413:
            return True
        if status in (400, 422):
            bl = body_text.lower()
            return any(p in bl for p in _SOFT_413_PATTERNS)
        return False

    def _forward(self):
        global _req_count
        with _req_lock:
            _req_count += 1
            req_id = _req_count

        length   = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(length) if length > 0 else None
        ctype    = self.headers.get("Content-Type", "application/json")
        is_chat  = "/chat/completions" in self.path

        fwd = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP}
        fwd["Content-Type"] = ctype
        fwd["User-Agent"]   = "hermes-groq-proxy/5.0"
        target = GROQ_BASE + self.path

        current_body = _prepare(raw_body, req_id) if (is_chat and raw_body) else raw_body

        for trim_pass in range(MAX_TRIM_PASSES + 1):
            if trim_pass > 0:
                log.warning("req#%d: too-large -> reactive trim pass %d/%d", req_id, trim_pass, MAX_TRIM_PASSES)
                if is_chat and current_body:
                    try:
                        p            = json.loads(current_body.decode("utf-8", errors="replace"))
                        p            = _trim(p, trim_pass, req_id)
                        current_body = json.dumps(p, ensure_ascii=False).encode("utf-8")
                        _snapshot(p, req_id, f"after-trim{trim_pass}")
                        log.info("req#%d: body now %d bytes", req_id, len(current_body))
                    except Exception as e:
                        log.warning("req#%d: trim error: %s", req_id, e)
                        self._err(413, "Payload too large - trim failed")
                        return
                else:
                    self._err(413, "Cannot trim non-chat or empty body")
                    return

            too_large = False
            for attempt in range(len(_keys)):
                key  = _next_key()
                hint = f"{key[:8]}...{key[-4:]}"
                fwd["Authorization"]  = f"Bearer {key}"
                fwd["Content-Length"] = str(len(current_body) if current_body else 0)
                log.info("req#%d %s key=%s pass=%d attempt=%d body=%db",
                         req_id, self.path, hint, trim_pass, attempt+1,
                         len(current_body) if current_body else 0)
                try:
                    resp = requests.request(
                        method=self.command, url=target, headers=fwd,
                        data=current_body, stream=True, timeout=180,
                    )
                except requests.RequestException as e:
                    log.warning("req#%d upstream: %s", req_id, e)
                    if attempt < len(_keys) - 1:
                        continue
                    self._err(503, f"Upstream failed: {e}")
                    return

                if resp.status_code == 429 and attempt < len(_keys) - 1:
                    log.warning("req#%d: 429 on %s - rotating", req_id, hint)
                    try:
                        resp.close()
                    except Exception:
                        pass
                    continue

                if resp.status_code in (400, 413, 422):
                    try:
                        err_body = b"".join(resp.iter_content(8192)).decode("utf-8", errors="replace")
                        log.warning("req#%d: Groq %d: %s", req_id, resp.status_code, err_body[:800])
                    except Exception:
                        err_body = ""
                    try:
                        resp.close()
                    except Exception:
                        pass
                    if self._is_too_large(resp.status_code, err_body):
                        too_large = True
                        break
                    self._raw(resp.status_code, err_body.encode("utf-8"))
                    return

                self._stream(resp, req_id)
                return

            if not too_large:
                self._err(429, "All keys exhausted")
                return

        log.error("req#%d: all %d passes exhausted - 413", req_id, MAX_TRIM_PASSES)
        self._err(413, f"Too large after {MAX_TRIM_PASSES} passes - see /tmp/groq-proxy.log")

    def _stream(self, resp, req_id):
        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() in ("transfer-encoding", "connection", "keep-alive", "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        n = 0
        try:
            for chunk in resp.iter_content(chunk_size=512):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    n += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass
        log.info("req#%d: HTTP %d %d bytes", req_id, resp.status_code, n)

    def _raw(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._raw(code, json.dumps({"error": {"message": msg, "type": "proxy_error"}}).encode())

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _forward


class ThreadedHTTPServer(HTTPServer):
    def process_request(self, request, client_address):
        threading.Thread(target=self._ht, args=(request, client_address), daemon=True).start()

    def _ht(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            pass
        finally:
            self.shutdown_request(request)


if __name__ == "__main__":
    log.info("=" * 65)
    log.info("Groq Proxy v5 -- 413 Eradicator Edition")
    log.info("Listening : %s:%d -> %s", PROXY_HOST, PROXY_PORT, GROQ_BASE)
    log.info("Keys      : %d loaded", len(_keys))
    for i, k in enumerate(_keys, 1):
        log.info("  [%d] %s...%s", i, k[:8], k[-4:])
    log.info("Fixes (v5 vs v4):")
    log.info("  reasoning_effort=none  -> only for thinking model requests")
    log.info("  reasoning_content strip -> stored CoT tokens removed from history")
    log.info("  tools trimmed          -> descriptions capped, excess tools dropped")
    log.info("  options stripped       -> Groq never receives Ollama-only options")
    log.info("  soft-413 detect        -> Groq 400 context errors trigger trim+retry")
    log.info("Config:")
    log.info("  safe budget   : body <= %d bytes before upstream", PRE_TRIM_BYTES)
    log.info("  per-msg       : %d chars", MAX_MSG_CHARS)
    log.info("  tool desc     : %d chars", MAX_TOOL_DESC_CHARS)
    log.info("  sys/pass      : %s", MAX_SYS_CHARS_BY_PASS)
    log.info("  msgs/pass     : %s", KEEP_MSGS_BY_PASS)
    log.info("  tools/pass    : %s", KEEP_TOOLS_BY_PASS)
    log.info("=" * 65)
    ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), GroqProxyHandler).serve_forever()
