#!/usr/bin/env python3
"""hermes_stage_artifact.py -- Stage Hermes memory files for artifact upload.

ROOT CAUSE FIX (primary persistence bug):
  upload-artifact@v4 preserves the full directory structure of absolute paths.
  When uploaded as ~/.hermes/memories/MEMORY.md it is stored inside the ZIP as
  home/runner/.hermes/memories/MEMORY.md. After download to /tmp/memory-restore,
  the file lands at /tmp/memory-restore/home/runner/.hermes/memories/MEMORY.md.
  But hermes_bootstrap_memory.py looks for the FLAT path /tmp/memory-restore/MEMORY.md
  and never finds it. Fix: stage files to /tmp/hermes-artifact/ with flat names.

WAL SAFETY FIX (race condition -- v2):
  Original code used PRAGMA wal_checkpoint(TRUNCATE) which requires exclusive
  access and fails (returns busy>0) if Hermes has not fully exited yet.
  When busy>0, WAL frames are NOT checkpointed but we removed the WAL files,
  causing data loss of everything written since the last checkpoint.

  Fix: use sqlite3.backup() API (same as hermes_sidecar.py):
  - Safe to call while another process has the database open
  - Uses SQLite Online Backup API -- reads a consistent WAL snapshot
  - Does NOT require exclusive locks
  - Produces a clean self-contained database file (no -wal dependency)
  This eliminates the race condition between kill -TERM and WAL staging.
"""
import os, sys, shutil, sqlite3, tarfile, glob
from datetime import datetime, timezone

HERMES_HOME = os.path.expanduser("~/.hermes")
MEM_DIR     = os.path.join(HERMES_HOME, "memories")
STATE_DB    = os.path.join(HERMES_HOME, "state.db")
PERSIST_STATE_DB = os.environ.get("HERMES_PERSIST_STATE_DB", "0").lower() in ("1", "true", "yes")
SKILLS_DIR  = os.path.join(HERMES_HOME, "skills")
SOUL_MD     = os.path.join(HERMES_HOME, "SOUL.md")

STAGING_DIR = "/tmp/hermes-artifact"
# Always start from an empty staging directory so an intentionally skipped
# state.db from a prior local/test run cannot be uploaded accidentally.
if os.path.isdir(STAGING_DIR):
    shutil.rmtree(STAGING_DIR)
os.makedirs(STAGING_DIR, exist_ok=True)

now = datetime.now(timezone.utc).isoformat()
print(f"[hermes_stage_artifact] staging at {now}")
print(f"  source: {HERMES_HOME}")
print(f"  dest  : {STAGING_DIR}")


def stage_statedb(db_path, dst_name):
    """
    Stage state.db using sqlite3.backup() -- WAL-safe online backup.

    WHY sqlite3.backup() INSTEAD OF wal_checkpoint(TRUNCATE):
      TRUNCATE requires that no readers are using the WAL file. If Hermes is
      still shutting down (SIGTERM received but not yet exited), it still has
      the database open. TRUNCATE then returns busy>0 meaning some WAL frames
      were NOT flushed. We removed the WAL files, losing that data.

      sqlite3.backup() uses the SQLite Online Backup API:
      - Works safely while another process has the database open
      - Reads a consistent snapshot including all WAL data
      - Produces a clean self-contained file with no -wal dependency
      Identical approach to hermes_sidecar.py Supabase backups.
    """
    if not os.path.exists(db_path):
        print(f"  SKIP {dst_name}: source not found ({db_path})")
        return False
    if os.path.getsize(db_path) == 0:
        print(f"  SKIP {dst_name}: source is empty")
        return False

    dst = os.path.join(STAGING_DIR, dst_name)
    tmp = dst + ".tmp"
    try:
        # WAL-safe online backup -- safe even if Hermes is still open
        src_con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        dst_con = sqlite3.connect(tmp)
        with dst_con:
            src_con.backup(dst_con, pages=0)  # pages=0 = entire DB in one step
        src_con.close()
        dst_con.close()

        # Verify integrity of the backup copy
        chk = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True, timeout=5)
        res = chk.execute("PRAGMA integrity_check").fetchone()
        chk.close()

        if res and res[0] == "ok":
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(tmp, dst)
            size = os.path.getsize(dst)
            print(f"  STAGED {dst_name}: {size:,} bytes (WAL-safe backup, integrity OK)")
            return True
        else:
            print(f"  WARN {dst_name}: backup integrity check failed ({res}) -- falling back to direct copy")
            try: os.remove(tmp)
            except Exception: pass
    except Exception as e:
        print(f"  WARN {dst_name} sqlite3.backup() failed: {e} -- falling back to direct copy")
        try: os.remove(tmp)
        except Exception: pass

    # Fallback: direct copy (less safe but better than nothing)
    try:
        shutil.copy2(db_path, dst)
        print(f"  STAGED {dst_name}: {os.path.getsize(dst):,} bytes (direct copy fallback)")
        return True
    except Exception as e:
        print(f"  FAIL {dst_name}: {e}")
        return False


