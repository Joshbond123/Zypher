#!/usr/bin/env python3
  """
  Zypher Status Agent v6 — Internal Monitor Only
  ================================================
  DESIGN: Zero Telegram output. No typing indicators. No messages. No alerts.
  All Telegram communication is handled exclusively by the OpenClaw gateway.

  This agent exists only to:
    1. Monitor gateway health (internal logging)
    2. Log channel activity (internal logging)
    3. Provide structured internal diagnostics

  WHY SILENT:
    - Typing indicators from a background Python script desync from actual
      agent activity and cause "stuck typing" indicators in Telegram.
    - "Still processing..." / "Alert 1/3" messages from prior versions were
      noise that polluted the chat and confused users.
    - The OpenClaw gateway's streaming.mode:block provides native delivery
      progress. An external monitor creating Telegram messages on top of this
      creates duplicate, conflicting output.
    - OpenClaw's stuckSessionWarnMs/stuckSessionAbortMs diagnostics handle
      stuck session detection internally.

  REMOVED from v5:
    - Typing indicator sends (were causing "stuck typing" in Telegram)
    
  REMOVED from v4/v3/v2/v1:
    - Startup notification (handled by gateway)
    - Error forwarding (noise)
    - Stall alerts ("Still working...", "Alert 1/3")
    - Pulse messages ("Still processing...")
    - Ack messages ("Got it. Working on...")
  """
  import os, json, time, logging
  from urllib.request import urlopen, Request

  logging.basicConfig(format="%(asctime)s [STATUS] %(message)s", level=logging.INFO)
  log = logging.getLogger("status-agent")

  GW_RPC_URL      = "http://127.0.0.1:18789/api/v1/admin/rpc"
  POLL_INTERVAL   = 30          # seconds between health checks
  STARTUP_DELAY   = 25          # wait before starting


  def gw_health():
      """True if gateway HTTP API responds."""
      try:
          data = json.dumps({"method": "health"}).encode()
          req = Request(GW_RPC_URL, data=data, method="POST",
                        headers={"Content-Type": "application/json"})
          with urlopen(req, timeout=4) as r:
              resp = json.loads(r.read())
              return bool(resp.get("ok"))
      except Exception:
          return False


  def main():
      log.info("Status agent v6 starting (silent monitor, no Telegram output)")
      time.sleep(STARTUP_DELAY)

      last_health = False
      check_count = 0

      while True:
          try:
              time.sleep(POLL_INTERVAL)
              check_count += 1
              healthy = gw_health()

              if healthy != last_health:
                  status = "HEALTHY" if healthy else "UNREACHABLE"
                  log.info("Gateway status changed: %s (check #%d)", status, check_count)
                  last_health = healthy
              elif check_count % 10 == 0:
                  log.info("Gateway health: %s (check #%d)",
                           "ok" if healthy else "unreachable", check_count)

          except KeyboardInterrupt:
              log.info("Status agent shutting down")
              break
          except Exception as e:
              log.warning("Status agent error: %s", e)
              time.sleep(10)


  if __name__ == "__main__":
      main()
  