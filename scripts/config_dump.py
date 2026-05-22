#!/usr/bin/env python3
  """
  config_dump.py — Print the effective openclaw.json values before gateway starts.
  Used for debug visibility to confirm all critical settings are active.
  """
  import json, os

  CONFIG = os.path.expanduser('~/.openclaw/openclaw.json')

  try:
      cfg = json.load(open(CONFIG))
  except Exception as e:
      print(f'config_dump: ERROR could not read config: {e}')
      raise SystemExit(1)

  tg = cfg.get('channels', {}).get('telegram', {})
  m  = cfg.get('agents', {}).get('defaults', {}).get('model', {})
  providers = list(cfg.get('models', {}).get('providers', {}).keys())
  diag = cfg.get('diagnostics', {})

  print('=== Effective openclaw.json before gateway start ===')
  print(f'  streaming.mode       : {tg.get("streaming", {}).get("mode")}')
  print(f'  allowFrom            : {tg.get("allowFrom")} (types: {[type(x).__name__ for x in tg.get("allowFrom", [])]})')
  print(f'  dmPolicy             : {tg.get("dmPolicy")}')
  print(f'  botToken set         : {bool(tg.get("botToken", ""))}')
  print(f'  primary model        : {m.get("primary")}')
  print(f'  fallbacks count      : {len(m.get("fallbacks", []))}')
  print(f'  providers            : {providers}')
  print(f'  stuckSessionAbortMs  : {diag.get("stuckSessionAbortMs")}')
  print(f'  stuckSessionWarnMs   : {diag.get("stuckSessionWarnMs")}')
  # Check for common problems
  issues = []
  if tg.get('streaming', {}).get('mode') != 'block':
      issues.append('WARN: streaming.mode is not "block" — may cause silent delivery failures')
  for entry in tg.get('allowFrom', []):
      if isinstance(entry, str):
          issues.append(f'WARN: allowFrom contains string "{entry}" — should be integer (messages will be silently dropped)')
  if not tg.get('botToken', ''):
      issues.append('ERROR: botToken is empty — bot will not connect')
  for issue in issues:
      print(f'  ** {issue}')
  if not issues:
      print('  ** All critical values OK')
  