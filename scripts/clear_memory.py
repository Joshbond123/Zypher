#!/usr/bin/env python3
  """
  Zypher Memory Reset v2
  ======================
  Wipes all Supabase memory tables and resets MEMORY.md to a clean state.
  Run from GitHub Actions with SUPABASE_URL + SUPABASE_SERVICE_KEY set.

  BUGFIX v2: Fixed wrong path from ~/.openclaw/workspace to ~/.hermes/memories.
             Fixed stale session cleanup from ~/.openclaw/agents to ~/.hermes/.

  Usage:
    python3 scripts/clear_memory.py           # wipe + reset
    python3 scripts/clear_memory.py --dry-run # preview only, no changes
  """
  import os, sys, json, time, argparse, shutil
  from urllib.request import urlopen, Request
  from urllib.error import HTTPError
  from datetime import datetime, timezone

  SB_URL  = os.environ.get("SUPABASE_URL", "")
  SB_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")
  RUN_ID  = os.environ.get("GITHUB_RUN_ID", "manual")

  HERMES_HOME = os.path.expanduser("~/.hermes")
  MEM_DIR     = os.path.join(HERMES_HOME, "memories")
  MEMORY_MD   = os.path.join(MEM_DIR, "MEMORY.md")
  USER_MD     = os.path.join(MEM_DIR, "USER.md")


  def sb(method, path, body=None, params=""):
      """Make a Supabase REST API call. Returns (status_code, response_text)."""
      if not SB_URL or not SB_KEY:
          return 0, "no-credentials"
      url = f"{SB_URL}/rest/v1/{path}{params}"
      data = json.dumps(body).encode() if body else None
      headers = {
          "apikey": SB_KEY,
          "Authorization": f"Bearer {SB_KEY}",
          "Content-Type": "application/json",
          "Prefer": "return=minimal",
      }
      req = Request(url, data=data, method=method, headers=headers)
      try:
          with urlopen(req, timeout=15) as r:
              return r.status, r.read().decode()[:200]
      except HTTPError as e:
          return e.code, e.read().decode()[:200]
      except Exception as e:
          return -1, str(e)


  def wipe_table(name, filter_param):
      """Delete all rows from a table using a broad filter."""
      code, resp = sb("DELETE", name, params=filter_param)
      if code in (200, 204):
          print(f"  OK {name}: cleared (HTTP {code})")
          return True
      else:
          print(f"  WARN {name}: HTTP {code} — {resp[:100]}")
          return False


  def reset_memory_md():
      """Write a clean MEMORY.md to the correct Hermes path."""
      os.makedirs(MEM_DIR, exist_ok=True)
      now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
      content = f"""# Zypher Memory
  *Reset on {now} | Run {RUN_ID}*

  ## Identity
  - Name: Zypher
  - Operator: Joshbond
  - Platform: GitHub Actions (Ubuntu/Kali tools)
  - Channel: Telegram @Zypher0_bot

  ## Long-term Facts
  *(empty — fresh start)*

  ## Notes
  *(empty)*
  """
      with open(MEMORY_MD, "w") as f:
          f.write(content)
      print(f"  OK MEMORY.md reset ({MEMORY_MD})")


  def reset_user_md():
      """Write a clean USER.md to the correct Hermes path."""
      os.makedirs(MEM_DIR, exist_ok=True)
      content = """# User Profile
  *Reset — fresh start*

  ## User
  - Name: Joshbond
  - Handle: @Joshbond
  - Timezone: unknown
  """
      with open(USER_MD, "w") as f:
          f.write(content)
      print(f"  OK USER.md reset ({USER_MD})")


  def main():
      ap = argparse.ArgumentParser()
      ap.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
      args = ap.parse_args()

      print(f"\n=== Zypher Memory Reset {'(DRY RUN)' if args.dry_run else ''} ===")
      print(f"Supabase: {'configured' if SB_URL else 'NOT SET'}")
      print(f"Hermes home: {HERMES_HOME}")
      print(f"Run ID: {RUN_ID}\n")

      if args.dry_run:
          print("DRY RUN — no changes made")
          return

      if not SB_URL or not SB_KEY:
          print("WARN: No Supabase credentials — only resetting local memory files")
          reset_memory_md()
          reset_user_md()
          return

      print("Wiping Supabase tables...")
      tables = [
          ("chat_messages",   "?created_at=gte.2000-01-01"),
          ("longterm_memory", "?id=gte.0"),
          ("memory_entries",  "?id=gte.0"),
          ("task_log",        "?created_at=gte.2000-01-01"),
          ("agent_status",    "?github_run_id=gte.0"),
          ("active_sessions", "?created_at=gte.2000-01-01"),
      ]

      ok_count = 0
      for table_name, filter_param in tables:
          if wipe_table(table_name, filter_param):
              ok_count += 1
          time.sleep(0.2)

      print(f"\n{ok_count}/{len(tables)} tables cleared")

      print("\nResetting local memory files...")
      reset_memory_md()
      reset_user_md()

      # Remove state.db so Hermes starts with a fresh session database
      import glob as globmod
      state_db = os.path.join(HERMES_HOME, "state.db")
      for p in [state_db, state_db + "-wal", state_db + "-shm"]:
          if os.path.exists(p):
              os.remove(p)
              print(f"  OK removed {p}")

      # Clean stale pairing/session files under ~/.hermes
      for stale_dir in ["agents", "platforms/pairing", "pairing"]:
          d = os.path.join(HERMES_HOME, stale_dir)
          if os.path.exists(d):
              shutil.rmtree(d, ignore_errors=True)
              print(f"  OK cleared stale dir {d}")

      print("\n=== Memory reset complete ===\n")


  if __name__ == "__main__":
      main()
  