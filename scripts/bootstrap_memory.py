#!/usr/bin/env python3
"""hermes_bootstrap_memory.py — Restore Hermes memory and state on startup.

FIXES (May 2026):
  - Added state.db restore from Supabase (base64-decoded, integrity-checked)
  - Added corruption detection: if restored state.db fails integrity_check,
    it is renamed to state.db.corrupt and a fresh one is created
  - Added skills restore from Supabase (hermes_skill_* keys)
  - Artifact-first: if /tmp/memory-restore/state.db exists (from artifact),
    it is always preferred over the Supabase copy
"""
import os,json,datetime,base64,sqlite3,shutil,glob
from urllib.request import urlopen,Request

SB_URL=os.environ.get("SUPABASE_URL","")
SB_KEY=os.environ.get("SUPABASE_SERVICE_KEY","")
RUN_ID=os.environ.get("GITHUB_RUN_ID","local")
HERMES_HOME=os.path.expanduser("~/.hermes")
MEM=os.path.join(HERMES_HOME,"memories")
STATE_DB=os.path.join(HERMES_HOME,"state.db")
SKILLS_DIR=os.path.join(HERMES_HOME,"skills")
ARTIFACT_DIR="/tmp/memory-restore"
NOW=datetime.datetime.now(datetime.timezone.utc).isoformat()

def sb_get(path):
    if not SB_URL or not SB_KEY:return None
    try:
        req=Request(f"{SB_URL}/rest/v1/{path}",headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}"})
        with urlopen(req,timeout=10) as r:return json.loads(r.read())
    except Exception as e:print(f"WARN Supabase: {e}");return None

def sb_get_value(key):
    data=sb_get(f"longterm_memory?key=eq.{key}&select=value")
    if data and isinstance(data,list) and data and data[0].get("value"):
        return data[0]["value"]
    return None

def check_integrity(path):
    """Returns True if path is a valid, non-corrupt SQLite database."""
    try:
        con=sqlite3.connect(path,timeout=5)
        res=con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        return res and res[0]=="ok"
    except Exception as e:
        print(f"WARN integrity check {path}: {e}")
        return False

def restore_text(fname,key,default):
    """Restore a text memory file (MEMORY.md, USER.md)."""
    path=os.path.join(MEM,fname)
    # 1. Artifact takes priority
    artifact_path=os.path.join(ARTIFACT_DIR,fname)
    if os.path.exists(artifact_path) and os.path.getsize(artifact_path)>50:
        shutil.copy2(artifact_path,path)
        print(f"{fname}: restored from artifact ({os.path.getsize(path)}b)")
        return
    # 2. Already present on disk (from a previous step)
    if os.path.exists(path) and os.path.getsize(path)>50:
        print(f"{fname}: present ({os.path.getsize(path)}b)");return
    # 3. Supabase fallback
    val=sb_get_value(key)
    if val:open(path,"w").write(val);print(f"{fname}: restored from Supabase ({len(val)}c)")
    else:open(path,"w").write(default);print(f"{fname}: fresh init")

def restore_statedb():
    """Restore ~/.hermes/state.db from artifact or Supabase."""
    # 1. Artifact takes priority
    artifact_db=os.path.join(ARTIFACT_DIR,"state.db")
    if os.path.exists(artifact_db) and os.path.getsize(artifact_db)>0:
        if check_integrity(artifact_db):
            shutil.copy2(artifact_db,STATE_DB)
            print(f"state.db: restored from artifact ({os.path.getsize(STATE_DB):,} bytes) — integrity OK")
            return
        else:
            print(f"WARN state.db: artifact copy failed integrity check — trying Supabase")

    # 2. If already present and valid, keep it
    if os.path.exists(STATE_DB) and os.path.getsize(STATE_DB)>0:
        if check_integrity(STATE_DB):
            print(f"state.db: already present on disk ({os.path.getsize(STATE_DB):,} bytes) — integrity OK")
            return
        else:
            corrupt_path=STATE_DB+".corrupt"
            shutil.move(STATE_DB,corrupt_path)
            print(f"WARN state.db: existing copy corrupt — moved to {corrupt_path}")

    # 3. Supabase fallback
    encoded=sb_get_value("hermes_state_db")
    if encoded:
        try:
            raw=base64.b64decode(encoded)
            tmp_path=STATE_DB+".tmp"
            with open(tmp_path,"wb") as f:f.write(raw)
            if check_integrity(tmp_path):
                shutil.move(tmp_path,STATE_DB)
                print(f"state.db: restored from Supabase ({len(raw):,} bytes) — integrity OK")
            else:
                os.remove(tmp_path)
                print(f"WARN state.db: Supabase copy failed integrity check — starting fresh")
        except Exception as e:
            print(f"WARN state.db restore from Supabase failed: {e}")
    else:
        print("state.db: not found in artifact or Supabase — Hermes will create fresh")

def restore_skills():
    """Restore skill files from Supabase (hermes_skill_* keys)."""
    if not SB_URL or not SB_KEY:return
    try:
        data=sb_get("longterm_memory?key=like.hermes_skill_*&select=key,value")
        if not data:print("skills: none in Supabase");return
        restored=0
        for row in data:
            key=row.get("key","");val=row.get("value","")
            if not key.startswith("hermes_skill_") or not val:continue
            fname=key[len("hermes_skill_"):]
            path=os.path.join(SKILLS_DIR,fname)
            if os.path.exists(path):continue  # don't overwrite existing skills
            with open(path,"w") as f:f.write(val)
            restored+=1
        if restored:print(f"skills: restored {restored} skill file(s) from Supabase")
        else:print("skills: all present or none in Supabase")
    except Exception as e:
        print(f"WARN skills restore: {e}")

if __name__=="__main__":
    os.makedirs(MEM,exist_ok=True)
    os.makedirs(SKILLS_DIR,exist_ok=True)

    restore_text("MEMORY.md","hermes_memory",
        f"# Zypher Memory\nInit: {NOW}\nFramework: Hermes v0.14.0\nRun: {RUN_ID}\n\n## Lessons\n*(empty)*\n\n## Tasks\n*(none)*\n")
    restore_text("USER.md","hermes_user",
        "# User: Joshbond\nTelegram: 6317345496\nBot: @Zypher0_bot\n\n## Preferences\n*(to be learned)*\n")
    restore_statedb()
    restore_skills()
    print("Memory bootstrap complete")
