#!/usr/bin/env python3
"""
Sends a single Telegram startup notification to the owner.
This is the ONLY place that sends the startup message.
status_agent.py must NOT send a duplicate startup notification.
"""
import os, urllib.request, json

tok  = os.environ.get('TELEGRAM_BOT_TOKEN', '')
rid  = os.environ.get('GITHUB_RUN_ID', '?')
inst = os.environ.get('INSTANCE_NUMBER', '1')

if not tok:
    print('WARN: No TELEGRAM_BOT_TOKEN — skipping notification')
    raise SystemExit(0)

msg = (
    '\U0001f7e2 Zypher online | Instance #' + inst + ' | Run #' + rid[-6:] + '\n'
    'Ready — send me a task.'
)

try:
    data = json.dumps({'chat_id': '6317345496', 'text': msg}).encode()
    req  = urllib.request.Request(
        'https://api.telegram.org/bot' + tok + '/sendMessage',
        data=data, headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=10)
    print('Startup notification sent')
except Exception as e:
    print(f'WARN: Notification failed: {e}')
