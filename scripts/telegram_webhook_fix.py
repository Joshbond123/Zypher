#!/usr/bin/env python3
"""
telegram_webhook_fix.py
=======================
Checks for and deletes any active Telegram webhook before the gateway starts.

WHY THIS MATTERS:
  When a Telegram webhook is active, Telegram delivers ALL incoming messages to
  the webhook URL via HTTP POST and delivers NOTHING to the long-polling gateway.
  The bot appears online but silently ignores every message. This is the most
  common cause of bots not responding despite the gateway running correctly.
"""
import os, sys, json
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TOKEN:
    print('telegram_webhook_fix: SKIP -- TELEGRAM_BOT_TOKEN not set')
    sys.exit(0)

BASE_URL = 'https://api.telegram.org/bot' + TOKEN

def tg_get(method, qs=""):
    try:
        url = BASE_URL + '/' + method + qs
        with urlopen(Request(url), timeout=10) as r:
            return json.loads(r.read())
    except (HTTPError, URLError) as e:
        return {'ok': False, 'error': str(e)}

def tg_post(method, qs=""):
    try:
        url = BASE_URL + '/' + method + qs
        with urlopen(Request(url, data=b""), timeout=10) as r:
            return json.loads(r.read())
    except (HTTPError, URLError) as e:
        return {'ok': False, 'error': str(e)}

print("=== Telegram webhook check ===")

info = tg_get("getWebhookInfo")
if not info.get("ok"):
    print("  WARN: getWebhookInfo failed:", info)
else:
    result = info.get("result", {})
    wh_url  = result.get("url", "")
    pending = result.get("pending_update_count", 0)
    last_err = result.get("last_error_message", "")
    print("  webhook_url    :", repr(wh_url))
    print("  pending_updates:", pending)
    if last_err:
        print("  last_error     :", last_err)
    if wh_url:
        print("  CRITICAL: Active webhook -- all messages go to webhook, NOT to polling gateway!")
    else:
        print("  No active webhook -- polling mode OK")

print("  Calling deleteWebhook?drop_pending_updates=true ...")
del_result = tg_post("deleteWebhook", "?drop_pending_updates=true")
if del_result.get("ok"):
    print("  deleteWebhook  : OK (polling mode confirmed)")
else:
    print("  WARN: deleteWebhook returned:", del_result)

print("=== Webhook fix complete ===")