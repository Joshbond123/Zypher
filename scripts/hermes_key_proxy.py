#!/usr/bin/env python3
"""
hermes_key_proxy.py — Local OpenAI-compatible proxy with key rotation + Cerebras sanitizer.

Listens on 127.0.0.1:7860/v1
Forwards to https://api.cerebras.ai/v1 with per-request API key rotation.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SANITIZATION LAYER (added May 2026) — strips Cerebras-incompatible fields
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ROOT CAUSE: _empty_recovery_synthetic HTTP 400
  Hermes injects synthetic nudge messages when the model returns an empty
  response after tool calls. These messages carry an internal Python dict
  key `_empty_recovery_synthetic: True`. Hermes serializes this directly into
  the JSON payload sent to Cerebras. Cerebras strict-validates every field
  against its spec and rejects unknown properties:
    400 "messages.18.assistant._empty_recovery_synthetic: property unsupported"
  Fix: strip all _* prefixed fields from message objects before forwarding.

FULL LIST of fields stripped by this proxy (confirmed against Cerebras spec):

  Top-level body (Cerebras rejects these entirely):
    store               — OpenAI request-storage flag
    maxTokens           — camelCase variant (use max_tokens)
    thinking            — Anthropic-style reasoning flag
    prompt_cache_key    — Zed/Anthropic-style prompt caching
    service_tier        — OpenAI-only routing tier
    reasoning_effort    — model-specific, unsupported on Cerebras
    metadata            — OpenAI-only request metadata
    Any field starting with _

  Message-level (Cerebras rejects these on all message roles):
    reasoning_content   — Cerebras uses "reasoning" (str|null) instead
    _empty_recovery_synthetic — Hermes internal nudge marker (the bug field)
    Any field starting with _

  Message content normalization:
    assistant messages with tool_calls and content="" → content=null
      (Cerebras requires null, not empty string, on tool-call assistant turns)
    role="developer" → role="system"
      (OpenAI o-series "developer" role not supported by Cerebras)

RATE-LIMIT STRATEGY:
  On HTTP 429: read Retry-After header (default 5s), wait, try next key.
  After all keys exhausted: wait 10s for Cerebras quota reset, retry once.
  Transient rate-limit blips are absorbed here — Hermes never sees them.
"""

import http.server
import http.client
import json as _json
import threading
import os
import ssl
import logging
import sys
import time

