#!/usr/bin/env python3
"""
hermes_key_proxy.py -- Local OpenAI-compatible proxy with key rotation,
                       Cerebras sanitizer, per-key cooldown tracking, and 5xx retry.

Listens on 127.0.0.1:7860/v1
Forwards to https://api.cerebras.ai/v1 with per-request API key rotation.

------------------------------------------------------------------------
BUG HISTORY
------------------------------------------------------------------------

[FIXED Jun 2026] Runtime freeze during hard tasks -- three causes:

  CAUSE 1: No per-key cooldown tracking
    When key[K] returned 429, the proxy immediately moved to key[K+1] and
    tried it. But key[K+1] was also likely rate-limited (all keys share the
    same Cerebras org quota). The proxy wasted the full Retry-After period
    on every key serially:
      5 keys x 30s Retry-After x 2 cycles = 310s of blocking per LLM request
    Hermes held open_request=1 for 310s -> visible 5-minute freeze.
    Combined with api_max_retries=7: 7 x 310s = 36 minutes of zombie behavior.
    FIX: Per-key cooldown dict (_key_cooldowns). On 429, mark the key as
    unavailable until now+retry_after. next_available_key() skips cooled-down
    keys and -- if ALL keys are in cooldown -- waits only until the EARLIEST
    one resets instead of spinning through all of them serially.

  CAUSE 2: Retry-After cap of 30s too high
    With 5 keys each waiting up to 30s, even one retry cycle blocked
    for 2.5 minutes on a single LLM call.
    FIX: Cap reduced from 30s to 10s. Cerebras Retry-After is usually 1-5s;
    10s is a safe upper bound that prevents extreme blocking.
    New worst case: 5 keys x 10s x 2 cycles = 100s (was 310s).

  CAUSE 3: CONNECT_TIMEOUT=60s cut off large model responses under load
    gpt-oss-120b generates large responses for complex tasks.
    Under Cerebras load, legitimate responses occasionally took >60s.
    Premature timeout triggered Hermes retries that cascaded into quota burn.
    FIX: CONNECT_TIMEOUT increased from 60s to 90s.

[FIXED May 2026] HTTP 502 -- three causes identified and fixed:

  CAUSE 1: Wrong primary model (zai-glm-4.7, 355B thinking model)
    FIX: Switch primary to gpt-oss-120b (120B, 3000 TPM, no thinking mode).

  CAUSE 2: Proxy never retried on 5xx errors
    FIX: Added 5xx retry with 2-second backoff per key.

  CAUSE 3: thinking tokens in responses from zai-glm-4.7
    FIX: strip_response_reasoning() strips reasoning field before forwarding.

[FIXED May 2026] Streaming stall -- "waiting for stream response (120s, no chunks yet)":
  FIX: streaming:false in hermes_setup.py + CONNECT_TIMEOUT reduced.

SANITIZATION LAYER:
  Top-level: store, maxTokens, thinking, prompt_cache_key, service_tier,
             reasoning_effort, metadata, _ prefixed fields.
  Message-level: reasoning_content, _empty_recovery_synthetic, _ prefixed.
  Message normalisation: assistant+tool_calls+content="" -> content=null.
  role="developer" -> role="system".
  thinking:{type:disabled} injected for known thinking models.

RATE-LIMIT / ERROR STRATEGY (Jun 2026):
  429: mark key in cooldown (capped at RETRY_AFTER_MAX=10s), rotate to
       next available key. next_available_key() waits once for earliest
       reset instead of sleeping per-key (prevents serial 310s blocking).
  5xx: 2s backoff, rotate key (up to N_KEYS per cycle).
  Two-cycle: after all keys exhausted, wait 5s then retry.
  401/403: skip key immediately (bad/revoked).
  Network errors: skip key, log, continue.
"""

import http.server
import http.client
import json as _json
import socket
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

# Timeout for the initial connect + non-streaming reads (seconds).
# INCREASED from 60s to 90s: handles large model responses under Cerebras load.
# gpt-oss-120b generates large context-heavy responses for complex tasks;
# premature 60s timeout triggered retries that burned quota unnecessarily.
CONNECT_TIMEOUT = 90

# Per-chunk timeout during streaming responses (seconds).
# Only applies when streaming: true is used (currently disabled in hermes_setup.py).
STREAM_CHUNK_TIMEOUT = 30

