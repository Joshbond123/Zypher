#!/usr/bin/env python3
"""
Zypher Schema Setup — Auto-verify and create all required Supabase tables.
Safe to run multiple times. Uses Management API if SUPABASE_PAT is available,
otherwise verifies via REST and reports missing tables.
"""
import os, sys, json, time
from urllib.request import urlopen, Request
from urllib.error import HTTPError

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_PAT  = os.environ.get("SUPABASE_PAT", "")
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")

# All required tables with their DDL
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_messages (
  id         BIGSERIAL PRIMARY KEY,
  session_id TEXT,
  role       TEXT NOT NULL,
  content    TEXT NOT NULL,
  user_id    TEXT,
  tool_name  TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_created ON chat_messages(created_at DESC);

CREATE TABLE IF NOT EXISTS longterm_memory (
  id         BIGSERIAL PRIMARY KEY,
  key        TEXT NOT NULL,
  value      TEXT,
  user_id    TEXT DEFAULT 'joshbond',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(key, user_id)
);
CREATE INDEX IF NOT EXISTS idx_longterm_key ON longterm_memory(key);
CREATE INDEX IF NOT EXISTS idx_longterm_updated ON longterm_memory(updated_at DESC);

CREATE TABLE IF NOT EXISTS memory_entries (
  id         BIGSERIAL PRIMARY KEY,
  key        TEXT NOT NULL,
  value      TEXT,
  category   TEXT,
  user_id    TEXT DEFAULT 'joshbond',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(key, user_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_cat ON memory_entries(category);
CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_entries(updated_at DESC);

CREATE TABLE IF NOT EXISTS task_log (
  id          BIGSERIAL PRIMARY KEY,
  task_id     TEXT,
  session_id  TEXT,
  status      TEXT,
  message     TEXT,
  tool_name   TEXT,
  duration_ms INTEGER,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_task_log_session ON task_log(session_id);
CREATE INDEX IF NOT EXISTS idx_task_log_created ON task_log(created_at DESC);

CREATE TABLE IF NOT EXISTS agent_status (
  id            BIGSERIAL PRIMARY KEY,
  github_run_id TEXT UNIQUE NOT NULL,
  status        TEXT DEFAULT 'idle',
  current_task  TEXT DEFAULT '',
  last_tool     TEXT DEFAULT '',
  tools_called  INTEGER DEFAULT 0,
  last_activity TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS active_sessions (
  id            BIGSERIAL PRIMARY KEY,
  github_run_id TEXT UNIQUE NOT NULL,
  is_active     BOOLEAN DEFAULT true,
  instance_num  INTEGER DEFAULT 1,
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now(),
  expires_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_active_sessions_active ON active_sessions(is_active);

CREATE TABLE IF NOT EXISTS cerebras_keys (
  id          BIGSERIAL PRIMARY KEY,
  key_id      TEXT UNIQUE NOT NULL,
  masked_key  TEXT,
  requests    INTEGER DEFAULT 0,
  rate_limits INTEGER DEFAULT 0,
  failures    INTEGER DEFAULT 0,
  last_used   TIMESTAMPTZ,
  last_error  TEXT,
  updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS telegram_connections (
  id           BIGSERIAL PRIMARY KEY,
  user_id      TEXT UNIQUE NOT NULL,
  chat_id      TEXT,
  username     TEXT,
  connected_at TIMESTAMPTZ DEFAULT now(),
  active       BOOLEAN DEFAULT true
);
"""

REQUIRED_TABLES = [
    "chat_messages", "longterm_memory", "memory_entries",
    "task_log", "agent_status", "active_sessions",
    "cerebras_keys", "telegram_connections",
]


def rest_check(table):
    """Check if a table exists via Supabase REST API."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None, "no-credentials"
    url = f"{SUPABASE_URL}/rest/v1/{table}?limit=1&select=id"
    req = Request(url, method="GET", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urlopen(req, timeout=8) as r:
            return r.status, "ok"
    except HTTPError as e:
        return e.code, e.read().decode()[:100]
    except Exception as e:
        return None, str(e)


def mgmt_run_sql(project_ref, sql):
    """Run SQL via Supabase Management API (requires PAT)."""
    url = f"https://api.supabase.com/v1/projects/{project_ref}/database/query"
    data = json.dumps({"query": sql}).encode()
    req = Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {SUPABASE_PAT}",
        "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return None, str(e)


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("WARN: No Supabase credentials — schema check skipped")
        sys.exit(0)

    proj = SUPABASE_URL.split("//")[1].split(".")[0] if "//" in SUPABASE_URL else "unknown"
    print(f"Schema check — project: {proj} | run: {GITHUB_RUN_ID}")

    # Check all required tables
    missing = []
    for table in REQUIRED_TABLES:
        status, msg = rest_check(table)
        ok = status in (200, 206)
        print(f"  {'OK' if ok else 'MISSING'} {table} (HTTP {status})")
        if not ok:
            missing.append(table)

    if not missing:
        print(f"Schema OK — all {len(REQUIRED_TABLES)} tables present")
        return

    print(f"\nMissing tables: {missing}")

    # Try to create tables via Management API if PAT is available
    if SUPABASE_PAT:
        print("Attempting auto-create via Management API...")
        status, body = mgmt_run_sql(proj, SCHEMA_SQL)
        if status in (200, 201):
            print("Schema created via Management API OK")
            # Verify
            still_missing = []
            for table in missing:
                s, _ = rest_check(table)
                if s not in (200, 206):
                    still_missing.append(table)
            if still_missing:
                print(f"ERROR: Still missing after creation: {still_missing}")
                sys.exit(1)
            else:
                print("All tables verified after creation")
        else:
            print(f"Management API failed: HTTP {status} — {body[:200]}")
            print("Tables are missing but no PAT to create them. Agent will run with degraded memory.")
    else:
        print("WARN: SUPABASE_PAT not set — cannot auto-create tables")
        print("Missing tables will cause Supabase logging to fail silently.")
        print("To fix: add SUPABASE_PAT to GitHub secrets, or run setup_schema.py locally.")


if __name__ == "__main__":
    main()
