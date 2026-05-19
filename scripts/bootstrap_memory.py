#!/usr/bin/env python3
  """Bootstrap MEMORY.md from Supabase — pulls chat messages, facts, and memory_entries."""
  import argparse, json, os, time
  from urllib.request import urlopen, Request

  def sb_get(url, key, path, params=""):
      if not url or not key: return []
      try:
          req = Request(f"{url}/rest/v1/{path}{params}", method="GET", headers={
              "apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"
          })
          with urlopen(req, timeout=10) as r: return json.loads(r.read())
      except Exception: return []

  def main():
      ap = argparse.ArgumentParser()
      ap.add_argument("--msgs",   required=True)
      ap.add_argument("--facts",  required=True)
      ap.add_argument("--run-id", default="unknown")
      ap.add_argument("--out",    required=True)
      ap.add_argument("--sb-url", default=os.environ.get("SUPABASE_URL", ""))
      ap.add_argument("--sb-key", default=os.environ.get("SUPABASE_SERVICE_KEY", ""))
      args = ap.parse_args()
      try:   msgs  = json.loads(open(args.msgs).read())
      except: msgs = []
      try:   facts = json.loads(open(args.facts).read())
      except: facts = []
      mem_entries = []
      if args.sb_url and args.sb_key:
          mem_entries = sb_get(args.sb_url, args.sb_key, "memory_entries",
                               "?order=updated_at.desc&limit=30&select=key,value,category,updated_at")
      msgs = list(reversed(msgs))
      now_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
      lines = ["# Zypher Persistent Memory", f"# Run: {args.run_id} | Built: {now_str}", "", "## Long-term Facts"]
      if mem_entries:
          for e in mem_entries:
              cat = e.get("category", ""); key = e.get("key","?"); val = str(e.get("value",""))[:300]
              lines.append(f"- {('['+cat+'] ') if cat else ''}{key}: {val}")
      elif facts:
          for f in facts: lines.append(f"- {f.get('key','?')}: {str(f.get('value',''))[:200]}")
      else: lines.append("(none yet)")
      lines += ["", f"## Conversation History (last {len(msgs)})"]
      if msgs:
          for m in msgs:
              lines.append(f"[{str(m.get('created_at',''))[:16]}] {m.get('role','?').upper()}: {str(m.get('content',''))[:400]}")
      else: lines.append("(no prior conversation)")
      lines += ["", "## Memory Tips",
                "- To remember: tell Zypher 'remember that X'",
                "- View memory: cat ~/.openclaw/workspace/MEMORY.md",
                "- Supabase reference: cat ~/.openclaw/workspace/skills/supabase.md"]
      out_path = os.path.expanduser(args.out)
      os.makedirs(os.path.dirname(out_path), exist_ok=True)
      open(out_path, "w").write("\n".join(lines))
      print(f"Memory bootstrapped: {len(msgs)} msgs, {len(facts)} facts, {len(mem_entries)} entries")

  if __name__ == "__main__":
      main()
  