#!/usr/bin/env python3
"""
Thread-safe Groq API key rotation proxy for Hermes agent.
413 Context Guardian Edition — v4.

ROOT CAUSE TIMELINE
===================
v1 (broken — May 2026 run #249):
  • Trimmed only by MESSAGE COUNT, never by content size.
  • Returned None immediately when conv_msgs <= 2 (fresh session → no trim → 413).
  • System prompt never touched.
  • No max_tokens injection.

v2 (broken — same run, same code as v1, just different threshold):
  • Same bugs as v1.

v3 (still broken — run #252):
  • Fixed max_tokens injection. ✓
  • Fixed pre-flight trim on large body. ✓
  • Truncated string-format content. ✓
  • MISSED: content in LIST format (OpenAI structured content) was silently SKIPPED.
    Any message with content=[{"type":"text","text":"..."}] passed through untouched.
    Hermes sends tool results (bash, browser outputs) and sometimes system messages
    in list format. A single nmap/gobuster result stored as list-format content bypasses
    all truncation and keeps triggering Groq 413.
  • MISSED: content=None (assistant messages that only have tool_calls) caused
    `_truncate(None, ...)` to fail silently or pass through a None.

v4 (this version — definitive fix):
  • Handles ALL three content formats for every message role:
      - content: None (or missing) → leave as-is
      - content: "string" → truncate to limit
      - content: [{"type":"text","text":"..."}, ...] → truncate the "text" field
        in each list item; non-text items (image_url, etc.) are kept as-is
  • Handles `tool_calls` array in assistant messages:
      - Each function call's `arguments` string truncated to MAX_ARGS_CHARS
  • Added per-request diagnostic log line showing every message role, content
    format, and byte size — so future failures are immediately diagnosable.
  • PRE_TRIM_BYTES lowered from 300KB to 100KB — ensures pre-flight fires for
    realistic Hermes payloads (skills + SOUL.md + bash outputs often reach 50-150KB)
  • Reactive passes now lower MAX_SYS_CHARS further per pass (10K→5K→2K)
    so extremely large skill payloads are also handled.
"""

import itertools
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# ── Groq endpoint ──────────────────────────────────────────────────────────────
GROQ_BASE = "https://api.groq.com/openai"   # self.path starts with /v1/...

# ── Proxy config ───────────────────────────────────────────────────────────────
PROXY_HOST = "127.0.0.1"
PROXY_PORT = int(os.environ.get("GROQ_PROXY_PORT", "18765"))

# ── Context guardian constants ─────────────────────────────────────────────────
MAX_OUTPUT_TOKENS = 4096      # injected if unset or larger
PRE_TRIM_BYTES    = 100_000   # pre-flight trim threshold (100 KB — catches realistic Hermes payloads)
MAX_MSG_CHARS     = 5_000     # per-message string content limit
MAX_ARGS_CHARS    = 2_000     # per-tool-call arguments limit
MAX_SYS_CHARS_BY_PASS = {     # system message limit per pass (gets more aggressive)
    0: 10_000,   # as-prepared (pre-flight or pass-0)
    1: 10_000,   # reactive pass 1
    2:  5_000,   # reactive pass 2 — more aggressive
    3:  2_000,   # reactive pass 3 — most aggressive
}
KEEP_MSGS_BY_PASS = {
    0: 12,
    1:  8,
    2:  4,
    3:  2,
}
MAX_TRIM_PASSES = 3

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level    = logging.INFO,
    format   = "[groq-proxy %(asctime)s] %(message)s",
    datefmt  = "%H:%M:%S",
    stream   = sys.stdout,
    force    = True,
)
log = logging.getLogger("groq-proxy")

# ── Key management ─────────────────────────────────────────────────────────────
def _load_keys() -> list:
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

_request_count = 0
_req_lock      = threading.Lock()

def _next_key() -> str:
    with _lock:
        return next(_key_cycle)

_HOP_BY_HOP = frozenset([
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding",
    "upgrade", "authorization",
])

# ── Content helpers ────────────────────────────────────────────────────────────

def _truncate_str(text: str, limit: int) -> tuple:
    """Return (truncated_text, was_truncated)."""
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n[…{len(text) - limit} chars trimmed by proxy]", True


