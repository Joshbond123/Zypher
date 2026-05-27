#!/usr/bin/env python3
"""
hermes_key_proxy.py — Local OpenAI-compatible proxy with per-request Cerebras key rotation.

Listens on localhost:7860/v1
Every request gets a fresh Cerebras API key (round-robin across all configured keys).
Handles streaming (SSE) responses transparently.
If a key returns 429/401/403, it is skipped and the next key is tried automatically.
"""
import http.server
import http.client
import threading
import itertools
import os
import ssl
import logging
import sys

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

_counter = 0
_counter_lock = threading.Lock()


def next_key() -> str:
    global _counter
    with _counter_lock:
        key = KEYS[_counter % len(KEYS)]
        _counter += 1
    return key


def try_request(method, path, headers, body, retries=3):
    ctx = ssl.create_default_context()
    last_err = None
    tried = set()

    for attempt in range(retries):
        key = next_key()
        if key in tried and len(tried) >= len(KEYS):
            break
        tried.add(key)

        req_headers = dict(headers)
        req_headers["Authorization"] = f"Bearer {key}"

        try:
            conn = http.client.HTTPSConnection(
                UPSTREAM_HOST, UPSTREAM_PORT, context=ctx, timeout=120
            )
            conn.request(method, path, body=body, headers=req_headers)
            resp = conn.getresponse()

            if resp.status in (401, 403, 429) and attempt < retries - 1:
                log.warning("Key attempt %d got HTTP %d — rotating", attempt + 1, resp.status)
                resp.read()
                conn.close()
                continue

            return conn, resp
        except Exception as e:
            last_err = e
            log.warning("Upstream error attempt %d: %s", attempt + 1, e)
            try:
                conn.close()
            except Exception:
                pass

    raise RuntimeError(f"All key attempts failed. Last: {last_err}")


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _proxy(self, method):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None
        path = self.path

        fwd_headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept":       self.headers.get("Accept", "application/json, text/event-stream"),
            "User-Agent":   "hermes-key-proxy/1.0",
        }
        if body:
            fwd_headers["Content-Length"] = str(len(body))

        try:
            conn, resp = try_request(method, path, fwd_headers, body)
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
            self.send_header("X-Key-Proxy", "cerebras-rotating")
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


def main():
    if not KEYS:
        log.error("No Cerebras API keys configured. Set CEREBRAS_API_KEY env vars.")
        sys.exit(1)
    log.info("Key-rotation proxy on 127.0.0.1:%d with %d key(s)", PROXY_PORT, len(KEYS))
    log.info("Upstream: https://%s", UPSTREAM_HOST)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
