#!/usr/bin/env python3
  """Print effective openclaw.json values before gateway starts (debug visibility)."""
  import json, os

  CONFIG = os.path.expanduser('~/.openclaw/openclaw.json')

  try:
      cfg = json.load(open(CONFIG))
  except Exception as e:
      print(f'config_dump: ERROR {e}')
      raise SystemExit(1)

  tg = cfg.get('channels', {}).get('telegram', {})
  m  = cfg.get('agents', {}).get('defaults', {}).get('model', {})
  providers = list(cfg.get('models', {}).get('providers', {}).keys())
  diag = cfg.get('diagnostics', {})

  print('=== Effective config before gateway start ===')
  print(f'  streaming.mode      : {tg.get("streaming", {}).get("mode")}')
  print(f'  allowFrom           : {tg.get("allowFrom")} types={[type(x).__name__ for x in tg.get("allowFrom", [])]}')
  print(f'  dmPolicy            : {tg.get("dmPolicy")}')
  print(f'  botToken set        : {bool(tg.get("botToken", ""))}')
  print(f'  primary model       : {m.get("primary")}')
  print(f'  fallbacks count     : {len(m.get("fallbacks", []))}')
  print(f'  providers           : {providers}')
  print(f'  stuckSessionAbortMs : {diag.get("stuckSessionAbortMs")}')

  issues = []
  if tg.get('streaming', {}).get('mode') != 'block':
      issues.append('WARN: streaming.mode is not block')
  for e in tg.get('allowFrom', []):
      if isinstance(e, str):
          issues.append(f'WARN: allowFrom has string "{e}" — should be integer')
  if not tg.get('botToken', ''):
      issues.append('ERROR: botToken empty')
  for issue in issues:
      print(f'  ** {issue}')
  if not issues:
      print('  ** All critical values OK')
  