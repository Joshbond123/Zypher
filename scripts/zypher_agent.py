#!/usr/bin/env python3
  """
  Zypher Supabase Bridge v4
  =========================
  - Polls OpenClaw log every 5 seconds (was 20s)
  - Robust JSON + regex parsing for OpenClaw log format
  - Writes chat_messages, task_log, agent_status, active_sessions heartbeat
  - Refreshes MEMORY.md every 5 minutes from Supabase
  - Writes daily memory/YYYY-MM-DD.md notes
  - Crash-safe: all Supabase calls wrapped in try/except
  """
  import os, json, time, re, logging, hashlib, threading
  from datetime import datetime, timezone, date
  from urllib.request import urlopen, Request
  from urllib.error import HTTPError

  logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
  log = logging.getLogger("zypher-bridge")

  SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
  SUPABASE_KEY    = os.environ.get("SUPABASE_SERVICE_KEY", "")
  GITHUB_RUN_ID   = os.environ.get("GITHUB_RUN_ID", "local")
  LOG_FILE        = "/tmp/openclaw.log"
  WORKSPACE_DIR   = os.path.expanduser("~/.openclaw/workspace")
  MEMORY_DIR      = os.path.join(WORKSPACE_DIR, "memory")
  POLL_INTERVAL   = 5
  HEARTBEAT_EVERY = 6
  MEMORY_FLUSH    = 60
  SB_TIMEOUT      = 6


  def sb(method, path, body=None, params=""):
      if not SUPABASE_URL or not SUPABASE_KEY:
          return False, "no-credentials"
      url = f"{SUPABASE_URL}/rest/v1/{path}{params}"
      data = json.dumps(body).encode() if body else None
      req = Request(url, data=data, method=method, headers={
          "apikey": SUPABASE_KEY,
          "Authorization": f"Bearer {SUPABASE_KEY}",
          "Content-Type": "application/json",
          "Prefer": "return=minimal",
      })
      try:
          with urlopen(req, timeout=SB_TIMEOUT) as resp:
              return True, resp.read().decode()
      except HTTPError as e:
          return False, f"http_{e.code}"
      except Exception as e:
          return False, str(e)


  class AgentState:
      def __init__(self):
          self.current_task = ""; self.last_tool = ""; self.tools_called = 0
          self.status = "idle"; self.last_activity = time.time()
          self.lock = threading.Lock()

      def record_tool(self, name):
          with self.lock:
              self.last_tool = name; self.tools_called += 1
              self.status = "working"; self.last_activity = time.time()

      def record_task(self, task):
          with self.lock:
              self.current_task = (task or "")[:200]
              self.status = "working"; self.last_activity = time.time()

      def record_reply(self):
          with self.lock:
              self.status = "idle"; self.last_activity = time.time()

      def to_dict(self):
          with self.lock:
              now = datetime.now(timezone.utc).isoformat()
              return {
                  "github_run_id": GITHUB_RUN_ID, "current_task": self.current_task,
                  "last_tool": self.last_tool, "tools_called": self.tools_called,
                  "status": self.status, "last_activity": now, "updated_at": now,
              }


  STATE = AgentState()

  USER_PAT = [
      re.compile(r'"text"\s*:\s*"([^"]{5,})"', re.I),
      re.compile(r'inbound.*?(?:from\s+\d+|dm)[:\s]+(.{5,})', re.I),
  ]
  ASST_PAT = [
      re.compile(r'\[(?:agent|assistant|reply)\][:\s]+(.{5,})', re.I),
      re.compile(r'(?:sending reply|telegram send)[:\s]+(.{5,})', re.I),
  ]
  TOOL_RE = re.compile(r'(?:tool_call|exec|bash|web_fetch|web_search|browser)[:\s]+([^\n]{5,})', re.I)
  TASK_RE = re.compile(r'(?:processing|received message|inbound dm)[:\s]+(.{5,})', re.I)
  INTERESTING = {"error","warn","telegram","cerebras","exec","tool","browser","web_fetch","memory","reply","inbound","fatal"}


  def parse_line(line):
      result = {}
      s = line.strip()
      if not s or len(s) < 8:
          return result
      try:
          obj = json.loads(s)
          msg = obj.get("msg", "")
          if obj.get("role") == "user" or "inbound" in msg.lower() or "telegram message" in msg.lower():
              text = obj.get("text") or obj.get("content") or ""
              if len(str(text)) > 4:
                  result["role"] = "user"; result["content"] = str(text)
          elif obj.get("role") == "assistant" or "send reply" in msg.lower():
              text = obj.get("text") or obj.get("content") or obj.get("reply", "")
              if len(str(text)) > 4:
                  result["role"] = "assistant"; result["content"] = str(text)
          if "tool" in msg.lower() or obj.get("tool"):
              result["tool"] = obj.get("tool") or msg
          return result
      except (json.JSONDecodeError, TypeError):
          pass
      for pat in USER_PAT:
          m = pat.search(s)
          if m and len(m.group(1)) > 4:
              result["role"] = "user"; result["content"] = m.group(1); break
      if "role" not in result:
          for pat in ASST_PAT:
              m = pat.search(s)
              if m and len(m.group(1)) > 4:
                  result["role"] = "assistant"; result["content"] = m.group(1); break
      m = TOOL_RE.search(s)
      if m:
          result["tool"] = m.group(1)[:100]
      m = TASK_RE.search(s)
      if m and "role" not in result:
          result["task"] = m.group(1)[:200]
      return result


  def write_message(role, content):
      if len(content) < 3:
          return
      sb("POST", "chat_messages", {
          "role": role, "content": content[:4000],
          "session_id": GITHUB_RUN_ID,
          "created_at": datetime.now(timezone.utc).isoformat(),
      })


  def write_task_log(status, message):
      sb("POST", "task_log", {
          "session_id": GITHUB_RUN_ID,
          "task_id": f"task_{int(time.time())}",
          "status": status, "message": message[:500],
          "created_at": datetime.now(timezone.utc).isoformat(),
      })


  def push_agent_status():
      state = STATE.to_dict()
      ok, body = sb("GET", "agent_status", params=f"?github_run_id=eq.{GITHUB_RUN_ID}&limit=1")
      if ok and body and body != "[]":
          sb("PATCH", "agent_status", state, f"?github_run_id=eq.{GITHUB_RUN_ID}")
      else:
          sb("POST", "agent_status", state)


  def heartbeat():
      now = datetime.now(timezone.utc).isoformat()
      ok, _ = sb("PATCH", "active_sessions",
                 {"is_active": True, "updated_at": now},
                 f"?github_run_id=eq.{GITHUB_RUN_ID}")
      if ok:
          log.info("Heartbeat ok run=%s", GITHUB_RUN_ID)


  def refresh_memory_files():
      if not SUPABASE_URL or not SUPABASE_KEY:
          return
      ok, msgs_raw = sb("GET", "chat_messages",
                        params="?order=created_at.desc&limit=40&select=role,content,created_at")
      ok2, facts_raw = sb("GET", "longterm_memory",
                          params="?order=updated_at.desc&limit=30&select=key,value,updated_at")
      msgs  = list(reversed(json.loads(msgs_raw)))  if ok and msgs_raw and msgs_raw != "[]" else []
      facts = json.loads(facts_raw) if ok2 and facts_raw and facts_raw != "[]" else []
      lines = [
          "# Zypher Persistent Memory",
          f"# Session: {GITHUB_RUN_ID}  Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
          "", "## Long-term Facts",
      ]
      for f in facts:
          lines.append(f"- {f.get('key','?')}: {str(f.get('value',''))[:300]}")
      if not facts:
          lines.append("(none yet)")
      lines += ["", f"## Recent Conversation (last {len(msgs)})"]
      for m in msgs:
          ts = str(m.get("created_at", ""))[:16]
          role = m.get("role", "?").upper()
          content = str(m.get("content", ""))[:400]
          lines.append(f"[{ts}] {role}: {content}")
      if not msgs:
          lines.append("(none yet)")
      os.makedirs(MEMORY_DIR, exist_ok=True)
      with open(os.path.join(WORKSPACE_DIR, "MEMORY.md"), "w") as f:
          f.write("\n".join(lines))
      log.info("Memory refreshed: %d msgs, %d facts", len(msgs), len(facts))
      today = date.today().isoformat()
      daily = os.path.join(MEMORY_DIR, f"{today}.md")
      if not os.path.exists(daily):
          with open(daily, "w") as f:
              f.write(f"# Zypher Daily — {today}\nRun: {GITHUB_RUN_ID}\n")


  def tail_log(pos):
      try:
          with open(LOG_FILE, "r", errors="replace") as f:
              f.seek(pos); lines = f.readlines(); return lines, f.tell()
      except FileNotFoundError:
          return [], pos


  def main():
      log.info("Zypher bridge v4 — session %s", GITHUB_RUN_ID)
      os.makedirs(MEMORY_DIR, exist_ok=True)
      try:
          refresh_memory_files()
      except Exception as e:
          log.warning("Initial memory refresh: %s", e)

      log_pos = 0; tick = 0; seen_msgs = set(); daily_buf = []; last_tool_write = 0

      while True:
          time.sleep(POLL_INTERVAL)
          tick += 1

          if tick % HEARTBEAT_EVERY == 0:
              try: heartbeat(); push_agent_status()
              except Exception as e: log.debug("Heartbeat: %s", e)

          if tick % MEMORY_FLUSH == 0:
              try: refresh_memory_files()
              except Exception as e: log.debug("Memory refresh: %s", e)

          if tick % 24 == 0 and daily_buf:
              today = date.today().isoformat()
              daily = os.path.join(MEMORY_DIR, f"{today}.md")
              try:
                  with open(daily, "a") as f:
                      for ln in daily_buf: f.write(ln.strip()[:300] + "\n")
                  daily_buf = []
              except Exception: pass

          lines, log_pos = tail_log(log_pos)
          for line in lines:
              s = line.strip()
              if not s: continue
              parsed = parse_line(s)
              if parsed.get("tool"):
                  STATE.record_tool(parsed["tool"])
                  if time.time() - last_tool_write > 1:
                      try: write_task_log("tool_call", f"Tool: {parsed['tool']}"); last_tool_write = time.time()
                      except Exception: pass
              if parsed.get("task"):
                  STATE.record_task(parsed["task"])
              role = parsed.get("role"); content = parsed.get("content", "")
              if role and content:
                  dedup = f"{role}:{hashlib.md5(content[:60].encode()).hexdigest()}"
                  if dedup not in seen_msgs:
                      seen_msgs.add(dedup)
                      if len(seen_msgs) > 1000: seen_msgs = set(list(seen_msgs)[-400:])
                      try: write_message(role, content)
                      except Exception: pass
                      if role == "assistant": STATE.record_reply()
                      elif role == "user": STATE.record_task(content)
              lower = s.lower()
              if any(k in lower for k in INTERESTING):
                  daily_buf.append(s)
                  log.info("[oc] %s", s[:250])

  if __name__ == "__main__":
      main()
  