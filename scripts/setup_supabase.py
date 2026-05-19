#!/usr/bin/env python3
"""
Zypher Supabase Session Setup
Verifies table health and registers/updates session. Non-fatal on all errors.
"""
import os, sys, json, time
from urllib.request import urlopen, Request
from urllib.error import HTTPError

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")

REQUIRED = [
    "chat_messages", "longterm_memory", "memory_entries",
    "task_log", "agent_status", "active_sessions",
]


def sb(method, path, body=None, params=""):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None, "no-credentials"
    url = f"{SUPABASE_URL}/rest/v1/{path}{params}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal,resolution=merge-duplicates",
    })
    try:
        with urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:
        return None, str(e)


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("WARN: No Supabase credentials — session registration skipped")
        sys.exit(0)

    proj = SUPABASE_URL.split("//")[1].split(".")[0] if "//" in SUPABASE_URL else "unknown"
    print(f"Supabase setup — project: {proj} | session: {GITHUB_RUN_ID}")

    # Verify tables exist (informational only — we don't exit on missing)
    all_ok = True
    for table in REQUIRED:
        status, _ = sb("GET", table, params="?limit=1&select=id")
        ok = status in (200, 206)
        print(f"  {'OK' if ok else 'MISSING'} {table} (HTTP {status})")
        if not ok:
            all_ok = False

    if not all_ok:
        print("WARN: Some tables missing — memory logging will be degraded")
        print("      Run scripts/setup_schema.py to fix (requires SUPABASE_PAT)")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 4 * 3600))

    # Register/update session (upsert via resolution=merge-duplicates)
    status, body = sb("POST", "active_sessions", {
        "github_run_id": GITHUB_RUN_ID,
        "is_active": True,
        "created_at": now,
        "updated_at": now,
        "expires_at": expires,
    })
    if status in (200, 201, 204):
        print(f"Session registered OK (HTTP {status})")
    else:
        print(f"WARN: Session registration returned HTTP {status}: {body[:100]}")

    # Initialize agent status row (upsert)
    status2, _ = sb("POST", "agent_status", {
        "github_run_id": GITHUB_RUN_ID,
        "status": "starting",
        "current_task": "",
        "tools_called": 0,
        "last_activity": now,
        "updated_at": now,
    })
    print(f"Agent status initialized: HTTP {status2}")
    print("Supabase setup complete")


if __name__ == "__main__":
    main()
