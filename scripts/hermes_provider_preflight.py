#!/usr/bin/env python3
  """hermes_provider_preflight.py — Cerebras API validation."""
  import os,json,sys
  from urllib.request import urlopen,Request
  from urllib.error import HTTPError
  KEY=os.environ.get("CEREBRAS_API_KEY","")
  def check():
      if not KEY:print("ERROR: CEREBRAS_API_KEY not set");sys.exit(1)
      print("Testing Cerebras API...")
      try:
          with urlopen(Request("https://api.cerebras.ai/v1/models",headers={"Authorization":f"Bearer {KEY}"}),timeout=15) as r:
              models=[m["id"] for m in json.loads(r.read()).get("data",[])];print(f"Models: {', '.join(models[:5])}")
          payload=json.dumps({"model":"llama3.1-8b","messages":[{"role":"user","content":"hi"}],"max_tokens":5}).encode()
          with urlopen(Request("https://api.cerebras.ai/v1/chat/completions",data=payload,headers={"Authorization":f"Bearer {KEY}","Content-Type":"application/json"}),timeout=20) as r:
              reply=json.loads(r.read())["choices"][0]["message"]["content"];print(f"Chat OK: {repr(reply[:20])}")
          print("Cerebras preflight: PASSED")
      except HTTPError as e:print(f"ERROR: {e.code}: {e.read().decode()[:200]}");sys.exit(1)
      except Exception as e:print(f"ERROR: {e}");sys.exit(1)
  if __name__=="__main__":check()
  