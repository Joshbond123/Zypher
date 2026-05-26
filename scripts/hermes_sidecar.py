#!/usr/bin/env python3
  """hermes_sidecar.py — Supabase bridge sidecar for Hermes Agent v0.14.0."""
  import os,json,time,logging,hashlib,sys
  from datetime import datetime,timezone
  from urllib.request import urlopen,Request
  from urllib.error import HTTPError
  logging.basicConfig(format="%(asctime)s %(levelname)s [hermes-sidecar] %(message)s",level=logging.INFO)
  log=logging.getLogger("hermes-sidecar")
  SB_URL=os.environ.get("SUPABASE_URL","");SB_KEY=os.environ.get("SUPABASE_SERVICE_KEY","")
  RUN_ID=os.environ.get("GITHUB_RUN_ID","local");LOG_FILE="/tmp/hermes.log"
  MEMORY_DIR=os.path.expanduser("~/.hermes/memories")
  POLL=5;HB=6;MF=60;SBT=8;RT=2
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
      upsert("agent_status",{"id":"zypher","status":s,"framework":"hermes","run_id":RUN_ID,"updated_at":datetime.now(timezone.utc).isoformat()})
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
          if n%HB==0:upsert("active_sessions",{"session_id":f"hermes-{RUN_ID}","framework":"hermes","run_id":RUN_ID,"last_heartbeat":datetime.now(timezone.utc).isoformat()})
          if n%MF==0:flush_memory()
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
                          upsert("task_log",{"run_id":RUN_ID,"role":m["role"],"content":m["content"][:2000],"framework":"hermes","created_at":datetime.now(timezone.utc).isoformat()})
              except Exception as e:log.warning("log read: %s",e)
          time.sleep(POLL)
  if __name__=="__main__":
      if "--flush-memory" in sys.argv:log.info("One-shot flush");flush_memory();set_status("flushing")
      else:main()
  