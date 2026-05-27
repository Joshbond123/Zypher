#!/usr/bin/env python3
"""
hermes_key_proxy.py — Local OpenAI-compatible proxy with per-request Cerebras key rotation.

Listens on localhost:7860/v1
Every request gets a fresh Cerebras API key (round-robin across all configured keys).
Handles streaming (SSE) responses transparently.

Rate-limit strategy (fixed May 2026):
  - On HTTP 429, read Retry-After header (default: 5s), wait, then try the NEXT key.
  - If ALL keys return 429, wait up to 30s total before giving up.
  - This ensures transient rate-limit blips are absorbed by the proxy and never
    seen by Hermes as a provider failure that triggers a fallback cascade.
  - The key insight: Cerebras's 60K TPM free-tier budget resets every 60 seconds.
    Waiting a few seconds typically restores quota.
"""
import http.server
import http.client
import threading
import itertools
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

_counter = 0
_counter_lock = threading.Lock()


def next_key() -> str:
    global _counter
    with _counter_lock:
        key = KEYS[_counter % len(KEYS)]
        _counter += 1
    return key


def try_request(method, path, headers, body):
    """
    Attempt the request with key rotation and 429 backoff.

    Strategy:
      1. Try each key in rotation.
      2. On 429: read Retry-After header, wait, rotate to next key.
      3. After all keys are exhausted on 429, wait 10s and retry the full cycle once.
      4. On 401/403: skip key immediately (bad key), don't wait.
      5. Other errors: retry up to 3 times total.
    """
    ctx = ssl.create_default_context()
    last_err = None
    n_keys = len(KEYS)

    # Two full passes: first pass tries every key, second pass is a 10s-wait retry.
    for cycle in range(2):
        if cycle == 1:
            log.warning("All %d key(s) rate-limited — waiting 10s for quota reset...", n_keys)
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
                    body_bytes = resp.read()
                    conn.close()
                    retry_after = resp.getheader("Retry-After", "")
                    try:
                        wait = min(int(retry_after), 30)
                    except (ValueError, TypeError):
                        wait = 5
                    log.warning(
                        "Key[%d/%d] rate-limited (429) — waiting %ds then rotating",
                        ki + 1, n_keys, wait,
                    )
                    time.sleep(wait)
                    continue

                if resp.status in (401, 403):
                    log.warning(
                        "Key[%d/%d] auth error HTTP %d — skipping",
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
        f"Likely sustained rate-limit — try adding more CEREBRAS_API_KEY_N env vars. "
        f"Last error: {last_err}"
    )


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
    log.info("Rate-limit strategy: wait Retry-After per key, then 10s cycle retry")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
