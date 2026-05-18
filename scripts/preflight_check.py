#!/usr/bin/env python3
"""Pre-flight check printed before starting the gateway."""
import json, sys, os

CONFIG = os.path.expanduser('~/.openclaw/openclaw.json')

try:
    cfg = json.load(open(CONFIG))
except Exception as e:
    print(f'Config error: {e}')
    sys.exit(1)

tg = cfg.get('channels', {}).get('telegram', {})
model_cfg = cfg.get('agents', {}).get('defaults', {}).get('model', {})
primary = model_cfg.get('primary', 'NOT SET')
fallbacks = model_cfg.get('fallbacks', [])
providers = cfg.get('models', {}).get('providers', {})
cerebras_providers = [p for p in providers if 'cerebras' in p.lower()]
streaming = tg.get('streaming', {}).get('mode', 'off')
sb_enabled = cfg.get('plugins', {}).get('entries', {}).get('supabase', {}).get('enabled', False)

print('  telegram=%s botToken_set=%s streaming=%s' % (tg.get('enabled'), bool(tg.get('botToken', '')), streaming))
print('  model.primary=%s fallbacks=%d providers=%s supabase_plugin=%s' % (primary, len(fallbacks), cerebras_providers, sb_enabled))

if not tg.get('botToken', ''):
    print('ERROR: botToken missing — bot will not respond')
    sys.exit(1)
if not fallbacks:
    print('WARN: No fallbacks configured')