def safe_copy(src, dst_name):
    """Copy src to STAGING_DIR/<dst_name>. Non-fatal on missing source."""
    dst = os.path.join(STAGING_DIR, dst_name)
    if not os.path.exists(src):
        print(f"  SKIP {dst_name}: source not found ({src})")
        return False
    if os.path.getsize(src) == 0:
        print(f"  SKIP {dst_name}: source is empty")
        return False
    shutil.copy2(src, dst)
    print(f"  STAGED {dst_name}: {os.path.getsize(dst):,} bytes")
    return True


def pack_skills():
    """Pack ~/.hermes/skills/ into skills.tar.gz in staging dir."""
    if not os.path.isdir(SKILLS_DIR):
        print("  SKIP skills.tar.gz: no skills directory")
        return False
    skill_files = (
        glob.glob(os.path.join(SKILLS_DIR, "*.md")) +
        glob.glob(os.path.join(SKILLS_DIR, "*.py")) +
        glob.glob(os.path.join(SKILLS_DIR, "*.json")) +
        glob.glob(os.path.join(SKILLS_DIR, "*.yaml")) +
        glob.glob(os.path.join(SKILLS_DIR, "*.txt"))
    )
    if not skill_files:
        print("  SKIP skills.tar.gz: skills directory is empty")
        return False
    tar_path = os.path.join(STAGING_DIR, "skills.tar.gz")
    with tarfile.open(tar_path, "w:gz") as tf:
        for fpath in skill_files:
            tf.add(fpath, arcname=os.path.basename(fpath))
    print(f"  STAGED skills.tar.gz: {len(skill_files)} skill(s), {os.path.getsize(tar_path):,} bytes")
    return True


def write_manifest():
    """Write a small manifest so we can verify artifact integrity on restore."""
    staged = os.listdir(STAGING_DIR)
    manifest_lines = [
        "# Hermes artifact manifest",
        f"# Staged: {now}",
        f"# Files: {len(staged)}",
    ]
    for f in sorted(staged):
        p = os.path.join(STAGING_DIR, f)
        manifest_lines.append(f"{f}: {os.path.getsize(p)} bytes")
    p = os.path.join(STAGING_DIR, "manifest.txt")
    open(p, "w").write("\n".join(manifest_lines) + "\n")
    print("  STAGED manifest.txt")


# -- Step 1: state.db intentionally not persisted by default -----------------
print("\n[1/4] Staging state.db (disabled by default)...")
if PERSIST_STATE_DB:
    stage_statedb(STATE_DB, "state.db")
else:
    print("  SKIP state.db: HERMES_PERSIST_STATE_DB is disabled; text memory persists without session bloat")

# -- Step 2: Copy flat text files -------------------------------------------
print("\n[2/4] Staging files...")
safe_copy(os.path.join(MEM_DIR, "MEMORY.md"), "MEMORY.md")
safe_copy(os.path.join(MEM_DIR, "USER.md"),   "USER.md")
safe_copy(SOUL_MD,                             "SOUL.md")

# -- Step 3: Pack skills -----------------------------------------------------
print("\n[3/4] Packing skills...")
pack_skills()

# -- Step 4: Write manifest --------------------------------------------------
print("\n[4/4] Writing manifest...")
write_manifest()

print("\n=== Staging complete ===")
print("Files in", STAGING_DIR + ":")
for f in sorted(os.listdir(STAGING_DIR)):
    size = os.path.getsize(os.path.join(STAGING_DIR, f))
    print(f"  {f}: {size:,} bytes")
sys.exit(0)