def _content_size(content) -> int:
    """Return the best estimate of a content field's character size."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for item in content:
            if isinstance(item, dict):
                total += len(item.get("text", "") or "")
        return total
    return len(str(content))


def _truncate_content(content, limit: int, role: str) -> tuple:
    """
    Truncate a message content field regardless of its format.
    Handles:
      • None / missing         → returned unchanged
      • str                    → simple truncation
      • list of content parts  → truncate "text" fields in each part
    Returns (new_content, was_truncated).
    """
    if content is None:
        return content, False

    if isinstance(content, str):
        return _truncate_str(content, limit)

    if isinstance(content, list):
        new_parts = []
        cut_any   = False
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text") or ""
                trimmed, cut = _truncate_str(text, limit)
                cut_any = cut_any or cut
                new_parts.append({**item, "text": trimmed})
            else:
                # image_url, audio, etc. — keep as-is
                new_parts.append(item)
        return new_parts, cut_any

    # Unexpected type — convert to string and truncate
    s = str(content)
    trimmed, cut = _truncate_str(s, limit)
    return trimmed, cut


def _truncate_tool_calls(tool_calls: list) -> tuple:
    """
    Truncate the `arguments` string in each tool_call.
    Returns (new_tool_calls, was_truncated).
    """
    if not isinstance(tool_calls, list):
        return tool_calls, False
    new_calls = []
    cut_any   = False
    for tc in tool_calls:
        if not isinstance(tc, dict):
            new_calls.append(tc)
            continue
        fn = tc.get("function", {})
        args = fn.get("arguments", "")
        if isinstance(args, str) and len(args) > MAX_ARGS_CHARS:
            args    = args[:MAX_ARGS_CHARS] + "…[args trimmed]"
            cut_any = True
            fn      = {**fn, "arguments": args}
            tc      = {**tc, "function": fn}
        new_calls.append(tc)
    return new_calls, cut_any


# ── Trim payload ───────────────────────────────────────────────────────────────

def _trim_payload(payload: dict, trim_pass: int, req_id: int) -> dict:
    """
    Return a new payload dict with reduced message sizes.

    Applied layers (cumulative, applied every pass):
      L1: Truncate every non-system message content (string OR list) to MAX_MSG_CHARS.
      L2: Truncate tool_calls.function.arguments in assistant messages.
      L3: Truncate system message content (string OR list) to MAX_SYS_CHARS_BY_PASS[trim_pass].
      L4: Drop oldest non-system messages based on KEEP_MSGS_BY_PASS[trim_pass].
    """
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return payload

    sys_limit  = MAX_SYS_CHARS_BY_PASS.get(trim_pass, 2_000)
    keep_n     = KEEP_MSGS_BY_PASS.get(trim_pass, 2)
    new_msgs   = []
    l1_count = l2_count = l3_count = 0

    for msg in messages:
        role    = msg.get("role", "unknown")
        content = msg.get("content")

        if role == "system":
            # L3: system message (string or list)
            new_content, cut = _truncate_content(content, sys_limit, role)
            if cut:
                l3_count += 1
                log.info(
                    "req#%d pass%d L3: system %d→%d chars (limit=%d)",
                    req_id, trim_pass, _content_size(content), _content_size(new_content), sys_limit,
                )
            new_msgs.append({**msg, "content": new_content})
        else:
            # L1: truncate content
            new_content, cut1 = _truncate_content(content, MAX_MSG_CHARS, role)
            if cut1:
                l1_count += 1
                log.info(
                    "req#%d pass%d L1: %s content %d→%d chars",
                    req_id, trim_pass, role, _content_size(content), _content_size(new_content),
                )
            new_msg = {**msg, "content": new_content}

            # L2: truncate tool_calls arguments
            if "tool_calls" in msg:
                new_tc, cut2 = _truncate_tool_calls(msg["tool_calls"])
                if cut2:
                    l2_count += 1
                    log.info("req#%d pass%d L2: %s tool_calls trimmed", req_id, trim_pass, role)
                new_msg = {**new_msg, "tool_calls": new_tc}

            new_msgs.append(new_msg)

    if l1_count or l2_count or l3_count:
        log.info(
            "req#%d pass%d content trim: L1=%d L2=%d L3=%d",
            req_id, trim_pass, l1_count, l2_count, l3_count,
        )

    # L4: drop oldest non-system messages
    sys_msgs  = [m for m in new_msgs if m.get("role") == "system"]
    conv_msgs = [m for m in new_msgs if m.get("role") != "system"]

    if len(conv_msgs) > keep_n:
        dropped   = len(conv_msgs) - keep_n
        conv_msgs = conv_msgs[-keep_n:]
        log.info(
            "req#%d pass%d L4: dropped %d oldest non-system msg(s), kept %d",
            req_id, trim_pass, dropped, keep_n,
        )

    return {**payload, "messages": sys_msgs + conv_msgs}


# ── Diagnostic per-request log ─────────────────────────────────────────────────

def _log_request_snapshot(payload: dict, req_id: int, label: str):
    """Log a compact snapshot of the payload for diagnostics."""
    messages = payload.get("messages", [])
    total_chars = 0
    parts = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        csize = _content_size(content)
        cfmt  = "null" if content is None else ("list" if isinstance(content, list) else "str")
        tc    = f"+tc{len(m['tool_calls'])}" if m.get("tool_calls") else ""
        parts.append(f"{role}({cfmt},{csize}c{tc})")
        total_chars += csize
    log.info(
        "req#%d %s: %d msgs, ~%d chars total | %s",
        req_id, label, len(messages), total_chars, " | ".join(parts),
    )


# ── Request preparation ────────────────────────────────────────────────────────

def _prepare_request(body_bytes: bytes, req_id: int):
    """
    Prepare every chat-completions request before forwarding:
      1. Parse JSON.
      2. Log diagnostic snapshot (role/format/size per message).
      3. Inject/cap max_tokens.
      4. Pre-flight trim if body > PRE_TRIM_BYTES.
    """
    if not body_bytes:
        return body_bytes, False

    try:
        payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return body_bytes, False

    if "messages" not in payload:
        return body_bytes, False

    # Log snapshot BEFORE any modification
    _log_request_snapshot(payload, req_id, "incoming")

    modified = False

    # 1. Inject / cap max_tokens
    current_mt = payload.get("max_tokens")
    if current_mt is None or (isinstance(current_mt, int) and current_mt > MAX_OUTPUT_TOKENS):
        payload["max_tokens"] = MAX_OUTPUT_TOKENS
        modified = True
        log.info("req#%d: max_tokens %s→%d", req_id, repr(current_mt), MAX_OUTPUT_TOKENS)

    # 2. Pre-flight trim
    body_after_mt = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body_after_mt) > PRE_TRIM_BYTES:
        log.warning(
            "req#%d: pre-flight body %d bytes > %d — trimming (pass=0)",
            req_id, len(body_after_mt), PRE_TRIM_BYTES,
        )
        payload  = _trim_payload(payload, trim_pass=0, req_id=req_id)
        modified = True
        _log_request_snapshot(payload, req_id, "after-preflight")

    if not modified:
        return body_bytes, False

    return json.dumps(payload, ensure_ascii=False).encode("utf-8"), True


# ── Request handler ────────────────────────────────────────────────────────────

class GroqProxyHandler(BaseHTTPRequestHandler):
    server_version = "HermesGroqProxy/4.0"

    def log_message(self, fmt, *args):
        pass  # suppress default httpd noise

    def _forward(self):
        global _request_count
        with _req_lock:
            _request_count += 1
            req_id = _request_count

        length       = int(self.headers.get("Content-Length", 0) or 0)
        raw_body     = self.rfile.read(length) if length > 0 else None
        content_type = self.headers.get("Content-Type", "application/json")
        is_chat      = "/chat/completions" in self.path

        fwd_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        fwd_headers["Content-Type"] = content_type
        fwd_headers["User-Agent"]   = "hermes-groq-proxy/4.0"

        target_url = GROQ_BASE + self.path

        # Prepare (diagnostic log + max_tokens + optional pre-flight trim)
        if is_chat and raw_body:
            current_body, was_prepared = _prepare_request(raw_body, req_id)
            if was_prepared:
                log.info("req#%d: prepared body %d→%d bytes", req_id, len(raw_body), len(current_body))
        else:
            current_body = raw_body

        # Outer loop: reactive trim passes (0 = as-prepared, 1-3 = after 413)
        for trim_pass in range(MAX_TRIM_PASSES + 1):
            if trim_pass > 0:
                log.warning("req#%d: 413 received — reactive trim pass %d/%d", req_id, trim_pass, MAX_TRIM_PASSES)
                if is_chat and current_body:
                    try:
                        payload      = json.loads(current_body.decode("utf-8", errors="replace"))
                        payload      = _trim_payload(payload, trim_pass=trim_pass, req_id=req_id)
                        current_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        _log_request_snapshot(payload, req_id, f"after-reactive-trim-{trim_pass}")
                        log.info("req#%d: reactive trim pass %d: body now %d bytes", req_id, trim_pass, len(current_body))
                    except Exception as exc:
                        log.warning("req#%d: reactive trim parse error: %s", req_id, exc)
                        self._send_error(413, "Payload too large — trim parse failed")
                        return
                else:
                    self._send_error(413, "Request payload too large — not a chat request or empty body")
                    return

            # Inner loop: key rotation (handles 429)
            max_tries = len(_keys)
            got_413   = False

            for attempt in range(max_tries):
                api_key  = _next_key()
                key_hint = f"{api_key[:8]}...{api_key[-4:]}"
                fwd_headers["Authorization"]  = f"Bearer {api_key}"
                fwd_headers["Content-Length"] = str(len(current_body) if current_body else 0)

                log.info(
                    "req#%d %s %s key=%s trim=%d attempt=%d/%d body=%db",
                    req_id, self.command, self.path, key_hint,
                    trim_pass, attempt + 1, max_tries,
                    len(current_body) if current_body else 0,
                )

                try:
                    resp = requests.request(
                        method  = self.command,
                        url     = target_url,
                        headers = fwd_headers,
                        data    = current_body,
                        stream  = True,
                        timeout = 180,
                    )
                except requests.RequestException as exc:
                    log.warning("req#%d upstream error: %s", req_id, exc)
                    if attempt < max_tries - 1:
                        continue
                    self._send_error(503, f"Upstream connection failed: {exc}")
                    return

                if resp.status_code == 429 and attempt < max_tries - 1:
                    log.warning("req#%d: 429 on key %s — rotating", req_id, key_hint)
                    try:
                        resp.close()
                    except Exception:
                        pass
                    continue

                if resp.status_code == 413:
                    try:
                        err_body = b"".join(resp.iter_content(8192)).decode("utf-8", errors="replace")
                        log.warning("req#%d: Groq 413 body: %s", req_id, err_body[:500])
                    except Exception:
                        pass
                    try:
                        resp.close()
                    except Exception:
                        pass
                    got_413 = True
                    break  # exit key loop → next trim pass

                # Success or other error code — stream back
                self._stream_response(resp, req_id)
                return

            if not got_413:
                self._send_error(429, "All API keys exhausted after rate-limit retries")
                return

        # All trim passes exhausted
        log.error(
            "req#%d: all %d trim passes exhausted — returning 413. "
            "Check snapshot logs above for content format/size.",
            req_id, MAX_TRIM_PASSES,
        )
        self._send_error(
            413,
            f"Request too large after {MAX_TRIM_PASSES} trim passes. "
            f"Check /tmp/groq-proxy.log for message size diagnostics.",
        )

    def _stream_response(self, resp, req_id):
        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() in ("transfer-encoding", "connection", "keep-alive", "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()

        bytes_sent = 0
        try:
            for chunk in resp.iter_content(chunk_size=512):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    bytes_sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            log.debug("req#%d: client disconnected mid-stream", req_id)
        finally:
            try:
                resp.close()
            except Exception:
                pass

        log.info("req#%d: done HTTP %d (%d bytes)", req_id, resp.status_code, bytes_sent)

    def _send_error(self, code, msg):
        body = json.dumps({"error": {"message": msg, "type": "proxy_error"}}).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection",     "close")
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _forward


# ── Threaded server ────────────────────────────────────────────────────────────

class ThreadedHTTPServer(HTTPServer):
    def process_request(self, request, client_address):
        t = threading.Thread(
            target = self._handle_thread,
            args   = (request, client_address),
            daemon = True,
        )
        t.start()

    def _handle_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            pass
        finally:
            self.shutdown_request(request)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("=" * 65)
    log.info("Groq API Key Rotation Proxy  v4 — Context Guardian Edition")
    log.info("Listening  : %s:%d", PROXY_HOST, PROXY_PORT)
    log.info("Target     : %s", GROQ_BASE)
    log.info("Keys       : %d loaded (round-robin)", len(_keys))
    for i, k in enumerate(_keys, 1):
        log.info("  [%d] %s...%s", i, k[:8], k[-4:])
    log.info("Protections:")
    log.info("  max_tokens cap  : %d", MAX_OUTPUT_TOKENS)
    log.info("  pre-flight trim : body > %d bytes", PRE_TRIM_BYTES)
    log.info("  per-msg limit   : %d chars (string OR list format)", MAX_MSG_CHARS)
    log.info("  args limit      : %d chars per tool_call", MAX_ARGS_CHARS)
    log.info("  sys limit/pass  : %s", MAX_SYS_CHARS_BY_PASS)
    log.info("  keep msgs/pass  : %s", KEEP_MSGS_BY_PASS)
    log.info("  reactive passes : %d", MAX_TRIM_PASSES)
    log.info("  content formats : string + list[{type,text}] + None")
    log.info("=" * 65)
    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), GroqProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Proxy shutting down.")
