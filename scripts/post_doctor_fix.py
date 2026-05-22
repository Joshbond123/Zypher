#!/usr/bin/env python3
"""
Restore critical config values after openclaw doctor --fix.
doctor --fix can silently reset streaming.mode, allowFrom types, and timeoutSeconds.
Call this script after every doctor invocation.
"""
import json, os, sys

CONFIG = os.path.expanduser('~/.openclaw/openclaw.json')

try:
    cfg = json.load(open(CONFIG))
except Exception as e:
    print(f'post_doctor_fix: WARN could not read config: {e}')
    sys.exit(0)

tg = cfg.setdefault('channels', {}).setdefault('telegram', {})
changed = []

# Fix streaming.mode — openclaw issue #66509: partial mode causes silent delivery failure
current_mode = tg.get('streaming', {}).get('mode')
if current_mode != 'block':
    tg.setdefault('streaming', {})['mode'] = 'block'
    changed.append(f'streaming.mode: "{current_mode}" -> "block"')

# Fix allowFrom types — grammy strict === drops messages if strings not integers
af = tg.get('allowFrom', [])
fixed_af = []
needs_fix = False
for entry in af:
    if isinstance(entry, str):
        fixed_af.append(int(entry))
        needs_fix = True
    else:
        fixed_af.append(entry)
if needs_fix:
    tg['allowFrom'] = fixed_af
    changed.append(f'allowFrom: {af} -> {fixed_af}')

# Fix timeoutSeconds — qwen3-32b needs 300s, default is 120s
for pname, prov in cfg.get('models', {}).get('providers', {}).items():
    if 'cerebras' in pname.lower() and prov.get('timeoutSeconds', 0) < 300:
        prov['timeoutSeconds'] = 300
        changed.append(f'{pname}.timeoutSeconds -> 300')

# Fix stuckSessionAbortMs — 15min minimum for long pentest tasks
diag = cfg.setdefault('diagnostics', {})
if diag.get('stuckSessionAbortMs', 0) < 900000:
    diag['stuckSessionAbortMs'] = 900000
    changed.append('stuckSessionAbortMs -> 900000')

if changed:
    json.dump(cfg, open(CONFIG, 'w'), indent=2)
    for c in changed:
        print(f'post_doctor_fix: RESTORED {c}')
    print(f'post_doctor_fix: {len(changed)} value(s) restored')
else:
    print('post_doctor_fix: config intact')
