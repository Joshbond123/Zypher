#!/usr/bin/env python3
  """
  post_doctor_fix.py — Restore critical config values after openclaw doctor --fix.

  openclaw doctor --fix can silently reset:
    - channels.telegram.streaming.mode back to "partial" (the known crash bug)
    - channels.telegram.allowFrom from integers back to strings (silent message drop)
    - models.providers.*.timeoutSeconds back to default (120s — too short for qwen3-32b)

  This script is called after every 'openclaw doctor --fix' invocation.
  """
  import json, os, sys

  CONFIG = os.path.expanduser('~/.openclaw/openclaw.json')

  try:
      cfg = json.load(open(CONFIG))
  except Exception as e:
      print(f'post_doctor_fix: WARN could not read config: {e}')
      sys.exit(0)  # non-fatal, let the workflow continue

  tg = cfg.setdefault('channels', {}).setdefault('telegram', {})
  changed = []

  # Fix 1: streaming.mode must be "block"
  # Upstream openclaw bug #66509: streaming.mode=partial causes gateway to buffer
  # tool responses and never deliver them. doctor --fix may reset this to "partial".
  current_mode = tg.get('streaming', {}).get('mode')
  if current_mode != 'block':
      tg.setdefault('streaming', {})['mode'] = 'block'
      changed.append(f'streaming.mode: "{current_mode}" -> "block"')

  # Fix 2: allowFrom must contain integers, not strings
  # grammy (TypeScript) uses strict === comparison. "6317345496" === 6317345496 is False.
  # All incoming messages would be silently dropped if allowFrom contains strings.
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
      changed.append(f'allowFrom: {af} -> {fixed_af} (string -> integer)')

  # Fix 3: timeoutSeconds must be >= 300 on all Cerebras providers
  # qwen3-32b with 16k max tokens needs up to 300s. Default is 120s (too short).
  providers = cfg.get('models', {}).get('providers', {})
  for pname, prov in providers.items():
      if 'cerebras' in pname.lower():
          ts = prov.get('timeoutSeconds', 0)
          if ts < 300:
              prov['timeoutSeconds'] = 300
              changed.append(f'{pname}.timeoutSeconds: {ts} -> 300')

  # Fix 4: stuckSessionAbortMs must be >= 900000 (15 min)
  diag = cfg.get('diagnostics', {})
  if diag.get('stuckSessionAbortMs', 0) < 900000:
      diag['stuckSessionAbortMs'] = 900000
      changed.append('diagnostics.stuckSessionAbortMs -> 900000')
      cfg['diagnostics'] = diag

  if changed:
      json.dump(cfg, open(CONFIG, 'w'), indent=2)
      for c in changed:
          print(f'post_doctor_fix: RESTORED {c}')
      print(f'post_doctor_fix: {len(changed)} value(s) restored OK')
  else:
      print('post_doctor_fix: config intact — no changes needed')
  