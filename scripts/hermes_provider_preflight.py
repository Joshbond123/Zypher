#!/usr/bin/env python3
"""hermes_provider_preflight.py — Cerebras API validation with robust fallback."""
import os,json,sys
from urllib.request import urlopen,Request
from urllib.error import HTTPError

KEY=os.environ.get("CEREBRAS_API_KEY","")
# Models to try in order — tested against current Cerebras API
PREFLIGHT_MODELS=["llama3.1-8b","qwen-3-235b-a22b-instruct-2507"]

def list_models():
    try:
        with urlopen(Request("https://api.cerebras.ai/v1/models",headers={"Authorization":f"Bearer {KEY}"}),timeout=15) as r:
            models=[m["id"] for m in json.loads(r.read()).get("data",[])]
            print(f"Available models: {', '.join(models)}")
            return models
    except Exception as e:
        print(f"WARN: could not list models: {e}")
        return []

def test_chat(model):
    payload=json.dumps({"model":model,"messages":[{"role":"user","content":"hi"}],"max_tokens":5}).encode()
    try:
        with urlopen(Request("https://api.cerebras.ai/v1/chat/completions",data=payload,
                             headers={"Authorization":f"Bearer {KEY}","Content-Type":"application/json"}),timeout=20) as r:
            reply=json.loads(r.read())["choices"][0]["message"]["content"]
            print(f"Chat OK [{model}]: {repr(reply[:30])}")
            return True
    except HTTPError as e:
        print(f"WARN: chat failed [{model}]: HTTP {e.code} {e.read().decode()[:100]}")
        return False
    except Exception as e:
        print(f"WARN: chat error [{model}]: {e}")
        return False

def check():
    if not KEY:print("ERROR: CEREBRAS_API_KEY not set");sys.exit(1)
    print("Testing Cerebras API...")
    models=list_models()
    if not models:
        print("ERROR: could not retrieve model list — API key invalid or network issue")
        sys.exit(1)
    # Try preflight models, fall back to first available
    for m in PREFLIGHT_MODELS:
        if m in models and test_chat(m):
            print("Cerebras preflight: PASSED");return
    # Try first available model as last resort
    for m in models:
        if test_chat(m):
            print(f"Cerebras preflight: PASSED (fallback model {m})");return
    print("ERROR: all Cerebras models failed chat test")
    sys.exit(1)

if __name__=="__main__":check()
