#!/usr/bin/env python3
"""Validates ~/.openclaw/openclaw.json after template substitution."""
import json, sys, os, re

CONFIG = os.path.expanduser('~/.openclaw/openclaw.json')

try:
    c = json.load(open(CONFIG))
except Exception as e:
    print(f'ERROR: Could not parse config: {e}')
    sys.exit(1)

tg = c.get('channels', {}).get('telegram', {})
assert tg.get('enabled') == True, 'telegram.enabled is not true'
tok = tg.get('botToken', '')
assert tok and tok != 'TELEGRAM_TOKEN_HERE', 'botToken not injected'
assert not re.search(r'CEREBRAS_KEY_[0-9]_HERE', json.dumps(c)), 'Cerebras placeholders not substituted'

model_cfg = c.get('agents', {}).get('defaults', {}).get('model', {})
primary = model_cfg.get('primary', '')
fallbacks = model_cfg.get('fallbacks', [])
assert primary, 'model.primary not configured'
assert len(fallbacks) > 0, 'model.fallbacks is empty — requests will fail if primary unavailable'

providers = c.get('models', {}).get('providers', {})
num_providers = len([p for p in providers if 'cerebras' in p.lower()])
streaming = tg.get('streaming', {}).get('mode', 'off')
sb_enabled = c.get('plugins', {}).get('entries', {}).get('supabase', {}).get('enabled', False)

print('Config OK: primary=%s fallbacks=%d providers=%d streaming=%s supabase_plugin=%s' % (
    primary, len(fallbacks), num_providers, streaming, sb_enabled))
