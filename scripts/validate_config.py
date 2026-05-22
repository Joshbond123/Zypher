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
  dm_policy = tg.get('dmPolicy', '')
  allow_from = tg.get('allowFrom', [])
  assert dm_policy == 'allowlist', f'telegram.dmPolicy must be "allowlist", got "{dm_policy}"'
  assert len(allow_from) > 0, 'telegram.allowFrom must list at least one chat ID'

  # allowFrom must be integers — grammy uses strict === (6317345496 !== "6317345496")
  for entry in allow_from:
      assert isinstance(entry, int), (
          f'telegram.allowFrom must contain integers (not strings). '
          f'Got {type(entry).__name__}: {entry!r}. '
          f'Fix: remove quotes around the chat ID in oc_template.json'
      )

  model_cfg = c.get('agents', {}).get('defaults', {}).get('model', {})
  primary = model_cfg.get('primary', '')
  fallbacks = model_cfg.get('fallbacks', [])
  assert primary, 'model.primary not configured'
  assert len(fallbacks) > 0, 'model.fallbacks is empty'

  providers = c.get('models', {}).get('providers', {})
  num_providers = len([p for p in providers if 'cerebras' in p.lower()])
  streaming_cfg = tg.get('streaming', {})
  streaming = streaming_cfg.get('mode', 'off') if isinstance(streaming_cfg, dict) else 'off'
  assert streaming in ('off', 'partial', 'block', 'progress'), f'streaming.mode invalid: "{streaming}"'
  assert streaming == 'block', (
      f'streaming.mode must be "block". Got "{streaming}". '
      f'openclaw issue #66509: partial mode buffers tool responses and never delivers them.'
  )

  for pname, prov in providers.items():
      if 'cerebras' in pname:
          ts = prov.get('timeoutSeconds', 0)
          assert ts >= 300, f'{pname}.timeoutSeconds={ts} is too low (need >= 300)'

  sb_enabled = c.get('plugins', {}).get('entries', {}).get('supabase', {}).get('enabled', False)
  print('Config OK: primary=%s fallbacks=%d providers=%d streaming=%s allowFrom=%s' % (
      primary, len(fallbacks), num_providers, streaming, allow_from))
  