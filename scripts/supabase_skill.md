# Supabase REST API — Zypher Skill

You have a Supabase database for persistent memory. Access it via bash using curl.
Environment variables always available in every session:
- $SUPABASE_URL — project REST URL
- $SUPABASE_SERVICE_KEY — service role key (full access)
- $GITHUB_RUN_ID — current workflow run ID

## Tables & Purposes
| Table | Purpose | Key columns |
|---|---|---|
| chat_messages | Full conversation history | role, content, session_id, created_at |
| longterm_memory | Key-value long-term facts | key, value, user_id, updated_at |
| memory_entries | Categorized memory | key, value, category, user_id, updated_at |
| task_log | Tool call + task events | task_id, status, message, session_id |
| agent_status | Live agent state | status, current_task, tools_called |
| active_sessions | Session registry | github_run_id, is_active, expires_at |

## Save a long-term fact (UPSERT)
```bash
curl -s -X POST "$SUPABASE_URL/rest/v1/longterm_memory" \
  -H "apikey: $SUPABASE_SERVICE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" \
  -H "Content-Type: application/json" \
  -H "Prefer: resolution=merge-duplicates" \
  -d '{"key":"target","value":"10.0.0.1","user_id":"joshbond"}'
```

## Read recent facts
```bash
curl -s "$SUPABASE_URL/rest/v1/longterm_memory?order=updated_at.desc&limit=20" \
  -H "apikey: $SUPABASE_SERVICE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" | python3 -m json.tool
```

## Save categorized memory entry (UPSERT)
```bash
curl -s -X POST "$SUPABASE_URL/rest/v1/memory_entries" \
  -H "apikey: $SUPABASE_SERVICE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" \
  -H "Content-Type: application/json" \
  -H "Prefer: resolution=merge-duplicates" \
  -d '{"key":"recon_notes","value":"target has ports 80,443,8080 open","category":"pentest","user_id":"joshbond"}'
```

## Read conversation history
```bash
curl -s "$SUPABASE_URL/rest/v1/chat_messages?order=created_at.desc&limit=20&select=role,content,created_at" \
  -H "apikey: $SUPABASE_SERVICE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_KEY"
```

## Log a task event
```bash
curl -s -X POST "$SUPABASE_URL/rest/v1/task_log" \
  -H "apikey: $SUPABASE_SERVICE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=minimal" \
  -d "{\"task_id\":\"task_$(date +%s)\",\"status\":\"complete\",\"message\":\"Done\",\"session_id\":\"$GITHUB_RUN_ID\"}"
```

## Remember a fact (shorthand)
Simply say "remember that X" and Zypher will write it to both MEMORY.md and Supabase longterm_memory.

## View all facts
```bash
curl -s "$SUPABASE_URL/rest/v1/longterm_memory?order=updated_at.desc&limit=50" \
  -H "apikey: $SUPABASE_SERVICE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" | python3 -m json.tool
```
