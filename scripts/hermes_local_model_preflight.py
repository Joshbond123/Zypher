#!/usr/bin/env python3
"""Validate the local Ollama OpenAI-compatible API used by Hermes."""
import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1:7860/v1"
MODEL = "Qwen3.5-9B-Uncensored-Q4_K_M"
API_KEY = "local-qwen"


def request_json(path, payload=None, timeout=180):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main():
    print("Testing local Ollama OpenAI-compatible API...")
    try:
        models = request_json("/models", timeout=30).get("data", [])
        ids = [m.get("id") for m in models]
        print("Models: {}".format(", ".join(ids) or "(none)"))
        advertised = any(mid == MODEL or mid == f"{MODEL}:latest" or str(mid).split(":", 1)[0] == MODEL for mid in ids)
        if not advertised:
            print("ERROR: expected model {!r} not advertised".format(MODEL))
            sys.exit(1)

        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Reply with exactly: qwen-live"}],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
        }
        data = request_json("/chat/completions", payload=payload, timeout=180)
        reply = data["choices"][0]["message"]["content"].strip()
        print("Chat OK: {!r}".format(reply[:80]))
        print("Local Qwen preflight: PASSED")
    except HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        print("ERROR: HTTP {}: {}".format(exc.code, body))
        sys.exit(1)
    except Exception as exc:
        print("ERROR: {}".format(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
