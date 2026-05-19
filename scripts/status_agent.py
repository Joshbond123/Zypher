#!/usr/bin/env python3
  """
  Zypher Status Agent — Dedicated Telegram Progress Reporter
  ==========================================================
  Background daemon alongside OpenClaw gateway.
  - Sends startup confirmation to Telegram (after 20s delay)
  - Detects tool activity and reply events from the OpenClaw log
  - Sends stall alert if no activity for 90s while task is in progress
  - Sends proactive pulse every 10 min when busy
  - Alerts on errors (with 120s dedup cooldown)
  """
  import os, re, json, time, logging, hashlib
  from urllib.request import urlopen, Request

  logging.basicConfig(format="%(asctime)s [STATUS] %(message)s", level=logging.INFO)
  log = logging.getLogger("status-agent")

  TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
  CHAT_ID         = "6317345496"
  GITHUB_RUN_ID   = os.environ.get("GITHUB_RUN_ID", "?")
  INSTANCE_NUM    = os.environ.get("INSTANCE_NUMBER", "1")
  LOG_FILE        = "/tmp/openclaw.log"
  POLL_INTERVAL   = 8
  STALL_THRESHOLD = 90
  PULSE_INTERVAL  = 600
  ERROR_COOLDOWN  = 120


  def tg_send(text):
      if not TELEGRAM_TOKEN:
          log.warning("No TELEGRAM_BOT_TOKEN")
          return False
      try:
          data = json.dumps({"chat_id": CHAT_ID, "text": text[:4096]}).encode()
          req = Request(
              f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
              data=data, method="POST",
              headers={"Content-Type": "application/json"},
          )
          with urlopen(req, timeout=10) as r:
              return json.loads(r.read()).get("ok", False)
      except Exception as e:
          log.debug("tg_send: %s", e)
          return False


  TOOL_RE  = re.compile(r'(?:tool_call|exec|bash|web_fetch|web_search|browser)\s*[:\s]\s*([^\n]{5,})', re.I)
  USER_RE  = re.compile(r'(?:inbound|from telegram|user message)\s*[:\s]\s*(.{5,})', re.I)
  REPLY_RE = re.compile(r'(?:sending reply|telegram send|response sent)', re.I)
  ERROR_RE = re.compile(r'(?:\berror\b|fatal|crash|refused|timeout|rate.?limit)', re.I)


  class Tracker:
      def __init__(self):
          self.last_activity  = time.time()
          self.last_pulse     = time.time()
          self.last_stall     = 0
          self.is_busy        = False
          self.current_task   = ""
          self.tools          = 0
          self.dedup_cache    = set()
          self.last_errors    = {}

      def on_user(self, text=""):
          self.last_activity = time.time(); self.current_task = (text or "")[:100]
          self.is_busy = True; self.tools = 0

      def on_tool(self):
          self.last_activity = time.time(); self.tools += 1; self.is_busy = True

      def on_reply(self):
          self.last_activity = time.time(); self.is_busy = False
          self.current_task = ""; self.tools = 0

      def should_stall(self):
          if not self.is_busy: return False
          if time.time() - self.last_activity < STALL_THRESHOLD: return False
          if time.time() - self.last_stall < STALL_THRESHOLD: return False
          self.last_stall = time.time(); return True

      def should_pulse(self):
          return self.is_busy and time.time() - self.last_pulse > PULSE_INTERVAL

      def dedup(self, key):
          h = hashlib.md5(key.encode()).hexdigest()[:12]
          if h in self.dedup_cache: return True
          self.dedup_cache.add(h)
          if len(self.dedup_cache) > 200: self.dedup_cache = set(list(self.dedup_cache)[-80:])
          return False


  def fmt(secs):
      m, s = divmod(int(secs), 60); h, m = divmod(m, 60)
      if h: return f"{h}h {m}m"
      if m: return f"{m}m {s}s"
      return f"{s}s"


  def main():
      log.info("Status agent — instance #%s run %s", INSTANCE_NUM, GITHUB_RUN_ID)
      tracker = Tracker(); start_t = time.time(); log_pos = 0
      time.sleep(20)
      tg_send(
          f"Zypher online — Instance #{INSTANCE_NUM}\n"
          f"Run: {GITHUB_RUN_ID}\n"
          f"Ready — 4 Cerebras keys, streaming off. Type anything to start."
      )
      while True:
          time.sleep(POLL_INTERVAL)
          try:
              with open(LOG_FILE, "r", errors="replace") as f:
                  f.seek(log_pos); lines = f.readlines(); log_pos = f.tell()
          except FileNotFoundError:
              lines = []
          for line in lines:
              s = line.strip()
              if not s or len(s) < 8: continue
              if USER_RE.search(s):
                  m = USER_RE.search(s); tracker.on_user(m.group(1) if m else "")
              elif TOOL_RE.search(s):
                  tracker.on_tool()
              elif REPLY_RE.search(s):
                  tracker.on_reply()
              elif ERROR_RE.search(s):
                  key = s[:80]
                  if time.time() - tracker.last_errors.get(key, 0) > ERROR_COOLDOWN:
                      tracker.last_errors[key] = time.time()
                      if not tracker.dedup("err:" + key): tg_send(f"Warning: {s[:250]}")
          if tracker.should_stall():
              elapsed = fmt(time.time() - tracker.last_activity)
              if not tracker.dedup(f"stall:{elapsed}"):
                  tg_send(f"Still working... (no update for {elapsed})\nTask: {tracker.current_task or 'in progress'}\nTools used so far: {tracker.tools}")
          if tracker.should_pulse():
              tracker.last_pulse = time.time()
              if not tracker.dedup(f"pulse:{tracker.current_task}"):
                  tg_send(f"Status — still working\nUptime: {fmt(time.time()-start_t)} | Tools: {tracker.tools}\nCurrent: {tracker.current_task or 'processing'}")

  if __name__ == "__main__":
      main()
  