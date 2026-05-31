#!/usr/bin/env python3
"""
Thread-safe Groq API key rotation proxy for Hermes agent.

Loads GROQ_KEY_1..N env vars and rotates them in strict round-robin on
every incoming request. On HTTP 429 (rate-limit), the current key is skipped
and the next key is tried immediately — up to N times before returning 503.

On HTTP 413 (payload too large), the proxy automatically trims the messages
array and retries — up to 3 progressive trim passes before returning 413.

Listens on 127.0.0.1:GROQ_PROXY_PORT (default 18765).
Forwards all requests to https://api.groq.com/openai/v1 with streaming support.

FIX (2026-05): Added automatic 413 payload trimming.
  ROOT CAUSE: Hermes sets context_length=131072 and compression.threshold=0.75,
  meaning compression only fires at ~98K tokens. By that point the payload is
  already too large for the Groq API to accept, so the compression API call
  itself returns 413 — causing the retry/fallback loop the user sees.

  FIX: The proxy intercepts 413 responses, progressively trims the messages
  array (50% → 25% → last 4 messages), and retries before returning the error.
  This breaks the 413 → fallback → 413 → reset loop entirely.
"""

import itertools
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

GROQ_BASE = "https://api.groq.com/openai"  # path already contains /v1 prefix
PROXY_HOST = "127.0.0.1"
PROXY_PORT = int(os.environ.get("GROQ_PROXY_PORT", "18765"))

# Maximum trim passes before giving up and returning 413 to caller.
# Pass 1: keep newest 50% of conversation turns
# Pass 2: keep newest 25% of conversation turns
# Pass 3: keep last 4 messages only (2 exchange pairs)
MAX_TRIM_PASSES = 3

logging.basicConfig(
    level=logging.INFO,
    format="[groq-proxy %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("groq-proxy")


def _load_keys() -> list:
    keys = []
    for i in range(1, 20):
        k = os.environ.get(f"GROQ_KEY_{i}", "").strip()
        if k:
            keys.append(k)
    if not keys:
        raise RuntimeError(
            "No Groq API keys found. "
            "Set GROQ_KEY_1, GROQ_KEY_2, ... as environment variables."
        )
    return keys


_keys = _load_keys()
_key_cycle = itertools.cycle(_keys)
_lock = threading.Lock()
_request_count = 0
_request_lock = threading.Lock()


def _next_key() -> str:
    """Return next API key in the round-robin cycle (thread-safe)."""
    with _lock:
        return next(_key_cycle)


_HOP_BY_HOP = frozenset([
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding",
    "upgrade", "authorization",
])


def _trim_messages(body_bytes: bytes, pass_number: int):
    """
    Progressively trim the messages array in a chat completion request payload.

    Pass 1: keep newest 50% of non-system messages
    Pass 2: keep newest 25% of non-system messages
    Pass 3: keep last 4 non-system messages (2 exchange pairs minimum)

    System messages are always preserved — they contain the agent identity,
    memory, and SOUL.md content that must be present in every request.

    Returns: trimmed body bytes, or None if cannot trim further.
    """
    if not body_bytes:
        return None

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return None

    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) <= 4:
        return None  # Too few messages to trim meaningfully

    # Partition: always keep system messages in their original positions
    system_msgs = [m for m in messages if m.get("role") == "system"]
    conv_msgs = [m for m in messages if m.get("role") != "system"]

    if len(conv_msgs) <= 2:
        return None  # Nothing non-system to trim

    # Determine how many conversation messages to keep
    if pass_number == 1:
        keep_count = max(4, int(len(conv_msgs) * 0.50))
    elif pass_number == 2:
        keep_count = max(4, int(len(conv_msgs) * 0.25))
    else:
        keep_count = 4  # Absolute minimum: keep last 2 exchange pairs

    if keep_count >= len(conv_msgs):
        return None  # Already at or below target — can't trim further

    trimmed_conv = conv_msgs[-keep_count:]
    dropped = len(conv_msgs) - keep_count

    # Reconstruct: system messages first, then trimmed conversation
    payload["messages"] = system_msgs + trimmed_conv

    new_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    log.info(
        "  trim pass %d: dropped %d messages (%d→%d msgs, %d→%d bytes)",
        pass_number, dropped, len(messages), len(payload["messages"]),
        len(body_bytes), len(new_body),
    )
    return new_body


