#!/usr/bin/env python3
  """
  hermes_key_rotator.py — Round-robin Cerebras API key selection for Hermes Agent.
  """
  import os, json, sys
  from urllib.request import urlopen, Request
  from urllib.error import HTTPError

  KEYS = [k for k in [
      os.environ.get("CEREBRAS_API_KEY",""),
      os.environ.get("CEREBRAS_API_KEY_2",""),
      os.environ.get("CEREBRAS_API_KEY_3",""),
      os.environ.get("CEREBRAS_API_KEY_4",""),
  ] if k.strip()]

  RUN_ID     = os.environ.get("GITHUB_RUN_ID", "1")
  HERMES_ENV = os.path.expanduser("~/.hermes/.env")


  def test_key(key):
      try:
          req = Request("https://api.cerebras.ai/v1/models", headers={"Authorization": f"Bearer {key}"})
          with urlopen(req, timeout=10) as r:
              return bool(json.loads(r.read()).get("data"))
      except: return False


  def select_key():
      if not KEYS:
          print("ERROR: No Cerebras keys found"); sys.exit(1)
      idx = int(RUN_ID) % len(KEYS) if RUN_ID.isdigit() else 0
      primary = KEYS[idx]
      print(f"Round-robin key index {idx} (run={RUN_ID})")
      if test_key(primary):
          print(f"Key {idx} healthy"); return primary
      for i, k in enumerate(KEYS):
          if i == idx: continue
          if test_key(k):
              print(f"Fallback key {i} healthy"); return k
      print("WARN: all keys failed, using key 0"); return KEYS[0]


  def update_env(key):
      if not os.path.exists(HERMES_ENV):
          print(f"WARN: {HERMES_ENV} not found"); return
      lines = open(HERMES_ENV).readlines()
      updated = [f"CEREBRAS_API_KEY={key}\n" if l.startswith("CEREBRAS_API_KEY=") else l for l in lines]
      if not any(l.startswith("CEREBRAS_API_KEY=") for l in lines):
          updated.append(f"CEREBRAS_API_KEY={key}\n")
      open(HERMES_ENV, "w").writelines(updated)
      print("Key updated in ~/.hermes/.env")


  if __name__ == "__main__":
      print(f"Key rotator: {len(KEYS)} keys")
      k = select_key()
      update_env(k)
      print("Key rotation complete")
  