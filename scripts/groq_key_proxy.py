#!/usr/bin/env python3
"""
Thread-safe Groq API key rotation proxy for Hermes agent.
413 Context Guardian Edition — v3.

WHAT THIS PROXY DOES
====================
1. Key rotation: GROQ_KEY_1..N round-robin, 429 skips to next key.
2. max_tokens injection: every chat request gets max_tokens=MAX_OUTPUT_TOKENS
   if unset or larger, so Groq always has a clear output-budget and the remaining
   context window is reserved for input (critical for preventing 413).
3. Pre-flight trim: if the request body exceeds PRE_TRIM_BYTES the payload is
   trimmed BEFORE sending — prevents 413 proactively.
4. 413 reactive trim: if Groq still returns 413, apply up to MAX_TRIM_PASSES of
   increasingly aggressive trimming and retry silently.
5. All trim operations are applied in layers:
     Layer 1 — truncate any individual message content > MAX_MSG_CHARS
     Layer 2 — truncate the system prompt > MAX_SYS_CHARS (handles big SOUL.md/skills)
     Layer 3 — drop oldest non-system messages (keep last KEEP_MSGS_PASS1)
     Layer 4 — drop more, keep last KEEP_MSGS_PASS2
     Layer 5 — drop all but system + last 2 messages

ROOT CAUSE ANALYSIS (v1 fix failure — May 2026)
================================================
v1 trimmed only by MESSAGE COUNT. Two bugs:
  Bug A — content never truncated: a single bash tool result (nmap, gobuster etc.)
    can be megabytes. Removing old messages while keeping a 2MB bash result does nothing.
  Bug B — early return on empty conv_msgs: when conv_msgs <= 2 the trim returned
    None immediately. Fresh session = 0 conv messages → trim does nothing → 413
    forwarded to Hermes → Hermes compression loop fires (and also 413s) → session reset.
  Bug C — system prompt never touched: SOUL.md grows over sessions as Hermes edits it.
    Skills injected without limit. System prompt alone can exceed Groq's input budget.
  Bug D — no max_tokens injection: without a declared output budget Groq may refuse
    inputs that leave it insufficient space to generate a response.

This version fixes all four bugs.
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
# Max tokens reserved for model output. Lower = more room for input.
# At 4096 Groq accepts up to 131072-4096=127K input tokens (~508KB text).
MAX_OUTPUT_TOKENS = 4096

# Pre-flight trim: if body exceeds this, trim BEFORE sending to Groq.
# 300 KB of JSON ≈ 75K tokens — well inside Groq's window after output reservation.
PRE_TRIM_BYTES = 300_000

# Per-message content limit (chars). Prevents a single huge bash/browser result
# from filling the entire context. ~5K chars ≈ 1250 tokens.
MAX_MSG_CHARS = 5_000

# System message content limit (chars). Hermes injects SOUL.md + skills here.
# 10K chars ≈ 2500 tokens — enough for identity + rules + compact memory.
MAX_SYS_CHARS = 10_000

# Number of non-system messages to keep in each trim pass.
KEEP_MSGS_PASS1 = 8    # pass 1 (reactive)
KEEP_MSGS_PASS2 = 4    # pass 2 (reactive)
KEEP_MSGS_PASS3 = 2    # pass 3 (reactive, most aggressive)

MAX_TRIM_PASSES = 3    # reactive passes after 413

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[groq-proxy %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,
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
        raise RuntimeError(
            "No Groq API keys found. Set GROQ_KEY_1, GROQ_KEY_2, ... env vars."
        )
    return keys


_keys      = _load_keys()
_key_cycle = itertools.cycle(_keys)
_lock      = threading.Lock()

_request_count = 0
_request_lock  = threading.Lock()


def _next_key() -> str:
    with _lock:
        return next(_key_cycle)


_HOP_BY_HOP = frozenset([
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding",
    "upgrade", "authorization",
])

# ── Payload helpers ────────────────────────────────────────────────────────────

def _truncate(text: str, limit: int, label: str) -> tuple:
    """Return (truncated_text, was_truncated)."""
    if len(text) <= limit:
        return text, False
    dropped = len(text) - limit
    return text[:limit] + f"\n[…{dropped} chars truncated by proxy]", True


def _trim_payload(payload: dict, trim_pass: int, req_id: int) -> dict:
    """
    Return a new payload dict with reduced message sizes.

    Applied layers (all in every pass — cumulative effect):
      L1: Truncate every non-system message content to MAX_MSG_CHARS
      L2: Truncate every system message content to MAX_SYS_CHARS
      L3: Drop oldest non-system messages based on trim_pass number
    """
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return payload

    new_msgs = []
    l1_count = l2_count = 0

    for msg in messages:
        role    = msg.get("role", "")
        content = msg.get("content", "")
        if not isinstance(content, str):
            new_msgs.append(msg)
            continue

        if role == "system":
            # Layer 2: truncate system prompt
            trimmed, cut = _truncate(content, MAX_SYS_CHARS, "system")
            if cut:
                l2_count += 1
                log.info(
                    "req#%d trim-pass%d L2: system msg %d→%d chars",
                    req_id, trim_pass, len(content), MAX_SYS_CHARS,
                )
            new_msgs.append({**msg, "content": trimmed})
        else:
            # Layer 1: truncate conversation/tool messages
            trimmed, cut = _truncate(content, MAX_MSG_CHARS, role)
            if cut:
                l1_count += 1
                log.info(
                    "req#%d trim-pass%d L1: %s msg %d→%d chars",
                    req_id, trim_pass, role, len(content), MAX_MSG_CHARS,
                )
            new_msgs.append({**msg, "content": trimmed})

    if l1_count or l2_count:
        log.info(
            "req#%d trim-pass%d content: %d msg(s) truncated, %d sys msg(s) truncated",
            req_id, trim_pass, l1_count, l2_count,
        )

    # Layer 3: drop oldest non-system messages
    keep_map = {1: KEEP_MSGS_PASS1, 2: KEEP_MSGS_PASS2, 3: KEEP_MSGS_PASS3}
    keep_n   = keep_map.get(trim_pass, KEEP_MSGS_PASS3)

    sys_msgs  = [m for m in new_msgs if m.get("role") == "system"]
    conv_msgs = [m for m in new_msgs if m.get("role") != "system"]

    if len(conv_msgs) > keep_n:
        dropped = len(conv_msgs) - keep_n
        conv_msgs = conv_msgs[-keep_n:]
        log.info(
            "req#%d trim-pass%d L3: dropped %d oldest non-system msg(s), kept %d",
            req_id, trim_pass, dropped, keep_n,
        )

    new_payload = {**payload, "messages": sys_msgs + conv_msgs}
    return new_payload


def _prepare_request(body_bytes: bytes, req_id: int):
    """
    Prepare every chat-completions request before forwarding:
      1. Parse JSON (returns original bytes on failure).
      2. Inject/cap max_tokens to MAX_OUTPUT_TOKENS.
      3. Pre-flight trim if body > PRE_TRIM_BYTES.

    Returns (prepared_bytes, was_modified).
    """
    if not body_bytes:
        return body_bytes, False

    try:
        payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return body_bytes, False

    if "messages" not in payload:
        return body_bytes, False

    modified = False

    # 1. Inject / cap max_tokens
    current_mt = payload.get("max_tokens")
    if current_mt is None or (isinstance(current_mt, int) and current_mt > MAX_OUTPUT_TOKENS):
        payload["max_tokens"] = MAX_OUTPUT_TOKENS
        modified = True
        log.info(
            "req#%d: max_tokens %s→%d",
            req_id, repr(current_mt), MAX_OUTPUT_TOKENS,
        )

    # 2. Pre-flight trim
    body_after_mt = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body_after_mt) > PRE_TRIM_BYTES:
        log.warning(
            "req#%d: pre-flight body %d bytes > %d threshold — pre-trimming",
            req_id, len(body_after_mt), PRE_TRIM_BYTES,
        )
        payload  = _trim_payload(payload, trim_pass=1, req_id=req_id)
        modified = True

    if not modified:
        return body_bytes, False

    return json.dumps(payload, ensure_ascii=False).encode("utf-8"), True


# ── Request handler ────────────────────────────────────────────────────────────

class GroqProxyHandler(BaseHTTPRequestHandler):
    server_version = "HermesGroqProxy/3.0"

    def log_message(self, fmt, *args):
        pass  # suppress default noise

    def _forward(self):
        global _request_count
        with _request_lock:
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
        fwd_headers["User-Agent"]   = "hermes-groq-proxy/3.0"

        target_url = GROQ_BASE + self.path

        # Prepare (inject max_tokens + optional pre-flight trim)
        if is_chat and raw_body:
            current_body, was_prepared = _prepare_request(raw_body, req_id)
            if was_prepared:
                log.info(
                    "req#%d: prepared body %d→%d bytes",
                    req_id, len(raw_body), len(current_body),
                )
        else:
            current_body = raw_body

        # Outer loop: trim passes (0 = as-prepared, 1-3 = reactive after 413)
        for trim_pass in range(MAX_TRIM_PASSES + 1):
            if trim_pass > 0:
                # Reactive: Groq returned 413 on previous pass — trim more
                log.warning(
                    "req#%d: 413 received — reactive trim pass %d/%d",
                    req_id, trim_pass, MAX_TRIM_PASSES,
                )
                if is_chat and current_body:
                    try:
                        payload = json.loads(current_body.decode("utf-8", errors="replace"))
                        payload = _trim_payload(payload, trim_pass=trim_pass, req_id=req_id)
                        current_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        log.info(
                            "req#%d: after reactive trim pass %d: %d bytes",
                            req_id, trim_pass, len(current_body),
                        )
                    except Exception as exc:
                        log.warning("req#%d: reactive trim parse error: %s", req_id, exc)
                        self._send_error(413, "Payload too large — trim parse failed")
                        return
                else:
                    self._send_error(
                        413,
                        "Request payload too large — cannot reduce non-chat or empty body",
                    )
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
                        method   = self.command,
                        url      = target_url,
                        headers  = fwd_headers,
                        data     = current_body,
                        stream   = True,
                        timeout  = 180,
                    )
                except requests.RequestException as exc:
                    log.warning("req#%d upstream error: %s", req_id, exc)
                    if attempt < max_tries - 1:
                        continue
                    self._send_error(503, f"Upstream connection failed: {exc}")
                    return

                if resp.status_code == 429 and attempt < max_tries - 1:
                    log.warning(
                        "req#%d: 429 on key %s — rotating", req_id, key_hint
                    )
                    try:
                        resp.close()
                    except Exception:
                        pass
                    continue

                if resp.status_code == 413:
                    # Read + log Groq's error body for diagnostics
                    try:
                        err_body = b"".join(resp.iter_content(8192)).decode("utf-8", errors="replace")
                        log.warning(
                            "req#%d: Groq 413 body: %s", req_id, err_body[:400]
                        )
                    except Exception:
                        pass
                    try:
                        resp.close()
                    except Exception:
                        pass
                    got_413 = True
                    break   # exit key loop → go to next trim pass

                # Success or unhandled error code — stream back to caller
                self._stream_response(resp, req_id)
                return

            if not got_413:
                # Key loop exhausted without 413 → all keys rate-limited
                self._send_error(429, "All API keys exhausted after rate-limit retries")
                return
            # got_413=True → continue outer loop for next trim pass

        # All trim passes exhausted → return 413 to Hermes
        log.error(
            "req#%d: all %d trim passes exhausted — returning 413 to Hermes. "
            "System prompt may be larger than MAX_SYS_CHARS=%d.",
            req_id, MAX_TRIM_PASSES, MAX_SYS_CHARS,
        )
        self._send_error(
            413,
            f"Request too large after {MAX_TRIM_PASSES} trim passes "
            f"(MAX_SYS_CHARS={MAX_SYS_CHARS}, MAX_MSG_CHARS={MAX_MSG_CHARS}). "
            f"Session reset may be needed.",
        )

    def _stream_response(self, resp, req_id):
        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() in (
                "transfer-encoding", "connection", "keep-alive", "content-encoding"
            ):
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
        body = json.dumps(
            {"error": {"message": msg, "type": "proxy_error"}}
        ).encode()
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
            target    = self._handle_thread,
            args      = (request, client_address),
            daemon    = True,
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
    log.info("Groq API Key Rotation Proxy  v3 — Context Guardian Edition")
    log.info("Listening  : %s:%d", PROXY_HOST, PROXY_PORT)
    log.info("Target     : %s", GROQ_BASE)
    log.info("Keys       : %d loaded (round-robin)", len(_keys))
    for i, k in enumerate(_keys, 1):
        log.info("  [%d] %s...%s", i, k[:8], k[-4:])
    log.info("Protections:")
    log.info("  max_tokens cap  : %d (injected if missing/larger)", MAX_OUTPUT_TOKENS)
    log.info("  pre-flight trim : body > %d bytes", PRE_TRIM_BYTES)
    log.info("  per-msg limit   : %d chars per message", MAX_MSG_CHARS)
    log.info("  system limit    : %d chars for system prompt", MAX_SYS_CHARS)
    log.info("  reactive passes : %d (after 413 response)", MAX_TRIM_PASSES)
    log.info("  keep msgs L3    : %d / %d / %d per trim pass", KEEP_MSGS_PASS1, KEEP_MSGS_PASS2, KEEP_MSGS_PASS3)
    log.info("=" * 65)
    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), GroqProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Proxy shutting down.")
