#!/usr/bin/env python3
"""
hermes_key_proxy.py — Local OpenAI-compatible proxy with key rotation,
                       Cerebras sanitizer, and 5xx retry logic.

Listens on 127.0.0.1:7860/v1
Forwards to https://api.cerebras.ai/v1 with per-request API key rotation.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUG HISTORY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[FIXED May 2026] HTTP 502 — three causes identified and fixed:

  CAUSE 1: Wrong primary model (zai-glm-4.7, 355B thinking model)
    zai-glm-4.7 has "Interleaved Thinking" enabled by default — it inserts
    reasoning tokens between every tool call. At 355B / ~1000 TPM it overloads
    under long agent conversations and returns HTTP 502.
    FIX: Switch primary to gpt-oss-120b (120B, 3000 TPM, no thinking mode,
    pure OpenAI-compatible). Keep zai-glm-4.7 as fallback only.

  CAUSE 2: Proxy never retried on 5xx errors
    The proxy previously only retried on 429 (rate limit) and skipped on
    401/403 (auth). Any 500/502/503/504 from Cerebras was forwarded
    straight to Hermes. Hermes retried the proxy 3× (all 502), then gave up.
    FIX: Added 5xx retry with 2-second backoff per key, same cycle structure
    as the 429 handler. Cerebras transient backend errors are now absorbed
    by the proxy before Hermes ever sees them.

  CAUSE 3: thinking tokens in responses from zai-glm-4.7 (when used as fallback)
    zai-glm-4.7 returns `reasoning` field in responses (thinking tokens).
    This is stripped from response bodies before forwarding to Hermes so
    Hermes never sees non-standard thinking fields.
    FIX: strip_response_reasoning() applied to non-streaming responses.

SANITIZATION LAYER — strips Cerebras-incompatible request fields:

  Top-level body fields stripped:
    store, maxTokens, thinking, prompt_cache_key, service_tier,
    reasoning_effort, metadata, and any field starting with _

  Message-level fields stripped:
    reasoning_content, _empty_recovery_synthetic, and any _ prefixed field

  Message content normalisation:
    assistant + tool_calls + content="" → content=null
    role="developer" → role="system"

  Thinking mode disabled for zai-glm-4.7:
    Injects thinking: {type: disabled} so the model skips reasoning tokens
    and returns a standard OpenAI-compatible response.

RATE-LIMIT / ERROR STRATEGY:
  429: read Retry-After (default 5s), rotate key, retry.
  5xx: 2s backoff, rotate key, retry (up to N_KEYS times per cycle).
  Two-cycle structure: after all keys exhausted, wait 10s for quota reset.
  401/403: skip key (bad/revoked).
  Network errors: skip key, log, continue.
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
    "store", "maxTokens", "thinking", "prompt_cache_key",
    "service_tier", "reasoning_effort", "metadata",
})

# ── Message-level fields Cerebras does not accept ────────────────────────────
_BLOCKED_MSG_FIELDS = frozenset({
    "reasoning_content",
    "_empty_recovery_synthetic",
})

# ── HTTP status codes that should trigger retry (transient upstream errors) ───
_RETRYABLE_5XX = frozenset({500, 502, 503, 504})

# ── Model ID for zai-glm-4.7 (requires thinking mode disabled) ───────────────
_ZAI_MODEL_ID = "zai-glm-4.7"

_counter = 0
_counter_lock = threading.Lock()


def next_key() -> str:
    global _counter
    with _counter_lock:
        key = KEYS[_counter % len(KEYS)]
        _counter += 1
    return key


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST SANITIZATION
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize_message(msg: dict) -> dict:
    cleaned = {}
    for k, v in msg.items():
        if k in _BLOCKED_MSG_FIELDS:
            log.debug("  msg sanitizer: stripped %r", k)
            continue
        if k.startswith("_"):
            log.info("  msg sanitizer: stripped internal field %r", k)
            continue
        cleaned[k] = v

    if cleaned.get("role") == "developer":
        log.info("  msg sanitizer: role 'developer' → 'system'")
        cleaned["role"] = "system"

    if (
        cleaned.get("role") == "assistant"
        and cleaned.get("tool_calls")
        and cleaned.get("content") == ""
    ):
        cleaned["content"] = None

    return cleaned


def sanitize_request_body(body_bytes: bytes, path: str) -> bytes:
    if not body_bytes:
        return body_bytes
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
        if field in _BLOCKED_BODY_FIELDS or field.startswith("_"):
            del payload[field]
            log.info("sanitizer: stripped top-level field %r", field)
            modified = True

    # ── 2. For zai-glm-4.7: inject thinking disabled to prevent reasoning tokens ──
    model = payload.get("model", "")
    if _ZAI_MODEL_ID in model:
        if "thinking" not in payload:
            payload["thinking"] = {"type": "disabled"}
            log.info("sanitizer: injected thinking:{type:disabled} for %s", model)
            modified = True

    # ── 3. Sanitize each message object ───────────────────────────────────
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
                    "sanitizer: message[%d] role=%r stripped: %s",
                    i, msg.get("role", "?"), sorted(set(msg) - set(cleaned)),
                )
            sanitized_msgs.append(cleaned)
        payload["messages"] = sanitized_msgs

    if not modified:
        return body_bytes

    result = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    log.info("sanitizer: body sanitized %d→%d bytes for %s", len(body_bytes), len(result), path)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# RESPONSE SANITIZATION — strip reasoning tokens Hermes can't handle
# ─────────────────────────────────────────────────────────────────────────────

def strip_response_reasoning(body_bytes: bytes) -> bytes:
    """
    Remove 'reasoning' (thinking tokens) from non-streaming Cerebras responses.

    zai-glm-4.7 (and other thinking models) return a 'reasoning' field inside
    each choice's message object. Hermes v0.14 doesn't know how to handle this
    field and may include it verbatim in subsequent requests, causing errors.
    Strip it so Hermes always sees a standard OpenAI-compatible response.
    """
    if not body_bytes or b'"reasoning"' not in body_bytes:
        return body_bytes
    try:
        payload = _json.loads(body_bytes)
        if not isinstance(payload, dict):
            return body_bytes
        modified = False
        for choice in payload.get("choices", []):
            msg = choice.get("message")
            if isinstance(msg, dict) and "reasoning" in msg:
                del msg["reasoning"]
                modified = True
                log.info("response sanitizer: stripped 'reasoning' from choice message")
        if not modified:
            return body_bytes
        return _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except Exception:
        return body_bytes


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST EXECUTION — key rotation + 429 backoff + 5xx retry
# ─────────────────────────────────────────────────────────────────────────────

def try_request(method: str, path: str, headers: dict, body: bytes | None):
    """
    Attempt the request with full key rotation, 429 backoff, and 5xx retry.

    Strategy per cycle (runs up to 2 cycles):
      - 429: wait Retry-After, rotate key.
      - 5xx (500/502/503/504): wait 2s, rotate key — transient upstream error.
      - 401/403: skip key immediately (bad/revoked).
      - Network error: skip key, log.
    Cycle 0: try every key once.
    Cycle 1: all keys exhausted → wait 10s for Cerebras quota reset, retry all.
    """
    ctx = ssl.create_default_context()
    last_err = None
    n_keys = len(KEYS)

    for cycle in range(2):
        if cycle == 1:
            log.warning("All %d key(s) exhausted — waiting 10s for Cerebras reset...", n_keys)
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

                # ── 429 Rate limit ─────────────────────────────────────────
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

                # ── 5xx Transient upstream error ───────────────────────────
                if resp.status in _RETRYABLE_5XX:
                    err_body = resp.read()[:200].decode("utf-8", errors="replace")
                    conn.close()
                    log.warning(
                        "Key[%d/%d] Cerebras %d — rotating key + 2s backoff: %s",
                        ki + 1, n_keys, resp.status, err_body,
                    )
                    time.sleep(2)
                    continue

                # ── Auth failure ────────────────────────────────────────────
                if resp.status in (401, 403):
                    log.warning("Key[%d/%d] auth error HTTP %d — rotating", ki + 1, n_keys, resp.status)
                    resp.read()
                    conn.close()
                    continue

                # ── Success (or non-retryable error like 400/404) ──────────
                return conn, resp

            except Exception as e:
                last_err = e
                log.warning("Upstream network error key[%d/%d]: %s", ki + 1, n_keys, e)
                try:
                    conn.close()
                except Exception:
                    pass

    raise RuntimeError(
        f"All {n_keys} key(s) failed after retry cycle. "
        f"Add more CEREBRAS_API_KEY_N env vars. Last error: {last_err}"
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

        body = sanitize_request_body(raw_body, self.path) if raw_body else raw_body

        fwd_headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept":       self.headers.get("Accept", "application/json, text/event-stream"),
            "User-Agent":   "hermes-key-proxy/2.0",
        }
        if body:
            fwd_headers["Content-Length"] = str(len(body))

        try:
            conn, resp = try_request(method, self.path, fwd_headers, body)
        except Exception as e:
            log.error("Proxy failed: %s", e)
            self.send_error(502, f"Upstream error: {e}")
            return

        try:
            # Read response body to apply response sanitization on non-streaming
            is_streaming = (resp.getheader("Content-Type", "").startswith("text/event-stream"))

            self.send_response(resp.status)
            skip = {"transfer-encoding", "connection", "keep-alive"}
            resp_headers = list(resp.getheaders())

            if not is_streaming:
                # Read full body, sanitize, then send with correct Content-Length
                resp_body = resp.read()
                sanitized_body = strip_response_reasoning(resp_body)
                for h, v in resp_headers:
                    if h.lower() in skip:
                        continue
                    if h.lower() == "content-length":
                        continue  # recalculate after sanitization
                    self.send_header(h, v)
                self.send_header("Content-Length", str(len(sanitized_body)))
                self.send_header("X-Key-Proxy", "cerebras-sanitizing-rotating/2.0")
                self.end_headers()
                self.wfile.write(sanitized_body)
                self.wfile.flush()
            else:
                # Streaming: forward chunks as-is
                for h, v in resp_headers:
                    if h.lower() not in skip:
                        self.send_header(h, v)
                self.send_header("X-Key-Proxy", "cerebras-sanitizing-rotating/2.0")
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
            log.warning("Response handling error: %s", e)
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
    log.info("Key-rotation proxy v2.0 on 127.0.0.1:%d with %d key(s)", PROXY_PORT, len(KEYS))
    log.info("Upstream: https://%s", UPSTREAM_HOST)
    log.info("Sanitizer: strips reasoning_content + blocked fields + zai-glm-4.7 thinking mode")
    log.info("Retry: 429→Retry-After, 5xx→2s backoff, both with key rotation + 10s cycle reset")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
