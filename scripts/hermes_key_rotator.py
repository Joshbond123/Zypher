#!/usr/bin/env python3
"""hermes_key_rotator.py — Round-robin Cerebras key rotation."""
import os,json,sys
from urllib.request import urlopen,Request
KEYS=[k for k in[os.environ.get(f"CEREBRAS_API_KEY{'' if i==0 else f'_{i+1}'}","") for i in range(4)] if k.strip()]
RUN_ID=os.environ.get("GITHUB_RUN_ID","1");ENV=os.path.expanduser("~/.hermes/.env")
def test_key(k):
    try:
        with urlopen(Request("https://api.cerebras.ai/v1/models",headers={"Authorization":f"Bearer {k}"}),timeout=10) as r:
            return bool(json.loads(r.read()).get("data"))
    except:return False
def select():
    if not KEYS:print("ERROR: no keys");sys.exit(1)
    idx=int(RUN_ID)%len(KEYS) if RUN_ID.isdigit() else 0
    print(f"Round-robin idx={idx} run={RUN_ID} keys={len(KEYS)}")
    if test_key(KEYS[idx]):print(f"Key {idx} healthy");return KEYS[idx]
    for i,k in enumerate(KEYS):
        if i==idx:continue
        if test_key(k):print(f"Fallback key {i}");return k
    print("WARN: all failed, using key 0");return KEYS[0]
def update(k):
    if not os.path.exists(ENV):return
    lines=open(ENV).readlines()
    updated=[f"CEREBRAS_API_KEY={k}\n" if l.startswith("CEREBRAS_API_KEY=") else l for l in lines]
    if not any(l.startswith("CEREBRAS_API_KEY=") for l in lines):updated.append(f"CEREBRAS_API_KEY={k}\n")
    open(ENV,"w").writelines(updated);print("Key updated in ~/.hermes/.env")
if __name__=="__main__":
    print(f"Key rotator: {len(KEYS)} keys");k=select();update(k);print("done")
