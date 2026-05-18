#!/usr/bin/env python3
  """
  Zypher Supabase Bridge v2
  - Parses OpenClaw log and writes chat messages to Supabase chat_messages table
  - Sends heartbeat to active_sessions
  - Syncs important facts to longterm_memory table
  """

  import os, json, time, re, logging, urllib.request, urllib.error
  from datetime import datetime, timezone

  logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
  logger = logging.getLogger(__name__)

  SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
  SUPABASE_KEY     = os.environ.get("SUPABASE_SERVICE_KEY", "")
  GITHUB_RUN_ID    = os.environ.get("GITHUB_RUN_ID", "local")
  LOG_FILE         = "/tmp/openclaw.log"

  # Pattern to detect OpenClaw message log lines
  # OpenClaw logs format: [timestamp] [level] role: content
  MSG_PATTERNS = [
      re.compile(r'(?:user|human)[:>]\s*(.+)', re.IGNORECASE),
      re.compile(r'(?:assistant|zypher|agent)[:>]\s*(.+)', re.IGNORECASE),
      re.compile(r'"role":\s*"(user|assistant)",\s*"content":\s*"([^"]{10,})"'),
      re.compile(r'\[(?:user|assistant)\]\s*(.+)', re.IGNORECASE),
  ]

  def sb(method, path, body=None, params=""):
      """Make a Supabase REST API call."""
      url = f"{SUPABASE_URL}/rest/v1/{path}{params}"
      data = json.dumps(body).encode() if body else None
      req = urllib.request.Request(url, data=data, method=method, headers={
          "apikey": SUPABASE_KEY,
          "Authorization": f"Bearer {SUPABASE_KEY}",
          "Content-Type": "application/json",
          "Prefer": "return=minimal"
      })
      try:
          with urllib.request.urlopen(req, timeout=8):
              return True
      except urllib.error.HTTPError as e:
          logger.debug(f"sb {method} {path}: HTTP {e.code} {e.read()[:200]}")
      except Exception as e:
          logger.debug(f"sb {method} {path}: {e}")
      return False

  def heartbeat():
      """Update session last-seen timestamp."""
      ok = sb("PATCH",
          "active_sessions",
          {"is_active": True, "updated_at": datetime.now(timezone.utc).isoformat()},
          f"?github_run_id=eq.{GITHUB_RUN_ID}")
      if ok:
          logger.info(f"Heartbeat ✓ run={GITHUB_RUN_ID}")

  def write_message(role: str, content: str):
      """Write a chat message to Supabase."""
      if len(content) < 3:
          return
      ok = sb("POST", "chat_messages", {
          "role": role,
          "content": content[:4000],
          "session_id": GITHUB_RUN_ID,
          "created_at": datetime.now(timezone.utc).isoformat()
      })
      if ok:
          logger.info(f"Saved [{role}] message ({len(content)} chars)")

  def write_fact(key: str, value: str):
      """Write a long-term fact to Supabase longterm_memory."""
      sb("POST", "longterm_memory", {
          "key": key,
          "value": value[:2000],
          "user_id": "joshbond",
          "created_at": datetime.now(timezone.utc).isoformat()
      })

  def parse_messages(lines: list) -> list:
      """Extract (role, content) pairs from OpenClaw log lines."""
      found = []
      for line in lines:
          line = line.strip()
          if not line or len(line) < 10:
              continue

          # Detect user messages
          if any(kw in line.lower() for kw in ['[user]', '"role": "user"', 'user:', 'from telegram']):
              # Try to extract content
              m = re.search(r'content["\':\s]+([^\'",]{10,})', line, re.IGNORECASE)
              if m:
                  found.append(('user', m.group(1).strip()))
              elif 'from telegram' in line.lower():
                  m2 = re.search(r'from telegram[^:]*:\s*(.+)', line, re.IGNORECASE)
                  if m2:
                      found.append(('user', m2.group(1).strip()))

          # Detect assistant messages
          elif any(kw in line.lower() for kw in ['[assistant]', '"role": "assistant"', 'zypher:', 'agent reply']):
              m = re.search(r'content["\':\s]+([^\'",]{10,})', line, re.IGNORECASE)
              if m:
                  found.append(('assistant', m.group(1).strip()))

          # Save notable tool results as facts
          elif any(kw in line.lower() for kw in ['tool_result', 'exec_result', 'web_fetch result']):
              if len(line) > 50:
                  key = f"tool_result_{int(time.time())}"
                  write_fact(key, line[:500])

      return found

  def tail_log(last_pos=0):
      """Read new lines from OpenClaw log since last_pos."""
      try:
          with open(LOG_FILE, "r", errors="replace") as f:
              f.seek(last_pos)
              lines = f.readlines()
              new_pos = f.tell()
          return lines, new_pos
      except FileNotFoundError:
          return [], last_pos

  def main():
      if not SUPABASE_URL or not SUPABASE_KEY:
          logger.info("No Supabase credentials — running heartbeat-only mode")
          while True:
              time.sleep(60)

      logger.info(f"Supabase bridge v2 started — session {GITHUB_RUN_ID}")
      log_pos = 0
      tick = 0
      seen_messages = set()  # dedup

      while True:
          time.sleep(20)
          tick += 1

          # Heartbeat every 2 minutes
          if tick % 6 == 0:
              heartbeat()

          # Parse new log lines for messages
          lines, log_pos = tail_log(log_pos)
          if lines:
              messages = parse_messages(lines)
              for role, content in messages:
                  key = f"{role}:{content[:60]}"
                  if key not in seen_messages:
                      seen_messages.add(key)
                      write_message(role, content)
                      # Keep dedup set bounded
                      if len(seen_messages) > 500:
                          seen_messages = set(list(seen_messages)[-200:])

              # Log interesting lines to stdout
              for line in lines:
                  line = line.strip()
                  if line and any(k in line.lower() for k in
                      ["error","warn","telegram","cerebras","exec","tool","browser","web_fetch","memory"]):
                      logger.info(f"[openclaw] {line[:200]}")

  if __name__ == "__main__":
      main()
  