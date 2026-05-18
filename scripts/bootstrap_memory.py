#!/usr/bin/env python3
"""Bootstrap MEMORY.md from Supabase JSON files."""
import argparse, json, os, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--msgs',   required=True)
    ap.add_argument('--facts',  required=True)
    ap.add_argument('--run-id', default='unknown')
    ap.add_argument('--out',    required=True)
    args = ap.parse_args()

    try:
        msgs = json.loads(open(args.msgs).read())
    except Exception:
        msgs = []
    try:
        facts = json.loads(open(args.facts).read())
    except Exception:
        facts = []

    msgs = list(reversed(msgs))

    lines = [
        f"# Zypher Memory — Run {args.run_id}",
        "",
        "## Long-term Facts",
    ]
    if facts:
        for f in facts:
            lines.append(f"- {f.get('key','?')}: {str(f.get('value',''))[:200]}")
    else:
        lines.append("(none yet)")

    lines += ["", "## Conversation History (last 30)"]
    if msgs:
        for m in msgs:
            ts      = m.get('created_at', '')[:16]
            role    = m.get('role', '?').upper()
            content = str(m.get('content', ''))[:300]
            lines.append(f"[{ts}] {role}: {content}")
    else:
        lines.append("(none yet)")

    out_path = os.path.expanduser(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    open(out_path, 'w').write('\n'.join(lines))
    print(f"Memory bootstrapped: {len(msgs)} messages, {len(facts)} facts")

if __name__ == '__main__':
    main()
