#!/usr/bin/env python3
import os, json, urllib.request, urllib.error, sys
key=os.getenv('CEREBRAS_API_KEY','')
if not key:
    print('provider_preflight: SKIP no CEREBRAS_API_KEY')
    sys.exit(0)
url='https://api.cerebras.ai/v1/chat/completions'
payload={"model":"qwen3-32b","messages":[{"role":"user","content":"ping"}],"max_tokens":8}
req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={
    'Authorization':f'Bearer {key}','Content-Type':'application/json'
},method='POST')
try:
    with urllib.request.urlopen(req,timeout=20) as r:
        body=r.read().decode('utf-8','ignore')
        if 'choices' not in body:
            print('provider_preflight: FAIL missing choices in response')
            print(body[:400])
            sys.exit(1)
        print('provider_preflight: OK cerebras chat endpoint healthy')
except urllib.error.HTTPError as e:
    b=e.read().decode('utf-8','ignore') if hasattr(e,'read') else ''
    print(f'provider_preflight: FAIL http {e.code} body={b[:400]}')
    sys.exit(1)
except Exception as e:
    print(f'provider_preflight: FAIL {e}')
    sys.exit(1)