# Maximum seconds to honour a Retry-After header per key.
# REDUCED from 30s to 10s. Cerebras Retry-After is usually 1-5s.
# Old worst case: 5 keys x 30s x 2 cycles = 310s blocking per LLM call.
# New worst case: 5 keys x 10s x 2 cycles = 100s (still handles all retries).
RETRY_AFTER_MAX = 10

# -- Top-level body fields Cerebras does not accept --------------------------
_BLOCKED_BODY_FIELDS = frozenset({
    "store", "maxTokens", "thinking", "prompt_cache_key",
    "service_tier", "reasoning_effort", "metadata",
})

# -- Message-level fields Cerebras does not accept ---------------------------
_BLOCKED_MSG_FIELDS = frozenset({
    "reasoning_content",
    "_empty_recovery_synthetic",
})

# -- HTTP status codes that trigger retry (transient upstream errors) ---------
_RETRYABLE_5XX = frozenset({500, 502, 503, 504})

# -- Model IDs that require thinking mode disabled ---------------------------
_THINKING_MODELS = ("zai-glm-4.7", "deepseek-r1", "qwq")

_counter = 0
_counter_lock = threading.Lock()

# -- Per-key cooldown tracking -----------------------------------------------
# Maps key string -> float (time.time() when the key becomes available again).
# Thread-safe: all access protected by _cooldown_lock.
#
# ROOT CAUSE FIX (Jun 2026): Previously the proxy had no per-key cooldown
# memory. On 429, it slept Retry-After for EACH key serially, causing up to
# 310s of blocking per LLM request when all keys were rate-limited.
# Now: mark each rate-limited key with its expiry time and skip it during
# next_available_key(). If ALL keys are in cooldown, wait only until the
# EARLIEST one resets -- one targeted wait instead of N_KEYS serial waits.
_key_cooldowns = {}   # key_string -> available_at (float epoch seconds)
_cooldown_lock = threading.Lock()


def _mark_key_cooldown(key, seconds):
    """Mark key as unavailable for `seconds` seconds from now."""
    available_at = time.time() + seconds
    with _cooldown_lock:
        _key_cooldowns[key] = available_at
    log.info(
        "Cooldown: key=...%s rate-limited for %.1fs",
        key[-4:], seconds,
    )


def _earliest_cooldown_expiry():
    """Return the earliest time at which any cooled-down key becomes available."""
    with _cooldown_lock:
        if not _key_cooldowns:
            return 0.0
        return min(_key_cooldowns.values())


def next_available_key():
    """
    Return the next available (not rate-limited) API key, round-robin.

    Algorithm:
      1. Scan all N keys starting from the current counter position.
      2. Return the first key whose cooldown has expired (or was never set).
      3. If ALL keys are in cooldown, wait until the earliest one resets
         (a single targeted sleep, NOT N_KEYS x Retry-After serially).
      4. After the wait, clear expired cooldowns and return the next key.

    This replaces the old next_key() which had no cooldown awareness and
    caused serial blocking: key1 wait 30s -> key2 wait 30s -> ... = 150s+.
    """
    global _counter
    now = time.time()

    with _cooldown_lock:
        cooldowns_snapshot = dict(_key_cooldowns)

    for _ in range(len(KEYS)):
        with _counter_lock:
            idx = _counter % len(KEYS)
            _counter += 1
        key = KEYS[idx]
        if now >= cooldowns_snapshot.get(key, 0.0):
            return key

    # All keys in cooldown -- wait for the earliest to reset
    earliest = _earliest_cooldown_expiry()
    wait_for = max(0.0, earliest - time.time())
    if wait_for > 0.05:
        log.warning(
            "All %d keys in rate-limit cooldown -- waiting %.1fs for earliest reset",
            len(KEYS), wait_for,
        )
        time.sleep(wait_for + 0.1)

    # Clear expired cooldowns
    now2 = time.time()
    with _cooldown_lock:
        for k in list(_key_cooldowns):
            if now2 >= _key_cooldowns[k]:
                del _key_cooldowns[k]

    with _counter_lock:
        key = KEYS[_counter % len(KEYS)]
        _counter += 1
    return key


# ----------------------------------------------------------------------------
# REQUEST SANITIZATION
# ----------------------------------------------------------------------------

