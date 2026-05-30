#!/usr/bin/env python3
"""hermes_startup_notify.py — Send Telegram startup notification."""
import os,json
from urllib.request import urlopen,Request
BOT=os.environ.get("TELEGRAM_BOT_TOKEN","");UID=os.environ.get("TELEGRAM_USER_ID","6317345496");RUN=os.environ.get("GITHUB_RUN_ID","?")
def send(text):
    if not BOT or not UID:print("WARN: creds missing");return
    payload=json.dumps({"chat_id":UID,"text":text,"parse_mode":"Markdown"}).encode()
    req=Request(f"https://api.telegram.org/bot{BOT}/sendMessage",data=payload,headers={"Content-Type":"application/json"})
    try:
        with urlopen(req,timeout=15) as r:d=json.loads(r.read());print("Sent" if d.get("ok") else f"WARN: {d}")
    except Exception as e:print(f"WARN: {e}")
if __name__=="__main__":
    send(f"*Zypher is online* (Run #{RUN})\n\nFramework: Hermes Agent\nProvider: local llama.cpp / Qwen3.5-9B Q4_K_M\nTools: bash, web_search, browser\n\nReady. Send me anything.")
