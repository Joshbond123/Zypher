#!/usr/bin/env python3
"""hermes_sidecar.py — Supabase bridge sidecar for Hermes Agent v0.14.0.

FIXES (May 2026):
  - Added state.db backup to Supabase (base64-encoded, key=hermes_state_db)
  - Added --flush-statedb CLI flag for one-shot state.db flush
  - SQLite integrity_check before upload to skip corrupt snapshots
  - Skills directory sync to Supabase (individual skill files as hermes_skill_<name>)
"""
import os,json,time,logging,hashlib,sys,base64,sqlite3,glob
from datetime import datetime,timezone
from urllib.request import urlopen,Request
from urllib.error import HTTPError

logging.basicConfig(format="%(asctime)s %(levelname)s [hermes-sidecar] %(message)s",level=logging.INFO)
log=logging.getLogger("hermes-sidecar")

SB_URL=os.environ.get("SUPABASE_URL","")
SB_KEY=os.environ.get("SUPABASE_SERVICE_KEY","")
RUN_ID=os.environ.get("GITHUB_RUN_ID","local")
LOG_FILE="/tmp/hermes.log"
HERMES_HOME=os.path.expanduser("~/.hermes")
MEMORY_DIR=os.path.join(HERMES_HOME,"memories")
STATE_DB=os.path.join(HERMES_HOME,"state.db")
SKILLS_DIR=os.path.join(HERMES_HOME,"skills")

POLL=5;HB=6;MF=60;SBT=8;RT=2
# Flush state.db every 15 min (180 × 5s) to avoid Supabase row-size pressure
STATEDB_FLUSH_INTERVAL=180

def sb(method,path,body=None,params="",retries=RT):
    if not SB_URL or not SB_KEY:return False,"no-credentials"
    url=f"{SB_URL}/rest/v1/{path}{params}";data=json.dumps(body).encode() if body else None
    h={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}","Content-Type":"application/json","Prefer":"return=minimal,resolution=merge-duplicates"}
    for a in range(retries+1):
        try:
            req=Request(url,data=data,method=method,headers=h)
            with urlopen(req,timeout=SBT) as r:return True,r.read().decode()
        except HTTPError as e:
            if e.code in(404,400):return False,f"http_{e.code}"
            if e.code in(429,503) and a<retries:time.sleep(2**a);continue
            return False,f"http_{e.code}"
        except Exception as e:
            if a<retries:time.sleep(1);continue
            return False,str(e)
    return False,"exhausted"

def upsert(table,rows):
    if not isinstance(rows,list):rows=[rows]
    ok,r=sb("POST",table,rows)
    if not ok:log.debug("upsert %s: %s",table,r[:60])

def set_status(s):
    upsert("agent_status",{"github_run_id":RUN_ID,"status":s,"current_task":"","last_tool":"","tools_called":0,"last_activity":datetime.now(timezone.utc).isoformat(),"updated_at":datetime.now(timezone.utc).isoformat()})

def flush_memory():
    for fn,key in[("MEMORY.md","hermes_memory"),("USER.md","hermes_user")]:
        p=os.path.join(MEMORY_DIR,fn)
        if not os.path.exists(p):continue
        try:
            c=open(p).read()
            if not c.strip():continue
            upsert("longterm_memory",{"key":key,"value":c,"updated_at":datetime.now(timezone.utc).isoformat()})
            log.info("Flushed %s (%d chars)",fn,len(c))
        except Exception as e:log.warning("flush %s: %s",fn,e)

def check_statedb_integrity(path):
    """Returns True if state.db passes SQLite integrity check."""
    try:
        con=sqlite3.connect(path,timeout=5)
        res=con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        return res and res[0]=="ok"
    except Exception as e:
        log.warning("state.db integrity check failed: %s",e)
        return False