def _sanitize_message(msg):
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
        log.info("  msg sanitizer: role 'developer' -> 'system'")
        cleaned["role"] = "system"

    if (
        cleaned.get("role") == "assistant"
        and cleaned.get("tool_calls")
        and cleaned.get("content") == ""
    ):
        cleaned["content"] = None

    return cleaned


def sanitize_request_body(body_bytes, path):
    if not body_bytes:
        return body_bytes
    if b'"messages"' not in body_bytes and b'"model"' not in body_bytes:
        return body_bytes

    try:
        payload = _json.loads(body_bytes)
    except Exception:
        log.warning("sanitize_request_body: JSON parse failed -- forwarding raw body")
        return body_bytes

    if not isinstance(payload, dict):
        return body_bytes

    modified = False

    for field in list(payload.keys()):
        if field in _BLOCKED_BODY_FIELDS or field.startswith("_"):
            del payload[field]
            log.info("sanitizer: stripped top-level field %r", field)
            modified = True

    model = payload.get("model", "")
    is_thinking_model = any(tm in model for tm in _THINKING_MODELS)
    if is_thinking_model:
        if "thinking" not in payload:
            payload["thinking"] = {"type": "disabled"}
            log.info("sanitizer: injected thinking:{type:disabled} for %s", model)
            modified = True

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
    log.info("sanitizer: body sanitized %d->%d bytes for %s", len(body_bytes), len(result), path)
    return result


# ----------------------------------------------------------------------------
# RESPONSE SANITIZATION -- strip reasoning tokens Hermes can't handle
# ----------------------------------------------------------------------------

def strip_response_reasoning(body_bytes):
    """Remove 'reasoning' (thinking tokens) from non-streaming Cerebras responses."""
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


# ----------------------------------------------------------------------------
# CORE REQUEST HANDLER WITH COOLDOWN-AWARE KEY ROTATION
# ----------------------------------------------------------------------------

def try_request(method, path, headers, body):
    """
    Attempt the request with full key rotation, per-key cooldown awareness,
    429 backoff, and 5xx retry.

    Strategy per cycle (runs up to 2 cycles):
      - 429: mark key in cooldown for min(Retry-After, RETRY_AFTER_MAX)s.
             next_available_key() skips cooled-down keys automatically and
             waits only until the EARLIEST key resets (not per-key serial).
      - 5xx (500/502/503/504): wait 2s, rotate key.
      - 401/403: skip key immediately (bad/revoked).
      - Network error: skip key, log.
    Cycle 0: try every key using next_available_key().
    Cycle 1: all keys exhausted -> wait 5s for quota reset, retry.

    KEY IMPROVEMENT (Jun 2026):
      OLD: sleep(Retry-After) for EACH key -> 5 keys x 30s = 150s per cycle
      NEW: mark cooldown, wait once for earliest expiry -> max 10s regardless
           of how many keys fail simultaneously.
    """
    ctx = ssl.create_default_context()
    last_err = None
    n_keys = len(KEYS)

    for cycle in range(2):
        if cycle == 1:
            log.warning("All %d key(s) exhausted -- waiting 5s for Cerebras reset...", n_keys)
            time.sleep(5)

        for ki in range(n_keys):
            key = next_available_key()
            req_headers = dict(headers)
            req_headers["Authorization"] = "Bearer " + key

            try:
                conn = http.client.HTTPSConnection(
                    UPSTREAM_HOST, UPSTREAM_PORT, context=ctx, timeout=CONNECT_TIMEOUT
                )
                conn.request(method, path, body=body, headers=req_headers)
                resp = conn.getresponse()

                # -- 429 Rate limit ------------------------------------------
                if resp.status == 429:
                    resp.read()
                    conn.close()
                    retry_after_hdr = resp.getheader("Retry-After", "")
                    try:
                        wait = min(int(retry_after_hdr), RETRY_AFTER_MAX)
                    except (ValueError, TypeError):
                        wait = 3
                    # Mark this key as rate-limited (cooldown tracking handles wait)
                    _mark_key_cooldown(key, wait)
                    log.warning(
                        "Key[%d/%d] 429 -- cooldown %.1fs, rotating to next available",
                        ki + 1, n_keys, wait,
                    )
                    # No sleep() here -- next_available_key() waits automatically
                    continue

                # -- 5xx Transient upstream error ----------------------------
                if resp.status in _RETRYABLE_5XX:
                    err_body = resp.read()[:200].decode("utf-8", errors="replace")
                    conn.close()
                    log.warning(
                        "Key[%d/%d] Cerebras %d -- rotating key + 2s backoff: %s",
                        ki + 1, n_keys, resp.status, err_body,
                    )
                    time.sleep(2)
                    continue

                # -- Auth failure --------------------------------------------
                if resp.status in (401, 403):
                    log.warning("Key[%d/%d] auth error HTTP %d -- rotating", ki + 1, n_keys, resp.status)
                    resp.read()
                    conn.close()
                    continue

                # -- Success (or non-retryable error like 400/404) -----------
                return conn, resp

            except Exception as e:
                last_err = e
                log.warning("Upstream network error key[%d/%d]: %s", ki + 1, n_keys, e)
                try:
                    conn.close()
                except Exception:
                    pass

    raise RuntimeError(
        "All {} key(s) failed after retry cycle. "
        "Add more CEREBRAS_API_KEY_N env vars. Last error: {}".format(n_keys, last_err)
    )


