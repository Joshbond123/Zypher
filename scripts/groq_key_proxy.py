#!/usr/bin/env python3
"""
Thread-safe Groq API key rotation proxy for Hermes agent.

Loads GROQ_KEY_1..N env vars and rotates them in strict round-robin on
every incoming request. On HTTP 429 (rate-limit), the current key is skipped
and the next key is tried immediately — up to N times before returning 503.

Listens on 127.0.0.1:GROQ_PROXY_PORT (default 18765).
Forwards all requests to https://api.groq.com/openai/v1 with streaming support.
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
        body = self.rfile.read(length) if length > 0 else None
        content_type = self.headers.get("Content-Type", "application/json")

        fwd_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        fwd_headers["Content-Type"] = content_type
        fwd_headers["User-Agent"] = "hermes-groq-proxy/1.0"

        target_url = GROQ_BASE + self.path
        max_tries = len(_keys)

        for attempt in range(max_tries):
            api_key = _next_key()
            key_hint = f"{api_key[:8]}...{api_key[-4:]}"
            fwd_headers["Authorization"] = f"Bearer {api_key}"

            log.info("req#%d %s %s → key %s (attempt %d/%d)",
                     req_id, self.command, self.path, key_hint, attempt + 1, max_tries)

            try:
                resp = requests.request(
                    method=self.command,
                    url=target_url,
                    headers=fwd_headers,
                    data=body,
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
                log.warning("req#%d 429 on key %s — rotating to next key",
                            req_id, key_hint)
                try:
                    resp.close()
                except Exception:
                    pass
                continue

            # Forward status + headers
            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() in ("transfer-encoding", "connection",
                                 "keep-alive", "content-encoding"):
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()

            # Stream the response body
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
            return

        self._send_error(429, "All API keys exhausted after rate-limit retries")

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
    log.info("Groq API Key Rotation Proxy")
    log.info("Listening : %s:%d", PROXY_HOST, PROXY_PORT)
    log.info("Target    : %s", GROQ_BASE)
    log.info("Keys      : %d loaded (round-robin per request)", len(_keys))
    for i, k in enumerate(_keys, 1):
        log.info("  [%d] %s...%s", i, k[:8], k[-4:])
    log.info("=" * 60)
    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), GroqProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Proxy shutting down.")