logging.basicConfig(
    format="%(asctime)s %(levelname)s [key-proxy] %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
log = logging.getLogger("key-proxy")

KEYS = [k.strip() for k in [
    os.environ.get("CEREBRAS_API_KEY",   ""),
    os.environ.get("CEREBRAS_API_KEY_2", ""),
    os.environ.get("CEREBRAS_API_KEY_3", ""),
    os.environ.get("CEREBRAS_API_KEY_4", ""),
    os.environ.get("CEREBRAS_API_KEY_5", ""),
] if k.strip()]

UPSTREAM_HOST = "api.cerebras.ai"
UPSTREAM_PORT = 443
PROXY_PORT    = int(os.environ.get("KEY_PROXY_PORT", "7860"))

# ── Top-level body fields Cerebras does not accept ────────────────────────────
_BLOCKED_BODY_FIELDS = frozenset({
    "store",
    "maxTokens",
    "thinking",
    "prompt_cache_key",
    "service_tier",
    "reasoning_effort",
    "metadata",
})

# ── Message-level fields Cerebras does not accept ────────────────────────────
_BLOCKED_MSG_FIELDS = frozenset({
    "reasoning_content",
    "_empty_recovery_synthetic",
})

_counter = 0
_counter_lock = threading.Lock()


def next_key() -> str:
    global _counter
    with _counter_lock:
        key = KEYS[_counter % len(KEYS)]
        _counter += 1
    return key


# ─────────────────────────────────────────────────────────────────────────────
# SANITIZATION
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize_message(msg: dict) -> dict:
    """
    Remove all Cerebras-incompatible fields from a single message object.

    Rules applied (in order):
      1. Strip explicitly blocked fields (_BLOCKED_MSG_FIELDS).
      2. Strip any field whose name starts with '_' (Hermes internal markers).
      3. If role == 'developer': replace with 'system' (Cerebras rejects 'developer').
      4. If role == 'assistant' AND tool_calls present AND content == '':
           set content = None  (Cerebras requires null, not empty string).
    """
    cleaned = {}
    for k, v in msg.items():
        if k in _BLOCKED_MSG_FIELDS:
            log.debug("  msg sanitizer: stripped message field %r", k)
            continue
        if k.startswith("_"):
            log.info("  msg sanitizer: stripped internal Hermes field %r from message", k)
            continue
        cleaned[k] = v

    # Role normalisation
    if cleaned.get("role") == "developer":
        log.info("  msg sanitizer: role 'developer' → 'system'")
        cleaned["role"] = "system"

    # Tool-call content normalisation
    if (
        cleaned.get("role") == "assistant"
        and cleaned.get("tool_calls")
        and cleaned.get("content") == ""
    ):
        log.debug("  msg sanitizer: assistant tool_calls + content='' → null")
        cleaned["content"] = None

    return cleaned


def sanitize_request_body(body_bytes: bytes, path: str) -> bytes:
    """
    Parse the JSON request body, strip all Cerebras-incompatible fields, and
    re-serialise.  Returns the original bytes unchanged if:
      - the body is empty
      - JSON parsing fails (pass through as-is, let Cerebras return the 400)
      - no modifications were needed (avoids pointless re-serialisation)
    """
    if not body_bytes:
        return body_bytes

    # Only chat/completions calls carry message arrays worth sanitizing.
    # For /v1/models and other GET-style calls the body is empty anyway,
    # but we also skip non-JSON content types to be safe.
    if b'"messages"' not in body_bytes and b'"model"' not in body_bytes:
        return body_bytes

    try:
        payload = _json.loads(body_bytes)
    except Exception:
        log.warning("sanitize_request_body: JSON parse failed — forwarding raw body")
        return body_bytes

    if not isinstance(payload, dict):
        return body_bytes

    modified = False

    # ── 1. Strip blocked top-level body fields ────────────────────────────
    for field in list(payload.keys()):
        if field in _BLOCKED_BODY_FIELDS:
            del payload[field]
            log.info("sanitizer: stripped top-level body field %r", field)
            modified = True
        elif field.startswith("_"):
            del payload[field]
            log.info("sanitizer: stripped internal top-level field %r", field)
            modified = True

    # ── 2. Sanitize each message object ───────────────────────────────────
    messages = payload.get("messages")
    if isinstance(messages, list):
        sanitized_msgs = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                sanitized_msgs.append(msg)
                continue
            cleaned = _sanitize_message(msg)
            if cleaned != msg:
                modified = True
                log.info(
                    "sanitizer: message[%d] role=%r — stripped fields: %s",
                    i,
                    msg.get("role", "?"),
                    sorted(set(msg) - set(cleaned)),
                )
            sanitized_msgs.append(cleaned)
        payload["messages"] = sanitized_msgs

    if not modified:
        return body_bytes

    result = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    log.info(
        "sanitizer: body sanitized %d→%d bytes for %s",
        len(body_bytes), len(result), path,
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST EXECUTION WITH KEY ROTATION + 429 BACKOFF
# ─────────────────────────────────────────────────────────────────────────────

def try_request(method: str, path: str, headers: dict, body: bytes | None):
    """
    Attempt the request with full key rotation and 429 backoff.

    Strategy:
      Pass 0: try every key once; on 429 wait Retry-After then rotate.
      Pass 1: all keys exhausted on 429 → wait 10s for quota reset, retry all.
      401/403: skip key immediately (bad or revoked key).
      Network errors: skip and continue.
    """
    ctx = ssl.create_default_context()
    last_err = None
    n_keys = len(KEYS)

    for cycle in range(2):
        if cycle == 1:
            log.warning(
                "All %d key(s) rate-limited — waiting 10s for Cerebras TPM reset...",
                n_keys,
            )
            time.sleep(10)

        for ki in range(n_keys):
            key = next_key()
            req_headers = dict(headers)
            req_headers["Authorization"] = f"Bearer {key}"

            try:
                conn = http.client.HTTPSConnection(
                    UPSTREAM_HOST, UPSTREAM_PORT, context=ctx, timeout=120
                )
                conn.request(method, path, body=body, headers=req_headers)
                resp = conn.getresponse()

                if resp.status == 429:
                    resp.read()
                    conn.close()
                    retry_after_hdr = resp.getheader("Retry-After", "")
                    try:
                        wait = min(int(retry_after_hdr), 30)
                    except (ValueError, TypeError):
                        wait = 5
                    log.warning(
                        "Key[%d/%d] 429 rate-limited — waiting %ds then rotating",
                        ki + 1, n_keys, wait,
                    )
                    time.sleep(wait)
                    continue

                if resp.status in (401, 403):
                    log.warning(
                        "Key[%d/%d] auth error HTTP %d — rotating",
                        ki + 1, n_keys, resp.status,
                    )
                    resp.read()
                    conn.close()
                    continue

                return conn, resp

            except Exception as e:
                last_err = e
                log.warning("Upstream error key[%d/%d]: %s", ki + 1, n_keys, e)
                try:
                    conn.close()
                except Exception:
                    pass

    raise RuntimeError(
        f"All {n_keys} key(s) failed after retry cycle. "
        f"Add more CEREBRAS_API_KEY_N env vars to increase capacity. "
        f"Last error: {last_err}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# HTTP HANDLER
# ─────────────────────────────────────────────────────────────────────────────

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _proxy(self, method: str):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else None

        # ── SANITIZE before forwarding ────────────────────────────────────
        body = sanitize_request_body(raw_body, self.path) if raw_body else raw_body

        fwd_headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept":       self.headers.get("Accept", "application/json, text/event-stream"),
            "User-Agent":   "hermes-key-proxy/1.0",
        }
        if body:
            # Recalculate Content-Length after sanitization may have changed body size
            fwd_headers["Content-Length"] = str(len(body))

        try:
            conn, resp = try_request(method, self.path, fwd_headers, body)
        except Exception as e:
            log.error("Proxy failed: %s", e)
            self.send_error(502, f"Upstream error: {e}")
            return

        try:
            self.send_response(resp.status)
            skip = {"transfer-encoding", "connection", "keep-alive"}
            for h, v in resp.getheaders():
                if h.lower() not in skip:
                    self.send_header(h, v)
            self.send_header("X-Key-Proxy", "cerebras-sanitizing-rotating")
            self.end_headers()

            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        except Exception as e:
            log.warning("Streaming error: %s", e)
        finally:
            conn.close()

    def do_GET(self):    self._proxy("GET")
    def do_POST(self):   self._proxy("POST")
    def do_PUT(self):    self._proxy("PUT")
    def do_DELETE(self): self._proxy("DELETE")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if not KEYS:
        log.error("No Cerebras API keys configured. Set CEREBRAS_API_KEY env vars.")
        sys.exit(1)
    log.info("Key-rotation + sanitizing proxy on 127.0.0.1:%d with %d key(s)", PROXY_PORT, len(KEYS))
    log.info("Upstream: https://%s", UPSTREAM_HOST)
    log.info("Sanitizer: strips _empty_recovery_synthetic + reasoning_content + blocked body fields")
    log.info("Rate-limit: Retry-After backoff per key, 10s cycle reset")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
