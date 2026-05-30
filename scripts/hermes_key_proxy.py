#!/usr/bin/env python3
"""
hermes_key_proxy.py -- Local OpenAI-compatible proxy with key rotation,
                       Cerebras sanitizer, per-key cooldown tracking,
                       circuit breaker, and 5xx retry.

Listens on 127.0.0.1:7860/v1
Forwards to https://api.cerebras.ai/v1 with per-request API key rotation.

------------------------------------------------------------------------
BUG HISTORY
------------------------------------------------------------------------

[FIXED Jul 2026] Infinite watchdog restart loop -- four root causes:

  CAUSE 1: test_key() in hermes_key_rotator.py only tested /v1/models
    The /v1/models endpoint has a separate (generous) quota from
    /v1/chat/completions. A key rate-limited for chat still returns 200
    from /v1/models. Key rotator declared all keys "healthy" while all
    were rate-limited for actual completions. Fixed in hermes_key_rotator.py
    -- now tests with a 1-token llama3.1-8b chat completion.

  CAUSE 2: api_max_retries=7 * worst-case proxy block = 455s freeze
    With all keys rate-limited:
      cycle 0: 5 keys * 1s (fast 429) = ~5s
      inter-cycle sleep: 5s
      cycle 1: 5 keys * (10s cooldown wait + 1s HTTP) = ~55s
      total per proxy call: ~65s
    7 retries * 65s = 455s total freeze. Watchdog fires at 420s (retry 6),
    kills Hermes. But 10s cooldowns expired during the 420s wait, so
    Hermes immediately hits rate limits again on restart -> infinite loop.
    Fixed: api_max_retries reduced from 7 to 2 in hermes_setup.py.
    New worst-case: 2 * 65s = 130s -- well under 240s watchdog threshold.

  CAUSE 3: No circuit breaker -- 10s cooldown for 1 or 100 failures
    A key failing 50 times got the same 10s quarantine as one failing once.
    After 10s it was retried and failed again -> infinite retry loops.
    FIX (this file): Per-key circuit breaker. After CB_THRESHOLD (3)
    consecutive failures, key quarantined for CB_QUARANTINE (300s = 5min).
    If ALL keys are circuit-broken, proxy returns HTTP 503 immediately
    instead of blocking -- Hermes fails fast without freezing.

  CAUSE 4: Watchdog restarted Hermes but NOT the proxy
    Proxy kept in-memory cooldown/circuit-breaker state across Hermes
    restarts. The 10s cooldowns expired during the 420s freeze window,
    so after restart Hermes immediately hit rate limits again.
    Fixed in hermes.yml: watchdog now restarts proxy alongside Hermes.

[FIXED Jun 2026] Runtime freeze during hard tasks -- three causes:

  CAUSE 1: No per-key cooldown tracking
    5 keys x 30s Retry-After x 2 cycles = 310s blocking per LLM request.
    FIX: Per-key cooldown dict. next_available_key() waits once for
    earliest reset instead of sleeping per-key serially.

  CAUSE 2: Retry-After cap of 30s too high
    FIX: Cap reduced from 30s to 10s.

  CAUSE 3: CONNECT_TIMEOUT=60s cut off large model responses under load
    FIX: CONNECT_TIMEOUT increased from 60s to 90s.

[FIXED May 2026] HTTP 502, streaming stall -- see prior history.

SANITIZATION LAYER:
  Top-level: store, maxTokens, thinking, prompt_cache_key, service_tier,
             reasoning_effort, metadata, _ prefixed fields.
  Message-level: reasoning_content, _empty_recovery_synthetic, _ prefixed.
  Message normalisation: assistant+tool_calls+content="" -> content=null.
  role="developer" -> role="system".
  thinking:{type:disabled} injected for known thinking models.

RATE-LIMIT / ERROR STRATEGY (Jul 2026):
  429: mark 10s cooldown, increment failure counter. Circuit trips at 3
       consecutive failures -> CB_QUARANTINE (300s). next_available_key()
       waits once for earliest reset (not N_KEYS serial sleeps).
  5xx: 2s backoff, rotate key, increment failure counter.
  401/403: permanent quarantine for this session (24h cooldown).
  Network errors: skip key, log, continue, increment failure counter.
  ALL keys circuit-broken: return HTTP 503 immediately (no blocking).
  Success: reset failure counter for that key.

ENDPOINTS:
  /health or /healthz  - JSON health status with per-key circuit state
  /v1/keys/status      - detailed per-key diagnostic (label, cooldown, failures)
  /v1/models           - passthrough to Cerebras models list
  /v1/*                - proxied POST to Cerebras with key rotation
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
CONNECT_TIMEOUT = 90

# Per-chunk timeout during streaming responses (seconds).
STREAM_CHUNK_TIMEOUT = 30

# Maximum seconds to honour a Retry-After header per key.
# REDUCED from 30s to 10s. Cerebras Retry-After is usually 1-5s.
RETRY_AFTER_MAX = 10

# -- Circuit breaker configuration -------------------------------------------
# After CB_THRESHOLD consecutive failures on a single key, quarantine it for
# CB_QUARANTINE seconds instead of the short RETRY_AFTER_MAX cooldown.
# This prevents infinite retry loops when all keys are rate-limited:
# after 3 failures per key the proxy returns a fast 503 to Hermes instead
# of blocking for up to 65s per retry.
CB_THRESHOLD  = 3    # consecutive failures before circuit trips
CB_QUARANTINE = 300  # 5 minutes in long quarantine after circuit trips

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
# Maps key string -> float (time.time() when key becomes available again).
# Thread-safe: all access protected by _cooldown_lock.
_key_cooldowns = {}
_cooldown_lock = threading.Lock()

# -- Per-key circuit breaker -------------------------------------------------
# Maps key string -> consecutive failure count.
# Thread-safe: all access protected by _cb_lock.
_key_failures = {}
_cb_lock = threading.Lock()


def _key_label(key):
    """Return a safe loggable label for a key (last 4 chars only)."""
    return ("..." + key[-4:]) if len(key) >= 4 else "????"


def _mark_key_cooldown(key, seconds):
    """Mark key unavailable for `seconds` seconds from now."""
    available_at = time.time() + seconds
    with _cooldown_lock:
        _key_cooldowns[key] = available_at
    log.info("Cooldown: key=%s rate-limited for %.1fs", _key_label(key), seconds)


def _record_key_failure(key, reason=""):
    """
    Increment consecutive failure count for key.
    If CB_THRESHOLD is reached, apply CB_QUARANTINE (long quarantine).

    ROOT CAUSE FIX (Jul 2026): Previously the proxy had no concept of
    escalating failures. A key could fail 100 times and still only get a
    10s cooldown. With the circuit breaker, 3 consecutive failures trigger
    a 5-minute quarantine. If ALL keys trip the circuit breaker, the proxy
    returns 503 immediately, ending Hermes's retry storm without blocking.

    Returns the new consecutive failure count.
    """
    with _cb_lock:
        count = _key_failures.get(key, 0) + 1
        _key_failures[key] = count

    if count >= CB_THRESHOLD:
        _mark_key_cooldown(key, CB_QUARANTINE)
        log.error(
            "CIRCUIT BREAKER TRIPPED: key=%s has %d consecutive failures%s. "
            "Quarantined for %ds (%.1f min). This key is likely rate-limited, "
            "exhausted, or invalid.",
            _key_label(key), count,
            (" (" + reason + ")") if reason else "",
            CB_QUARANTINE, CB_QUARANTINE / 60,
        )
    return count


def _record_key_success(key):
    """Reset consecutive failure count on a successful response."""
    with _cb_lock:
        prev = _key_failures.pop(key, 0)
    if prev > 0:
        log.info(
            "Circuit breaker reset: key=%s recovered after %d failures",
            _key_label(key), prev,
        )


def _all_keys_circuit_broken():
    """
    Return (all_broken: bool, earliest_reset: float).

    If all_broken is True, every key is in cooldown (circuit-broken or
    rate-limited). Caller should return 503 immediately instead of blocking
    for next_available_key()'s targeted sleep. This is the key mechanism
    that prevents Hermes from freezing when the Cerebras API is unavailable.
    """
    now = time.time()
    with _cooldown_lock:
        snap = dict(_key_cooldowns)
    broken = [k for k in KEYS if now < snap.get(k, 0.0)]
    if len(broken) < len(KEYS):
        return False, 0.0
    earliest_reset = min(snap.get(k, now) for k in KEYS)
    return True, earliest_reset


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
      1. Scan all N keys starting from the current counter.
      2. Return the first key whose cooldown has expired (or was never set).
      3. If ALL keys are in cooldown, wait until the earliest one resets
         (single targeted sleep -- NOT N_KEYS x Retry-After serially).
      4. Clear expired cooldowns and return the next key.

    ROOT CAUSE FIX (Jun 2026): Replaced the old next_key() which had no
    cooldown awareness and caused serial blocking: key1 30s -> key2 30s
    -> ... = 150s+. New: mark cooldown, wait once for earliest expiry,
    max 10s regardless of how many keys fail simultaneously.
    """
    global _counter
    now = time.time()

    with _cooldown_lock:
        snap = dict(_key_cooldowns)

    for _ in range(len(KEYS)):
        with _counter_lock:
            idx = _counter % len(KEYS)
            _counter += 1
        key = KEYS[idx]
        if now >= snap.get(key, 0.0):
            return key

    # All keys in cooldown -- single targeted wait
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
# CORE REQUEST HANDLER WITH COOLDOWN-AWARE KEY ROTATION + CIRCUIT BREAKER
# ----------------------------------------------------------------------------