def flush_statedb():
    """Backup ~/.hermes/state.db to Supabase as base64-encoded blob."""
    if not os.path.exists(STATE_DB):
        log.debug("state.db not found, skipping flush")
        return
    size=os.path.getsize(STATE_DB)
    if size==0:
        log.debug("state.db is empty, skipping flush")
        return
    if not check_statedb_integrity(STATE_DB):
        log.warning("state.db failed integrity check — skipping Supabase backup to avoid storing corrupt snapshot")
        return
    try:
        with open(STATE_DB,"rb") as f:raw=f.read()
        encoded=base64.b64encode(raw).decode("ascii")
        sha=hashlib.sha256(raw).hexdigest()[:16]
        upsert("longterm_memory",{
            "key":"hermes_state_db",
            "value":encoded,
            "updated_at":datetime.now(timezone.utc).isoformat()
        })
        log.info("Flushed state.db to Supabase (%d bytes, sha256=%s)",size,sha)
    except Exception as e:
        log.warning("flush state.db: %s",e)

def flush_skills():
    """Backup each skill file in ~/.hermes/skills/ to Supabase."""
    if not os.path.isdir(SKILLS_DIR):return
    for fpath in glob.glob(os.path.join(SKILLS_DIR,"*.md"))+glob.glob(os.path.join(SKILLS_DIR,"*.py"))+glob.glob(os.path.join(SKILLS_DIR,"*.json")):
        fname=os.path.basename(fpath)
        key=f"hermes_skill_{fname}"
        try:
            c=open(fpath).read()
            if not c.strip():continue
            upsert("longterm_memory",{"key":key,"value":c,"updated_at":datetime.now(timezone.utc).isoformat()})
            log.debug("Flushed skill %s",fname)
        except Exception as e:
            log.warning("flush skill %s: %s",fname,e)

def classify(line):
    line=line.strip()
    if not line:return None
    if line.startswith("{"):
        try:
            d=json.loads(line)
            return{"role":d.get("role","agent"),"content":d.get("content",d.get("text",line[:500])),"type":d.get("type","msg"),"raw":line[:500]}
        except:pass
    for kw,t in[("telegram","telegram"),("response","response"),("reply","response"),("bash","tool"),("browser","tool"),("search","tool"),("task","task")]:
        if kw in line.lower():return{"role":"agent","content":line[:500],"type":t,"raw":line[:500]}
    return None

def main():
    log.info("Hermes sidecar | run=%s",RUN_ID);set_status("starting")
    pos=0;n=0;seen=set()
    for _ in range(30):
        if os.path.exists(LOG_FILE):break
        time.sleep(2)
    set_status("running");log.info("Monitoring %s",LOG_FILE)
    while True:
        n+=1
        if n%HB==0:
            upsert("active_sessions",{
                "github_run_id":RUN_ID,
                "is_active":True,
                "instance_num":1,
                "updated_at":datetime.now(timezone.utc).isoformat()
            })
        if n%MF==0:
            flush_memory()
            flush_skills()
        if n%STATEDB_FLUSH_INTERVAL==0:
            flush_statedb()
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE) as f:f.seek(pos);lines=f.readlines();pos=f.tell()
                for line in lines:
                    m=classify(line)
                    if not m:continue
                    h=hashlib.md5(m["raw"].encode()).hexdigest()
                    if h in seen:continue
                    seen.add(h)
                    if len(seen)>2000:seen=set(list(seen)[-1000:])
                    if m["type"] in("telegram","response","tool","task"):
                        upsert("task_log",{
                            "task_id":h[:16],
                            "session_id":f"hermes-{RUN_ID}",
                            "status":m["type"],
                            "message":m["content"][:2000],
                            "tool_name":m["type"],
                            "created_at":datetime.now(timezone.utc).isoformat()
                        })
            except Exception as e:log.warning("log read: %s",e)
        time.sleep(POLL)

if __name__=="__main__":
    if "--flush-memory" in sys.argv:
        log.info("One-shot memory flush")
        flush_memory()
        flush_skills()
        flush_statedb()
        set_status("flushing")
    elif "--flush-statedb" in sys.argv:
        log.info("One-shot state.db flush")
        flush_statedb()
    else:
        main()
