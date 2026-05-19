#!/usr/bin/env python3
  """Zypher Supabase Setup — verify schema health and register session on startup."""
  import os, sys, json, time
  from urllib.request import urlopen, Request
  from urllib.error import HTTPError

  SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
  SUPABASE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")
  GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
  REQUIRED      = ["chat_messages","longterm_memory","active_sessions",
                   "agent_status","task_log","memory_entries"]

  def sb(method, path, body=None, params=""):
      if not SUPABASE_URL or not SUPABASE_KEY: return None, "no-credentials"
      url = f"{SUPABASE_URL}/rest/v1/{path}{params}"
      data = json.dumps(body).encode() if body else None
      req = Request(url, data=data, method=method, headers={
          "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
          "Content-Type": "application/json", "Prefer": "return=minimal",
      })
      try:
          with urlopen(req, timeout=10) as r: return r.status, r.read().decode()
      except HTTPError as e: return e.code, e.read().decode()[:200]
      except Exception as e: return None, str(e)

  def main():
      if not SUPABASE_URL or not SUPABASE_KEY:
          print("WARN: No Supabase credentials — skipping setup")
          sys.exit(0)
      proj = SUPABASE_URL.split("//")[1].split(".")[0]
      print(f"Supabase setup — project: {proj} | session: {GITHUB_RUN_ID}")
      all_ok = True
      for table in REQUIRED:
          status, body = sb("GET", table, params="?limit=1&select=id")
          ok = status in (200, 206)
          print(f"  {'OK' if ok else 'MISSING'} {table} (HTTP {status})")
          if not ok: all_ok = False
      if not all_ok:
          print("ERROR: Required tables missing — run schema setup manually")
          sys.exit(1)
      now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
      expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 4*3600))
      # Register session (upsert on github_run_id)
      status, _ = sb("POST", "active_sessions", {
          "github_run_id": GITHUB_RUN_ID, "is_active": True,
          "created_at": now, "updated_at": now, "expires_at": expires,
      })
      print(f"Session registered: HTTP {status}")
      sb("POST", "agent_status", {
          "github_run_id": GITHUB_RUN_ID, "status": "starting",
          "current_task": "", "tools_called": 0,
          "last_activity": now, "updated_at": now,
      })
      print("Supabase setup complete ✓")

  if __name__ == "__main__":
      main()
  