def try_request(method, path, headers, body):
    """
    Attempt the request with full key rotation, per-key cooldown awareness,
    circuit breaker fast-fail, 429 backoff, and 5xx retry.

    FAST-FAIL (Jul 2026 fix):
      If ALL keys are circuit-broken (3+ consecutive failures each), return
      HTTP 503 immediately. This ends Hermes's retry loop in <1s instead of
      blocking for up to 65s per retry. Combined with api_max_retries=2 in
      hermes_setup.py, the maximum agent freeze time drops from 455s to ~2s.

    Strategy per cycle (runs up to 2 cycles):
      429: mark key in cooldown for min(Retry-After, RETRY_AFTER_MAX)s.
           Increment failure count; circuit trips at CB_THRESHOLD failures.
      5xx: wait 2s, rotate key, increment failure count.
      401/403: permanent quarantine (24h cooldown), skip key.
      Network/timeout: skip key, increment failure count.
      Success: reset failure count for that key.
      Cycle 0: try every key using next_available_key().
      Cycle 1: all keys exhausted -> wait 5s, retry once more.
               Re-check fast-fail before cycle 1 begins.
    """
    # FAST-FAIL: if all keys are circuit-broken, return 503 immediately
    all_broken, reset_at = _all_keys_circuit_broken()
    if all_broken:
        wait_s = max(0, reset_at - time.time())
        msg = (
            "All {} Cerebras key(s) are circuit-broken (reset in {:.0f}s). "
            "Returning 503 immediately to prevent agent freeze. "
            "Check /v1/keys/status for diagnostics.".format(len(KEYS), wait_s)
        )
        log.error(msg)
        err_body = _json.dumps({
            "error": {"message": msg, "type": "rate_limit_error", "code": "all_keys_exhausted"}
        }).encode()
        return 503, {"Content-Type": "application/json"}, err_body

    ctx = ssl.create_default_context()
    last_err = None
    n_keys = len(KEYS)

    for cycle in range(2):
        if cycle == 1:
            log.warning("All %d key(s) exhausted in cycle 0 -- waiting 5s for quota reset", n_keys)
            time.sleep(5)

            # Re-check fast-fail: circuit breakers may have tripped during cycle 0
            all_broken, reset_at = _all_keys_circuit_broken()
            if all_broken:
                wait_s = max(0, reset_at - time.time())
                msg = (
                    "All {} key(s) circuit-broken after cycle 0 exhaustion "
                    "(reset in {:.0f}s).".format(len(KEYS), wait_s)
                )
                log.error(msg)
                err_body = _json.dumps({
                    "error": {"message": msg, "type": "rate_limit_error"}
                }).encode()
                return 503, {"Content-Type": "application/json"}, err_body

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
                status = resp.status
                resp_body = resp.read()
                conn.close()

                if status == 429:
                    raw_ra = (resp.headers.get("Retry-After")
                              or resp.headers.get("retry-after")
                              or "5")
                    try:
                        retry_after = float(raw_ra)
                    except ValueError:
                        retry_after = 5.0
                    cooldown_secs = min(retry_after, RETRY_AFTER_MAX)
                    _mark_key_cooldown(key, cooldown_secs)
                    failures = _record_key_failure(key, "429")
                    log.warning(
                        "429 key=%s retry_after=%.1fs cooldown=%.1fs "
                        "consecutive_failures=%d (circuit trips at %d)",
                        _key_label(key), retry_after, cooldown_secs,
                        failures, CB_THRESHOLD,
                    )
                    last_err = "429 key={}".format(_key_label(key))
                    continue

                if status in _RETRYABLE_5XX:
                    log.warning(
                        "HTTP %d from key=%s -- backing off 2s", status, _key_label(key)
                    )
                    failures = _record_key_failure(key, str(status))
                    time.sleep(2)
                    last_err = "HTTP {}".format(status)
                    continue

                if status in (401, 403):
                    log.error(
                        "Key %s rejected with HTTP %d (bad/revoked) -- "
                        "permanently quarantining for this session",
                        _key_label(key), status,
                    )
                    _mark_key_cooldown(key, 86400)  # 24h = permanent for this run
                    _record_key_failure(key, "auth_{}".format(status))
                    last_err = "HTTP {} auth error".format(status)
                    continue

                # Success -- reset circuit breaker and return
                _record_key_success(key)
                resp_body = strip_response_reasoning(resp_body)
                resp_headers = {}
                for h in ("Content-Type", "content-type"):
                    v = resp.headers.get(h)
                    if v:
                        resp_headers["Content-Type"] = v
                        break
                if not resp_headers.get("Content-Type"):
                    resp_headers["Content-Type"] = "application/json"
                log.info(
                    "OK: %s %s -> HTTP %d (%d bytes) key=%s",
                    method, path, status, len(resp_body), _key_label(key),
                )
                return status, resp_headers, resp_body

            except (socket.timeout, TimeoutError) as e:
                log.warning("Timeout key=%s path=%s: %s", _key_label(key), path, e)
                _record_key_failure(key, "timeout")
                last_err = "timeout: {}".format(e)
            except Exception as e:
                log.warning("Network error key=%s: %s", _key_label(key), e)
                _record_key_failure(key, "network")
                last_err = "error: {}".format(e)

    err_body = _json.dumps({
        "error": {
            "message": (
                "All Cerebras keys failed after 2 cycles. "
                "Last error: {}. Check proxy logs for details.".format(last_err)
            ),
            "type": "provider_error",
        }
    }).encode()
    log.error("All keys and cycles exhausted. Last error: %s", last_err)
    return 502, {"Content-Type": "application/json"}, err_body


