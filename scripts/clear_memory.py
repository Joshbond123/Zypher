#!/usr/bin/env python3
"""
Zypher Memory Reset v1
======================
Wipes all Supabase memory tables and resets MEMORY.md to a clean state.
Run from GitHub Actions with SUPABASE_URL + SUPABASE_SERVICE_KEY set.

Usage:
  python3 scripts/clear_memory.py           # wipe + reset
  python3 scripts/clear_memory.py --dry-run # preview only, no changes
"""
import os, sys, json, time, argparse
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from datetime import datetime, timezone

SB_URL  = os.environ.get("SUPABASE_URL", "")
SB_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")
RUN_ID  = os.environ.get("GITHUB_RUN_ID", "manual")
WORKSPACE = os.path.expanduser("~/.openclaw/workspace")
MEMORY_MD = os.path.join(WORKSPACE, "MEMORY.md")


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
        print(f"  ✓ {name}: cleared (HTTP {code})")
        return True
    else:
        print(f"  ✗ {name}: HTTP {code} — {resp[:100]}")
        return False


def reset_memory_md():
    """Write a clean MEMORY.md."""
    os.makedirs(WORKSPACE, exist_ok=True)
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
    print(f"  ✓ MEMORY.md reset to clean state")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    args = ap.parse_args()

    print(f"\n=== Zypher Memory Reset {'(DRY RUN)' if args.dry_run else ''} ===")
    print(f"Supabase: {'configured' if SB_URL else 'NOT SET'}")
    print(f"Run ID: {RUN_ID}\n")

    if args.dry_run:
        print("DRY RUN — no changes made")
        return

    if not SB_URL or not SB_KEY:
        print("WARN: No Supabase credentials — only resetting local MEMORY.md")
        reset_memory_md()
        return

    print("Wiping Supabase tables...")
    # Each table needs a filter that matches all rows (Supabase REST requires a filter for DELETE)
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

    print("\nResetting MEMORY.md...")
    reset_memory_md()

    # Also clean stale session files that cause EmbeddedAttemptSessionTakeoverError
    agents_dir = os.path.expanduser("~/.openclaw/agents")
    if os.path.exists(agents_dir):
        import shutil
        shutil.rmtree(agents_dir, ignore_errors=True)
        print(f"  ✓ Cleared stale session files from {agents_dir}")
    else:
        print(f"  ✓ {agents_dir} does not exist (nothing to clean)")

    print("\n=== Memory reset complete ===\n")


if __name__ == "__main__":
    main()