class GroqProxyHandler(BaseHTTPRequestHandler):
    server_version = "HermesGroqProxy/1.0"

    def log_message(self, fmt, *args):
        pass  # suppress default per-request noise; we log ourselves

    def _forward(self):
        global _request_count
        with _request_lock:
            _request_count += 1
            req_id = _request_count

        length = int(self.headers.get("Content-Length", 0) or 0)
        original_body = self.rfile.read(length) if length > 0 else None
        content_type = self.headers.get("Content-Type", "application/json")

        fwd_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        fwd_headers["Content-Type"] = content_type
        fwd_headers["User-Agent"] = "hermes-groq-proxy/1.0"

        target_url = GROQ_BASE + self.path
        is_chat = "/chat/completions" in self.path

        # Outer loop: trim passes (0=original, 1-3=progressive trimming)
        current_body = original_body
        for trim_pass in range(MAX_TRIM_PASSES + 1):
            if trim_pass > 0:
                # Attempt to compress the payload before next round of key retries
                log.warning(
                    "req#%d — 413 payload too large, attempting trim pass %d/%d",
                    req_id, trim_pass, MAX_TRIM_PASSES,
                )
                if is_chat and current_body:
                    trimmed = _trim_messages(current_body, pass_number=trim_pass)
                    if trimmed:
                        current_body = trimmed
                    else:
                        log.warning(
                            "req#%d — cannot trim further after %d pass(es) — returning 413",
                            req_id, trim_pass,
                        )
                        self._send_error(
                            413,
                            f"Request payload too large — cannot compress further "
                            f"after {trim_pass} trim attempt(s). "
                            f"Session history must be reset to continue.",
                        )
                        return
                else:
                    # Non-chat request or no body — cannot trim
                    self._send_error(413, "Request payload too large (non-chat or empty body)")
                    return

            # Inner loop: key rotation (handles 429 rate limits)
            max_tries = len(_keys)
            for attempt in range(max_tries):
                api_key = _next_key()
                key_hint = f"{api_key[:8]}...{api_key[-4:]}"
                fwd_headers["Authorization"] = f"Bearer {api_key}"
                if current_body is not None:
                    fwd_headers["Content-Length"] = str(len(current_body))

                log.info(
                    "req#%d %s %s → key %s (trim=%d attempt=%d/%d)",
                    req_id, self.command, self.path, key_hint,
                    trim_pass, attempt + 1, max_tries,
                )

                try:
                    resp = requests.request(
                        method=self.command,
                        url=target_url,
                        headers=fwd_headers,
                        data=current_body,
                        stream=True,
                        timeout=180,
                    )
                except requests.RequestException as exc:
                    log.warning("req#%d upstream error: %s", req_id, exc)
                    if attempt < max_tries - 1:
                        continue
                    self._send_error(503, f"Upstream connection failed: {exc}")
                    return

                if resp.status_code == 429 and attempt < max_tries - 1:
                    log.warning(
                        "req#%d 429 on key %s — rotating to next key",
                        req_id, key_hint,
                    )
                    try:
                        resp.close()
                    except Exception:
                        pass
                    continue  # try next key

                if resp.status_code == 413:
                    # Payload too large — exit key-rotation loop, go to next trim pass
                    log.warning(
                        "req#%d 413 received on key %s (trim_pass=%d) — will trim and retry",
                        req_id, key_hint, trim_pass,
                    )
                    try:
                        resp.close()
                    except Exception:
                        pass
                    break  # break inner key loop → next trim_pass

                # Success or unhandled error — forward to caller
                self._stream_response(resp, req_id)
                return  # done

            else:
                # Inner loop exhausted without 413 — all keys rate-limited
                self._send_error(429, "All API keys exhausted after rate-limit retries")
                return

        # All trim passes exhausted (should not reach here due to return inside loop)
        self._send_error(
            413,
            f"Request payload too large after {MAX_TRIM_PASSES} trim attempts. "
            "Session reset required.",
        )

    def _stream_response(self, resp, req_id):
        """Stream an upstream response back to the caller."""
        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() in ("transfer-encoding", "connection",
                             "keep-alive", "content-encoding"):
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
            log.debug("req#%d client disconnected mid-stream", req_id)
        finally:
            try:
                resp.close()
            except Exception:
                pass

        log.info("req#%d done → HTTP %d (%d bytes)", req_id, resp.status_code, bytes_sent)

    def _send_error(self, code, msg):
        body = json.dumps({"error": {"message": msg, "type": "proxy_error"}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _forward


class ThreadedHTTPServer(HTTPServer):
    """Handle each request in its own daemon thread."""
    def process_request(self, request, client_address):
        t = threading.Thread(
            target=self._handle_thread,
            args=(request, client_address),
            daemon=True,
        )
        t.start()

    def _handle_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            pass
        finally:
            self.shutdown_request(request)


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Groq API Key Rotation Proxy  (413-trim enabled)")
    log.info("Listening : %s:%d", PROXY_HOST, PROXY_PORT)
    log.info("Target    : %s", GROQ_BASE)
    log.info("Keys      : %d loaded (round-robin per request)", len(_keys))
    for i, k in enumerate(_keys, 1):
        log.info("  [%d] %s...%s", i, k[:8], k[-4:])
    log.info("413 trim  : up to %d progressive passes before failing", MAX_TRIM_PASSES)
    log.info("  pass 1  : keep newest 50%% of conversation turns")
    log.info("  pass 2  : keep newest 25%% of conversation turns")
    log.info("  pass 3  : keep last 4 messages only")
    log.info("=" * 60)
    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), GroqProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Proxy shutting down.")