# ----------------------------------------------------------------------------
# HTTP SERVER + HANDLER
# ----------------------------------------------------------------------------

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default Apache-style access log; we use our own

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            self._send_health()
            return
        if self.path == "/v1/keys/status":
            self._send_key_status()
            return
        if self.path.startswith("/v1/models"):
            self._do_proxy("GET", self.path, b"")
            return
        self._send_raw(404, b"Not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        body = sanitize_request_body(body, self.path)
        self._do_proxy("POST", self.path, body)

    def _do_proxy(self, method, path, body):
        fwd_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("host", "connection", "authorization")
        }
        fwd_headers["Host"] = UPSTREAM_HOST
        fwd_headers["Content-Length"] = str(len(body))
        status, resp_headers, resp_body = try_request(method, path, fwd_headers, body)
        self.send_response(status)
        for k, v in resp_headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def _send_raw(self, status, body, ctype="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_health(self):
        """
        GET /health -- Returns JSON with overall status and per-key circuit state.
        HTTP 200 if at least one key is available, HTTP 503 if all are broken.
        Used by the GitHub Actions watchdog to determine if the proxy is healthy
        before declaring Hermes frozen.
        """
        now = time.time()
        with _cooldown_lock:
            snap_cd = dict(_key_cooldowns)
        with _cb_lock:
            snap_f = dict(_key_failures)

        available = sum(1 for k in KEYS if now >= snap_cd.get(k, 0.0))
        healthy = available > 0
        status_str = "ok" if healthy else "degraded"

        key_details = {}
        for i, k in enumerate(KEYS):
            cd_remaining = max(0.0, snap_cd.get(k, now) - now)
            failures = snap_f.get(k, 0)
            key_details["key_{}".format(i)] = {
                "label": _key_label(k),
                "available": now >= snap_cd.get(k, 0.0),
                "cooldown_remaining_s": round(cd_remaining, 1),
                "consecutive_failures": failures,
                "circuit_tripped": failures >= CB_THRESHOLD,
                "in_long_quarantine": cd_remaining > RETRY_AFTER_MAX,
            }

        body = _json.dumps({
            "status": status_str,
            "keys_total": len(KEYS),
            "keys_available": available,
            "keys_in_cooldown": len(KEYS) - available,
            "cb_threshold": CB_THRESHOLD,
            "cb_quarantine_s": CB_QUARANTINE,
            "circuit_breakers": key_details,
        }, indent=2).encode()
        http_status = 200 if healthy else 503
        self._send_raw(http_status, body, "application/json")

    def _send_key_status(self):
        """
        GET /v1/keys/status -- Detailed per-key diagnostic.
        Useful for debugging which keys are rate-limited or broken.
        """
        now = time.time()
        with _cooldown_lock:
            snap_cd = dict(_key_cooldowns)
        with _cb_lock:
            snap_f = dict(_key_failures)

        rows = []
        for i, k in enumerate(KEYS):
            cd_rem = max(0.0, snap_cd.get(k, now) - now)
            failures = snap_f.get(k, 0)
            rows.append({
                "index": i,
                "label": _key_label(k),
                "available": now >= snap_cd.get(k, 0.0),
                "cooldown_remaining_s": round(cd_rem, 1),
                "consecutive_failures": failures,
                "circuit_tripped": failures >= CB_THRESHOLD,
                "in_long_quarantine": cd_rem > RETRY_AFTER_MAX,
            })
        body = _json.dumps({"keys": rows, "cb_threshold": CB_THRESHOLD}, indent=2).encode()
        self._send_raw(200, body, "application/json")


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if not KEYS:
        log.error(
            "FATAL: no Cerebras API keys found. Set CEREBRAS_API_KEY and/or "
            "CEREBRAS_API_KEY_2 ... CEREBRAS_API_KEY_5 in environment."
        )
        sys.exit(1)

    log.info(
        "Cerebras key-rotation proxy v2 starting on 127.0.0.1:%d | "
        "%d key(s) | CB_THRESHOLD=%d failures | CB_QUARANTINE=%ds (%.1fmin) | "
        "RETRY_AFTER_MAX=%ds | CONNECT_TIMEOUT=%ds",
        PROXY_PORT, len(KEYS), CB_THRESHOLD, CB_QUARANTINE, CB_QUARANTINE / 60,
        RETRY_AFTER_MAX, CONNECT_TIMEOUT,
    )
    log.info(
        "Endpoints: /health (key status), /v1/keys/status (detailed diagnostics)"
    )

    server = ThreadedHTTPServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Proxy shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
