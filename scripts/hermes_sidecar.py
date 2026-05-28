#!/usr/bin/env python3
  """hermes_sidecar.py — Supabase bridge sidecar for Hermes Agent v0.14.0.

  FIXES (May 2026):
    - state.db backup now uses Python sqlite3.backup() API (WAL-safe online backup)
      Previously used direct file copy which is UNSAFE for WAL-mode databases:
      the WAL file may contain uncommitted transactions not yet flushed to the main db.
      sqlite3.backup() uses the SQLite Online Backup API — works safely even while
      Hermes gateway has the database open (no exclusive lock required).
    - Added state.db backup to Supabase (base64-encoded, key=hermes_state_db)
    - Added skills directory sync to Supabase
    - Corruption detection before upload
    - INCREASED state.db flush frequency: every 5 min (was 15 min)
      Reduces maximum data loss window from 15 min to 5 min on unexpected crash.
    - Added SOUL.md sync to Supabase for cross-run integrity verification
  """
  import os,json,time,logging,hashlib,sys,base64,sqlite3,glob,shutil
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
  SOUL_MD=os.path.join(HERMES_HOME,"SOUL.md")

  POLL=5           # seconds between main loop iterations
  HB=6             # heartbeat every N iterations (30s)
  MF=60            # memory flush every N iterations (5 min)
  # State.db flush every 60 iterations (5 min). Previously 180 (15 min).
  # Reduced to 5 min to cut maximum data loss window from 15 min to 5 min.
  STATEDB_FLUSH_INTERVAL=60
  SBT=8            # Supabase request timeout
  RT=2             # Supabase retry count

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
      """Flush MEMORY.md and USER.md to Supabase."""
      for fn,key in[("MEMORY.md","hermes_memory"),("USER.md","hermes_user")]:
          p=os.path.join(MEMORY_DIR,fn)
          if not os.path.exists(p):continue
          try:
              c=open(p).read()
              if not c.strip():continue
              upsert("longterm_memory",{"key":key,"value":c,"updated_at":datetime.now(timezone.utc).isoformat()})
              log.info("Flushed %s (%d chars)",fn,len(c))
          except Exception as e:log.warning("flush %s: %s",fn,e)

  def flush_soul_md():
      """Flush SOUL.md to Supabase for cross-run integrity verification."""
      if not os.path.exists(SOUL_MD):return
      try:
          c=open(SOUL_MD).read()
          if not c.strip():return
          upsert("longterm_memory",{"key":"hermes_soul_md","value":c,"updated_at":datetime.now(timezone.utc).isoformat()})
          log.debug("Flushed SOUL.md (%d chars)",len(c))
      except Exception as e:log.warning("flush SOUL.md: %s",e)

  def backup_statedb_wal_safe(source_path,dest_path):
      """
      WAL-safe SQLite backup using Python's sqlite3.backup() API.

      WHY THIS MATTERS (root cause of state.db loss):
        state.db operates in WAL mode. Direct file copy (shutil.copy) is UNSAFE:
        - The WAL file (-wal) may contain transactions not yet in the main db file
        - Copying only the .db without the .wal produces an incomplete snapshot
        - If Hermes is writing, a raw copy may capture the db mid-transaction

      The sqlite3.backup() API uses SQLite's Online Backup API (C level):
        - Works safely while another process has the database open
        - Correctly handles WAL mode — reads a consistent snapshot
        - Does not require exclusive locks
        - Returns a clean, self-contained database file (no -wal dependency)

      This is the ONLY safe method for online backup of a WAL-mode SQLite DB.
      """
      try:
          src=sqlite3.connect(f"file:{source_path}?mode=ro",uri=True,timeout=30)
          dst=sqlite3.connect(dest_path)
          with dst:
              src.backup(dst,pages=0)  # pages=0 = copy entire DB in one step
          src.close()
          dst.close()
          return True
      except Exception as e:
          log.warning("WAL-safe backup failed: %s",e)
          return False

  def check_statedb_integrity(path):
      """Returns True if path is a valid non-corrupt SQLite database."""
      try:
          con=sqlite3.connect(f"file:{path}?mode=ro",uri=True,timeout=5)
          res=con.execute("PRAGMA integrity_check").fetchone()
          con.close()
          return res and res[0]=="ok"
      except Exception as e:
          log.warning("integrity check %s: %s",path,e)
          return False

  def flush_statedb():
      """
      Backup ~/.hermes/state.db to Supabase using WAL-safe sqlite3.backup() API.
      Runs every STATEDB_FLUSH_INTERVAL iterations (5 min) to minimize data loss.
      """
      if not os.path.exists(STATE_DB):
          log.debug("state.db not found — skipping")
          return
      if os.path.getsize(STATE_DB)==0:
          log.debug("state.db is empty — skipping")
          return

      tmp_backup="/tmp/state_backup.db"
      try:
          if os.path.exists(tmp_backup):os.remove(tmp_backup)
      except:pass

      if not backup_statedb_wal_safe(STATE_DB,tmp_backup):
          log.warning("state.db WAL-safe backup to /tmp failed — skipping Supabase upload")
          return

      if not check_statedb_integrity(tmp_backup):
          log.warning("state.db backup copy failed integrity check — skipping upload")
          try:os.remove(tmp_backup)
          except:pass
          return

      try:
          with open(tmp_backup,"rb") as f:raw=f.read()
          encoded=base64.b64encode(raw).decode("ascii")
          sha=hashlib.sha256(raw).hexdigest()[:16]
          size=len(raw)
          upsert("longterm_memory",{
              "key":"hermes_state_db",
              "value":encoded,
              "updated_at":datetime.now(timezone.utc).isoformat()
          })
          log.info("Flushed state.db -> Supabase (%d bytes, sha256=%s, WAL-safe)",size,sha)
      except Exception as e:
          log.warning("flush state.db Supabase upload: %s",e)
      finally:
          try:os.remove(tmp_backup)
          except:pass

  def flush_skills():
      """Backup skill files in ~/.hermes/skills/ to Supabase."""
      if not os.path.isdir(SKILLS_DIR):return
      count=0
      for fpath in (glob.glob(os.path.join(SKILLS_DIR,"*.md"))+glob.glob(os.path.join(SKILLS_DIR,"*.py"))+
                    glob.glob(os.path.join(SKILLS_DIR,"*.json"))+glob.glob(os.path.join(SKILLS_DIR,"*.yaml"))):
          fname=os.path.basename(fpath)
          key=f"hermes_skill_{fname}"
          try:
              c=open(fpath).read()
              if not c.strip():continue
              upsert("longterm_memory",{"key":key,"value":c,"updated_at":datetime.now(timezone.utc).isoformat()})
              count+=1
          except Exception as e:
              log.warning("flush skill %s: %s",fname,e)
      if count:log.info("Flushed %d skill file(s) to Supabase",count)

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
      set_status("running");log.info("Monitoring %s | state.db flush every %ds",LOG_FILE,STATEDB_FLUSH_INTERVAL*POLL)
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
              flush_soul_md()
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
          log.info("One-shot full flush (memory + skills + state.db + SOUL.md)")
          flush_memory()
          flush_soul_md()
          flush_skills()
          flush_statedb()
          set_status("flushing")
      elif "--flush-statedb" in sys.argv:
          log.info("One-shot state.db flush")
          flush_statedb()
      else:
          main()
  