# ----------------------------------------------------------------------------
# HTTP HANDLER
# ----------------------------------------------------------------------------

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _proxy(self, method):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else None

        body = sanitize_request_body(raw_body, self.path) if raw_body else raw_body

        fwd_headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept":       self.headers.get("Accept", "application/json, text/event-stream"),
            "User-Agent":   "hermes-key-proxy/2.3",
        }
        if body:
            fwd_headers["Content-Length"] = str(len(body))

        try:
            conn, resp = try_request(method, self.path, fwd_headers, body)
        except Exception as e:
            log.error("Proxy failed: %s", e)
            self.send_error(502, "Upstream error: {}".format(e))
            return

        try:
            is_streaming = resp.getheader("Content-Type", "").startswith("text/event-stream")

            self.send_response(resp.status)
            skip = {"transfer-encoding", "connection", "keep-alive"}
            resp_headers = list(resp.getheaders())

            if not is_streaming:
                resp_body = resp.read()
                sanitized_body = strip_response_reasoning(resp_body)
                for h, v in resp_headers:
                    if h.lower() in skip:
                        continue
                    if h.lower() == "content-length":
                        continue
                    self.send_header(h, v)
                self.send_header("Content-Length", str(len(sanitized_body)))
                self.send_header("X-Key-Proxy", "cerebras-sanitizing-rotating/2.3")
                self.end_headers()
                self.wfile.write(sanitized_body)
                self.wfile.flush()
            else:
                for h, v in resp_headers:
                    if h.lower() not in skip:
                        self.send_header(h, v)
                self.send_header("X-Key-Proxy", "cerebras-sanitizing-rotating/2.3")
                self.end_headers()

                try:
                    if conn.sock is not None:
                        conn.sock.settimeout(STREAM_CHUNK_TIMEOUT)
                except Exception as _st_err:
                    log.debug("Could not set stream chunk timeout: %s", _st_err)

                chunk_count = 0
                while True:
                    try:
                        chunk = resp.read(4096)
                    except (socket.timeout, TimeoutError, OSError) as e:
                        log.warning(
                            "Stream stall after %ds -- closing. chunks_received=%d error=%s",
                            STREAM_CHUNK_TIMEOUT, chunk_count, type(e).__name__,
                        )
                        break
                    if not chunk:
                        break
                    chunk_count += 1
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


# ----------------------------------------------------------------------------
# ENTRY POINT
# ----------------------------------------------------------------------------

def main():
    if not KEYS:
        log.error("No Cerebras API keys configured. Set CEREBRAS_API_KEY env vars.")
        sys.exit(1)
    log.info("Key-rotation proxy v2.3 on 127.0.0.1:%d with %d key(s)", PROXY_PORT, len(KEYS))
    log.info("Upstream: https://%s", UPSTREAM_HOST)
    log.info("CONNECT_TIMEOUT: %ds (increased from 60s -- handles large model responses)", CONNECT_TIMEOUT)
    log.info("RETRY_AFTER_MAX: %ds per key (was 30s -- prevents 310s serial blocking)", RETRY_AFTER_MAX)
    log.info("Cooldown tracking: per-key, waits once for earliest reset (not per-key serial)")
    log.info("Thinking models: %s -- thinking:{type:disabled} injected", _THINKING_MODELS)
    log.info("Retry: 429->cooldown+rotate, 5xx->2s backoff+rotate, cycle reset after 5s")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
