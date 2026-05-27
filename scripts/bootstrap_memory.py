#!/usr/bin/env python3
"""hermes_bootstrap_memory.py — Restore Hermes memory and state on startup.

FIXES (May 2026):
  - state.db restore now handles WAL mode correctly:
    After restoring, runs PRAGMA wal_checkpoint(FULL) to flush WAL → main db
    and PRAGMA journal_mode=WAL to ensure WAL mode is active on the fresh copy.
  - Uses sqlite3.backup() API for the clean copy operation
  - Artifact-first strategy: /tmp/memory-restore/{file} always wins over Supabase
  - Corruption detection: corrupt files renamed to .corrupt, fresh db started
  - Skills restore from Supabase (hermes_skill_* keys)
  - Handles state.db-wal and state.db-shm cleanup after restore
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
    """Returns True if path is a valid non-corrupt SQLite database."""
    try:
        con=sqlite3.connect(f"file:{path}?mode=ro",uri=True,timeout=5)
        res=con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        return res and res[0]=="ok"
    except Exception as e:
        print(f"WARN integrity check {path}: {e}")
        return False

def clean_copy_statedb(src,dst):
    """
    Copy a WAL-mode SQLite database safely using sqlite3.backup() API.
    Creates a clean, self-contained snapshot with no -wal dependency.
    """
    try:
        source=sqlite3.connect(f"file:{src}?mode=ro",uri=True,timeout=30)
        dest=sqlite3.connect(dst)
        with dest:
            source.backup(dest,pages=0)
        source.close()
        dest.close()
        return True
    except Exception as e:
        print(f"WARN sqlite3.backup() failed: {e}")
        return False

def checkpoint_and_activate_wal(path):
    """
    After restoring a WAL-mode database:
    1. Checkpoint to flush any remaining WAL transactions into main db
    2. Re-enable WAL mode (in case the restored copy is in DELETE mode)
    """
    try:
        con=sqlite3.connect(path,timeout=10)
        # Enable WAL mode (idempotent if already WAL)
        con.execute("PRAGMA journal_mode=WAL")
        # Checkpoint: flush WAL file into main db, then truncate WAL
        con.execute("PRAGMA wal_checkpoint(FULL)")
        con.close()
        print(f"  WAL checkpoint complete on {os.path.basename(path)}")
    except Exception as e:
        print(f"WARN WAL checkpoint {path}: {e}")

def remove_wal_files(db_path):
    """Remove stale -wal and -shm files that could corrupt a restored db."""
    for ext in ["-wal","-shm"]:
        p=db_path+ext
        if os.path.exists(p):
            os.remove(p)
            print(f"  Removed stale {os.path.basename(p)}")

def restore_text(fname,key,default):
    """Restore a text memory file (MEMORY.md, USER.md)."""
    path=os.path.join(MEM,fname)
    # 1. Artifact takes priority
    artifact_path=os.path.join(ARTIFACT_DIR,fname)
    if os.path.exists(artifact_path) and os.path.getsize(artifact_path)>50:
        shutil.copy2(artifact_path,path)
        print(f"{fname}: restored from artifact ({os.path.getsize(path)}b)")
        return
    # 2. Already present on disk
    if os.path.exists(path) and os.path.getsize(path)>50:
        print(f"{fname}: already on disk ({os.path.getsize(path)}b)");return
    # 3. Supabase fallback
    val=sb_get_value(key)
    if val:open(path,"w").write(val);print(f"{fname}: restored from Supabase ({len(val)}c)")
    else:open(path,"w").write(default);print(f"{fname}: fresh init")

def restore_statedb():
    """
    Restore ~/.hermes/state.db with full WAL-mode safety.

    Priority: artifact > Supabase > fresh start
    After restore: checkpoint, activate WAL, remove stale WAL files.
    """
    print("state.db: starting restore...")

    # 1. Artifact takes priority
    artifact_db=os.path.join(ARTIFACT_DIR,"state.db")
    if os.path.exists(artifact_db) and os.path.getsize(artifact_db)>0:
        # Use clean_copy to avoid -wal dependency issues
        tmp=STATE_DB+".restore_tmp"
        if clean_copy_statedb(artifact_db,tmp):
            if check_integrity(tmp):
                # Remove any stale WAL files before placing restored copy
                remove_wal_files(STATE_DB)
                shutil.move(tmp,STATE_DB)
                checkpoint_and_activate_wal(STATE_DB)
                print(f"state.db: restored from artifact ({os.path.getsize(STATE_DB):,}b) — integrity OK")
                return
            else:
                print(f"WARN state.db: artifact copy failed integrity check — trying Supabase")
                try:os.remove(tmp)
                except:pass
        else:
            # Fallback: try direct copy of artifact
            remove_wal_files(STATE_DB)
            shutil.copy2(artifact_db,STATE_DB)
            if check_integrity(STATE_DB):
                checkpoint_and_activate_wal(STATE_DB)
                print(f"state.db: restored from artifact (direct copy, {os.path.getsize(STATE_DB):,}b)")
                return
            else:
                print(f"WARN state.db: artifact direct copy corrupt — trying Supabase")

    # 2. Already present and valid
    if os.path.exists(STATE_DB) and os.path.getsize(STATE_DB)>0:
        if check_integrity(STATE_DB):
            checkpoint_and_activate_wal(STATE_DB)
            print(f"state.db: already on disk ({os.path.getsize(STATE_DB):,}b) — integrity OK")
            return
        else:
            corrupt_path=STATE_DB+".corrupt"
            remove_wal_files(STATE_DB)
            shutil.move(STATE_DB,corrupt_path)
            print(f"WARN state.db: on-disk copy corrupt → moved to state.db.corrupt")

    # 3. Supabase fallback
    encoded=sb_get_value("hermes_state_db")
    if encoded:
        try:
            raw=base64.b64decode(encoded)
            tmp=STATE_DB+".supabase_tmp"
            with open(tmp,"wb") as f:f.write(raw)
            if check_integrity(tmp):
                remove_wal_files(STATE_DB)
                shutil.move(tmp,STATE_DB)
                checkpoint_and_activate_wal(STATE_DB)
                print(f"state.db: restored from Supabase ({len(raw):,}b) — integrity OK")
            else:
                os.remove(tmp)
                print(f"WARN state.db: Supabase copy failed integrity check — starting fresh")
        except Exception as e:
            print(f"WARN state.db restore from Supabase: {e}")
    else:
        print("state.db: not in artifact or Supabase — Hermes will init fresh on first run")

def restore_skills():
    """Restore skill files from Supabase (hermes_skill_* keys, non-destructive)."""
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
            if os.path.exists(path):continue  # never overwrite existing skills
            with open(path,"w") as f:f.write(val)
            restored+=1
        if restored:print(f"skills: restored {restored} skill(s) from Supabase")
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
