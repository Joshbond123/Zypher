#!/usr/bin/env python3
"""hermes_stage_artifact.py — Stage Hermes memory files for artifact upload.

ROOT CAUSE FIX (primary persistence bug):
  actions/upload-artifact@v4 preserves the full directory structure of absolute
  paths inside the artifact ZIP. When uploaded with:
    ~/.hermes/memories/MEMORY.md  ->  stored as  home/runner/.hermes/memories/MEMORY.md
  After "gh run download --dir /tmp/memory-restore", files land at:
    /tmp/memory-restore/home/runner/.hermes/memories/MEMORY.md

  BUT hermes_bootstrap_memory.py expects flat paths:
    /tmp/memory-restore/MEMORY.md  <-- NEVER FOUND

  Fix: Copy all files to /tmp/hermes-artifact/ with flat names BEFORE upload.
  hermes_bootstrap_memory.py already expects flat paths — no changes needed there.

Additional fixes applied here:
  - WAL checkpoint on state.db AFTER Hermes is killed (safe, exclusive access)
  - Skills directory packed into skills.tar.gz for artifact transport
  - SOUL.md backed up (though always re-written from AGENTS.md on startup)
  - Graceful handling of missing files (non-fatal)
"""
import os, sys, shutil, sqlite3, tarfile, glob
from datetime import datetime, timezone

HERMES_HOME = os.path.expanduser("~/.hermes")
MEM_DIR     = os.path.join(HERMES_HOME, "memories")
STATE_DB    = os.path.join(HERMES_HOME, "state.db")
SKILLS_DIR  = os.path.join(HERMES_HOME, "skills")
SOUL_MD     = os.path.join(HERMES_HOME, "SOUL.md")

STAGING_DIR = "/tmp/hermes-artifact"
os.makedirs(STAGING_DIR, exist_ok=True)

now = datetime.now(timezone.utc).isoformat()
print(f"[hermes_stage_artifact] staging at {now}")
print(f"  source: {HERMES_HOME}")
print(f"  dest  : {STAGING_DIR}")


def checkpoint_wal(db_path):
    """
    Checkpoint WAL into the main database file after Hermes is stopped.
    Since the process is dead, we can safely open in read-write mode and
    run PRAGMA wal_checkpoint(TRUNCATE) to flush all WAL transactions and
    truncate the WAL file.  This ensures state.db is a self-contained file
    with no outstanding -wal dependency.
    """
    if not os.path.exists(db_path):
        print("  state.db: not present — skipping WAL checkpoint")
        return False
    try:
        con = sqlite3.connect(db_path, timeout=20)
        con.execute("PRAGMA journal_mode=WAL")
        mode, size, ckpt = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        con.close()
        print(f"  state.db: WAL checkpoint OK (mode={mode} size={size} ckpt={ckpt})")
        # Remove residual WAL/SHM files so the artifact is self-contained
        for ext in ("-wal", "-shm"):
            p = db_path + ext
            if os.path.exists(p):
                os.remove(p)
                print(f"  state.db: removed {os.path.basename(p)}")
        return True
    except Exception as e:
        print(f"  WARN state.db WAL checkpoint: {e}")
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
        f"# Hermes artifact manifest",
        f"# Staged: {now}",
        f"# Files: {len(staged)}",
    ]
    for f in sorted(staged):
        p = os.path.join(STAGING_DIR, f)
        manifest_lines.append(f"{f}: {os.path.getsize(p)} bytes")
    p = os.path.join(STAGING_DIR, "manifest.txt")
    open(p, "w").write("\n".join(manifest_lines) + "\n")
    print(f"  STAGED manifest.txt")


# ── Step 1: WAL checkpoint (Hermes must be stopped first) ──────────────────
print("\n[1/4] WAL checkpoint...")
checkpoint_wal(STATE_DB)

# ── Step 2: Copy flat files ─────────────────────────────────────────────────
print("\n[2/4] Staging files...")
safe_copy(os.path.join(MEM_DIR, "MEMORY.md"), "MEMORY.md")
safe_copy(os.path.join(MEM_DIR, "USER.md"),   "USER.md")
safe_copy(STATE_DB,                            "state.db")
safe_copy(SOUL_MD,                             "SOUL.md")

# ── Step 3: Pack skills ──────────────────────────────────────────────────────
print("\n[3/4] Packing skills...")
pack_skills()

# ── Step 4: Write manifest ───────────────────────────────────────────────────
print("\n[4/4] Writing manifest...")
write_manifest()

print("\n=== Staging complete ===")
print("Files in", STAGING_DIR + ":")
for f in sorted(os.listdir(STAGING_DIR)):
    size = os.path.getsize(os.path.join(STAGING_DIR, f))
    print(f"  {f}: {size:,} bytes")
sys.exit(0)
