#!/usr/bin/env python3
"""
Bootstrap MEMORY.md from Supabase — v2
Pulls chat history, long-term facts, and memory_entries.
Gracefully handles missing tables or Supabase unavailability.
"""
import argparse, json, os, time
from urllib.request import urlopen, Request
from urllib.error import HTTPError


def sb_get(url, key, path, params=""):
    """Fetch from Supabase REST. Returns list on success, [] on any error."""
    if not url or not key:
        return []
    try:
        req = Request(
            f"{url}/rest/v1/{path}{params}",
            method="GET",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
        )
        with urlopen(req, timeout=10) as r:
            if r.status in (200, 206):
                data = json.loads(r.read())
                return data if isinstance(data, list) else []
            return []
    except HTTPError as e:
        # 404 = table missing, 401 = bad key — both non-fatal
        print(f"  WARN: {path} fetch HTTP {e.code} (non-fatal)")
        return []
    except Exception as e:
        print(f"  WARN: {path} fetch error: {e} (non-fatal)")
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--msgs",   required=True,  help="Path to pre-fetched chat_messages JSON")
    ap.add_argument("--facts",  required=True,  help="Path to pre-fetched longterm_memory JSON")
    ap.add_argument("--run-id", default="unknown")
    ap.add_argument("--out",    required=True,  help="Output MEMORY.md path")
    ap.add_argument("--sb-url", default=os.environ.get("SUPABASE_URL", ""))
    ap.add_argument("--sb-key", default=os.environ.get("SUPABASE_SERVICE_KEY", ""))
    args = ap.parse_args()

    # Load pre-fetched data (from curl in the workflow step)
    try:
        msgs = json.loads(open(args.msgs).read())
        if not isinstance(msgs, list):
            msgs = []
    except Exception:
        msgs = []

    try:
        facts = json.loads(open(args.facts).read())
        if not isinstance(facts, list):
            facts = []
    except Exception:
        facts = []

    # Also try to fetch memory_entries directly from Supabase
    mem_entries = []
    if args.sb_url and args.sb_key:
        mem_entries = sb_get(
            args.sb_url, args.sb_key, "memory_entries",
            "?order=updated_at.desc&limit=50&select=key,value,category,updated_at"
        )

    # Reverse messages so oldest is first in the file
    msgs = list(reversed(msgs))
    now_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    lines = [
        "# Zypher Persistent Memory",
        f"# Run: {args.run_id} | Built: {now_str}",
        "",
        "## Long-term Facts",
    ]

    if mem_entries:
        for e in mem_entries:
            cat = e.get("category", "")
            key = e.get("key", "?")
            val = str(e.get("value", ""))[:300]
            prefix = f"[{cat}] " if cat else ""
            lines.append(f"- {prefix}{key}: {val}")
    elif facts:
        for f in facts:
            key = f.get("key", "?")
            val = str(f.get("value", ""))[:200]
            lines.append(f"- {key}: {val}")
    else:
        lines.append("(no long-term facts yet — use 'remember that X' to store facts)")

    lines += [
        "",
        f"## Conversation History (last {len(msgs)} messages)",
    ]

    if msgs:
        for m in msgs:
            ts   = str(m.get("created_at", ""))[:16]
            role = m.get("role", "?").upper()
            body = str(m.get("content", ""))[:400]
            lines.append(f"[{ts}] {role}: {body}")
    else:
        lines.append("(no prior conversation — fresh session)")

    lines += [
        "",
        "## How to Use Memory",
        "- **Remember fact**: tell Zypher 'remember that X'",
        "- **View memory**: `cat ~/.openclaw/workspace/MEMORY.md`",
        "- **Supabase reference**: `cat ~/.openclaw/workspace/skills/supabase.md`",
        "- **Save to Supabase**: use the supabase skill curl commands",
    ]

    out_path = os.path.expanduser(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    open(out_path, "w").write("\n".join(lines) + "\n")

    print(
        f"Memory bootstrapped: {len(msgs)} messages, "
        f"{len(facts)} facts, {len(mem_entries)} memory entries"
    )


if __name__ == "__main__":
    main()
