#!/bin/bash
# Configure OpenClaw's Supabase plugin connection
set -e

echo "=== Setting up OpenClaw Supabase Plugin ==="

# Try native openclaw supabase plugin install
openclaw plugins install clawhub:supabase 2>/dev/null || true

# Configure via environment
mkdir -p ~/.openclaw/plugins/supabase
cat > ~/.openclaw/plugins/supabase/config.json << CONFIG
{
  "url": "$SUPABASE_URL",
  "key": "$SUPABASE_SERVICE_KEY",
  "schema": "public",
  "tables": {
    "sessions": "active_sessions",
    "messages": "chat_messages",
    "users": "profiles",
    "telegram": "telegram_connections",
    "model_status": "agent_status"
  },
  "realtime": false
}
CONFIG

echo "Supabase plugin configured at ~/.openclaw/plugins/supabase/config.json"

# Also store as env for the agent python script
export SUPABASE_PLUGIN_CONFIGURED=1
