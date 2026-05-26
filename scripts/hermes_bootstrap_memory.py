#!/usr/bin/env python3
"""hermes_bootstrap_memory.py — Restore Hermes memory on startup."""
import os,json,datetime
from urllib.request import urlopen,Request
SB_URL=os.environ.get("SUPABASE_URL","");SB_KEY=os.environ.get("SUPABASE_SERVICE_KEY","")
RUN_ID=os.environ.get("GITHUB_RUN_ID","local");MEM=os.path.expanduser("~/.hermes/memories")
NOW=datetime.datetime.now(datetime.timezone.utc).isoformat()
def sb_get(path):
    if not SB_URL or not SB_KEY:return None
    try:
        req=Request(f"{SB_URL}/rest/v1/{path}",headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}"})
        with urlopen(req,timeout=10) as r:return json.loads(r.read())
    except Exception as e:print(f"WARN Supabase: {e}");return None
def restore(fname,key,default):
    path=os.path.join(MEM,fname)
    if os.path.exists(path) and os.path.getsize(path)>50:print(f"{fname}: present ({os.path.getsize(path)}b)");return
    data=sb_get(f"longterm_memory?key=eq.{key}&select=value")
    if data and data[0].get("value"):open(path,"w").write(data[0]["value"]);print(f"{fname}: restored from Supabase ({len(data[0]['value'])}c)")
    else:open(path,"w").write(default);print(f"{fname}: fresh init")
if __name__=="__main__":
    os.makedirs(MEM,exist_ok=True)
    restore("MEMORY.md","hermes_memory",f"# Zypher Memory\nInit: {NOW}\nFramework: Hermes v0.14.0\nRun: {RUN_ID}\n\n## Lessons\n*(empty)*\n\n## Tasks\n*(none)*\n")
    restore("USER.md","hermes_user","# User: Joshbond\nTelegram: 6317345496\nBot: @Zypher0_bot\n\n## Preferences\n*(to be learned)*\n")
    print("Memory bootstrap complete")
