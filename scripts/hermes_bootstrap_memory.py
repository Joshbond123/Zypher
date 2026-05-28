#!/usr/bin/env python3
  """hermes_bootstrap_memory.py — Restore Hermes memory and state on startup.

  FIXES (May 2026):
    - ROOT CAUSE FIX: Artifact path structure mismatch.
      Previous artifact upload used ~/.hermes/memories/MEMORY.md (absolute path).
      upload-artifact@v4 preserves the full path structure inside the ZIP:
        home/runner/.hermes/memories/MEMORY.md
      After "gh run download --dir /tmp/memory-restore":
        /tmp/memory-restore/home/runner/.hermes/memories/MEMORY.md
      But this script was looking for /tmp/memory-restore/MEMORY.md (flat path) -> MISS.
      NEW: The workflow now stages files to a flat directory /tmp/hermes-artifact/
      before upload via hermes_stage_artifact.py, so flat-path lookup works correctly.
      FALLBACK: If flat file not found, recursive glob searches ARTIFACT_DIR for
      the filename anywhere in the tree (handles old-format artifacts gracefully).

    - state.db restore now handles WAL mode correctly:
      After restoring, runs PRAGMA wal_checkpoint(FULL) to flush WAL -> main db
      and PRAGMA journal_mode=WAL to ensure WAL mode is active on the fresh copy.
    - Uses sqlite3.backup() API for the clean copy operation
    - Artifact-first strategy: /tmp/memory-restore/{file} always wins over Supabase
    - Corruption detection: corrupt files renamed to .corrupt, fresh db started
    - Skills restore from artifact (skills.tar.gz) and Supabase (hermes_skill_* keys)
    - Handles state.db-wal and state.db-shm cleanup after restore
  """
  import os,json,datetime,base64,sqlite3,shutil,glob,tarfile
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
          con.execute("PRAGMA journal_mode=WAL")
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

  def find_in_artifact(fname):
      """
      Find a file by name anywhere under ARTIFACT_DIR.
      First tries the flat path (new format from hermes_stage_artifact.py).
      Falls back to recursive glob for backwards compatibility with old artifacts
      that used absolute-path uploads (preserving home/runner/.hermes/... structure).
      Returns the found path or None.
      """
      # 1. Flat path (new format — hermes_stage_artifact.py stages to flat dir)
      flat = os.path.join(ARTIFACT_DIR, fname)
      if os.path.exists(flat) and os.path.getsize(flat) > 0:
          return flat
      # 2. Recursive search (backwards compat: old artifact used absolute paths)
      matches = glob.glob(os.path.join(ARTIFACT_DIR, "**", fname), recursive=True)
      if matches:
          # Pick largest (most complete) if multiple found
          best = max(matches, key=lambda p: os.path.getsize(p))
          if os.path.getsize(best) > 0:
              print(f"  find_in_artifact: {fname} found at nested path {best}")
              return best
      return None

  def restore_text(fname,key,default):
      """Restore a text memory file (MEMORY.md, USER.md)."""
      path=os.path.join(MEM,fname)
      # 1. Artifact (flat path or recursive search)
      artifact_path = find_in_artifact(fname)
      if artifact_path and os.path.getsize(artifact_path) > 50:
          shutil.copy2(artifact_path, path)
          print(f"{fname}: restored from artifact ({os.path.getsize(path)}b) [{artifact_path}]")
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
      Priority: artifact (flat or nested) > Supabase > fresh start
      After restore: checkpoint, activate WAL, remove stale WAL files.
      """
      print("state.db: starting restore...")

      # 1. Artifact (flat path first, then recursive)
      artifact_db = find_in_artifact("state.db")
      if artifact_db and os.path.getsize(artifact_db) > 0:
          tmp=STATE_DB+".restore_tmp"
          if clean_copy_statedb(artifact_db,tmp):
              if check_integrity(tmp):
                  remove_wal_files(STATE_DB)
                  shutil.move(tmp,STATE_DB)
                  checkpoint_and_activate_wal(STATE_DB)
                  print(f"state.db: restored from artifact ({os.path.getsize(STATE_DB):,}b) [{artifact_db}]")
                  return
              else:
                  print(f"WARN state.db: artifact copy failed integrity check — trying Supabase")
                  try:os.remove(tmp)
                  except:pass
          else:
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
              print(f"WARN state.db: on-disk copy corrupt -> moved to state.db.corrupt")

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
                  print(f"state.db: restored from Supabase ({os.path.getsize(STATE_DB):,}b) — integrity OK")
                  return
              else:
                  print(f"WARN state.db: Supabase copy failed integrity check")
                  try:os.remove(tmp)
                  except:pass
          except Exception as e:
              print(f"WARN state.db Supabase restore: {e}")

      # 4. Fresh start
      print("state.db: no valid backup found — Hermes will create a fresh database")

  def restore_skills():
      """
      Restore ~/.hermes/skills/ from:
        1. skills.tar.gz in artifact (packed by hermes_stage_artifact.py)
        2. Individual skill files from Supabase (hermes_skill_* keys)
      """
      os.makedirs(SKILLS_DIR, exist_ok=True)

      # 1. skills.tar.gz from artifact
      tar_path = find_in_artifact("skills.tar.gz")
      if tar_path:
          try:
              with tarfile.open(tar_path, "r:gz") as tf:
                  tf.extractall(SKILLS_DIR)
              extracted = os.listdir(SKILLS_DIR)
              print(f"skills: restored {len(extracted)} file(s) from artifact tar [{tar_path}]")
              return
          except Exception as e:
              print(f"WARN skills tar restore: {e}")

      # 2. Supabase fallback: restore individual skill files
      if not SB_URL or not SB_KEY:
          print("skills: no artifact and no Supabase credentials — skipping")
          return
      try:
          req=Request(
              f"{SB_URL}/rest/v1/longterm_memory?key=like.hermes_skill_*&select=key,value",
              headers={"apikey":SB_KEY,"Authorization":f"Bearer {SB_KEY}"}
          )
          with urlopen(req,timeout=10) as r:
              rows=json.loads(r.read())
          if not rows:
              print("skills: no skill files in Supabase")
              return
          count=0
          for row in rows:
              fname=row["key"].replace("hermes_skill_","")
              if row.get("value") and fname:
                  open(os.path.join(SKILLS_DIR,fname),"w").write(row["value"])
                  count+=1
          print(f"skills: restored {count} file(s) from Supabase")
      except Exception as e:
          print(f"WARN skills Supabase restore: {e}")

  def main():
      os.makedirs(MEM,exist_ok=True)
      os.makedirs(SKILLS_DIR,exist_ok=True)

      print(f"=== hermes_bootstrap_memory | run={RUN_ID} | {NOW} ===")
      print(f"Artifact dir: {ARTIFACT_DIR}")
      if os.path.exists(ARTIFACT_DIR):
          files = []
          for root, dirs, fnames in os.walk(ARTIFACT_DIR):
              for fn in fnames:
                  fp = os.path.join(root, fn)
                  files.append(f"  {fp} ({os.path.getsize(fp)}b)")
          if files:
              print("Artifact contents:")
              for f in files:
                  print(f)
          else:
              print("Artifact dir is empty")
      else:
          print("Artifact dir not found — fresh start")

      print("\n--- Restoring MEMORY.md ---")
      restore_text("MEMORY.md","hermes_memory",
          "# Zypher Memory\n*Initialized by bootstrap*\n\n## Identity\n- Name: Zypher\n- Operator: Joshbond\n")

      print("\n--- Restoring USER.md ---")
      restore_text("USER.md","hermes_user",
          "# User Profile\n*Initialized by bootstrap*\n\n## User\n- Name: Joshbond\n")

      print("\n--- Restoring state.db ---")
      restore_statedb()

      print("\n--- Restoring skills ---")
      restore_skills()

      print("\n=== Bootstrap complete ===")
      print("~/.hermes/memories/:")
      for f in os.listdir(MEM):
          p = os.path.join(MEM, f)
          print(f"  {f}: {os.path.getsize(p)}b")
      if os.path.exists(STATE_DB):
          print(f"state.db: {os.path.getsize(STATE_DB):,}b")
      print(f"skills: {len(os.listdir(SKILLS_DIR))} file(s)")

  if __name__=="__main__":
      main()
  