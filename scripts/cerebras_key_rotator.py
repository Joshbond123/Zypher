#!/usr/bin/env python3
"""
Cerebras API Key Rotator — Zypher v5
=====================================
Round-robin key selection across all available Cerebras API keys.
Tracks: request count, rate-limit events, failed requests, last used.
Persists stats to ~/.openclaw/cerebras_key_stats.json and optionally Supabase.
Patches ~/.openclaw/openclaw.json with the selected key as primary + numbered providers.

Usage:
  python3 cerebras_key_rotator.py [--stats] [--reset-key KEY_SUFFIX]

Env vars consumed:
  CEREBRAS_API_KEY, CEREBRAS_API_KEY_2 ... CEREBRAS_API_KEY_N
  SUPABASE_URL, SUPABASE_SERVICE_KEY  (optional — for persistent sync)
  GITHUB_RUN_ID                       (optional — for session tagging)
"""

import json, os, sys, time, hashlib, pathlib, re
import urllib.request, urllib.error

STATS_FILE   = os.path.expanduser('~/.openclaw/cerebras_key_stats.json')
CONFIG_FILE  = os.path.expanduser('~/.openclaw/openclaw.json')
LOG_FILE     = '/tmp/key_rotator.log'
RATE_LIMIT_COOLDOWN = 90   # seconds before a rate-limited key is retried
MAX_FAILURES = 5           # consecutive failures before a key is deprioritised

# ── helpers ────────────────────────────────────────────────────────────────

def log(msg):
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    line = f'[{ts}] {msg}'
    print(line)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass

def key_id(key: str) -> str:
    """Stable short ID for a key (never reveals the full key)."""
    return 'key_' + hashlib.md5(key.encode()).hexdigest()[:8]

def mask(key: str) -> str:
    return key[:6] + '...' + key[-6:] if len(key) > 12 else '***'

def load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        try:
            return json.load(open(STATS_FILE))
        except Exception as e:
            log(f'WARN: could not load stats ({e}), starting fresh')
    return {}

def save_stats(stats: dict):
    pathlib.Path(STATS_FILE).parent.mkdir(parents=True, exist_ok=True)
    json.dump(stats, open(STATS_FILE, 'w'), indent=2)

def collect_keys() -> list:
    """Collect all CEREBRAS_API_KEY* env vars, deduplicated, in order."""
    raw = []
    # Primary key
    if os.environ.get('CEREBRAS_API_KEY'):
        raw.append(os.environ['CEREBRAS_API_KEY'])
    # Numbered keys
    for n in range(2, 20):
        k = os.environ.get(f'CEREBRAS_API_KEY_{n}') or os.environ.get(f'CEREBRAS_KEY_{n}')
        if k:
            raw.append(k)
    # Deduplicate preserving order
    seen, unique = set(), []
    for k in raw:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique

def select_key(keys: list, stats: dict) -> tuple:
    """
    Select the best available key using:
    1. Skip keys in rate-limit cooldown
    2. Among available, pick least-recently-used (round-robin)
    3. Deprioritise keys with many consecutive failures
    Returns (key, kid)
    """
    now = time.time()
    candidates = []
    skipped = []

    for key in keys:
        kid = key_id(key)
        st = stats.get(kid, {})
        rl_at = st.get('rate_limited_at', 0)
        consecutive_failures = st.get('consecutive_failures', 0)

        if rl_at and (now - rl_at) < RATE_LIMIT_COOLDOWN:
            remaining = int(RATE_LIMIT_COOLDOWN - (now - rl_at))
            skipped.append((kid, f'rate-limited ({remaining}s cooldown)'))
            continue

        last_used = st.get('last_used', 0)
        # Score: lower = preferred. Add penalty for consecutive failures.
        score = last_used + (consecutive_failures * 30)
        candidates.append((score, key, kid))

    if skipped:
        for kid, reason in skipped:
            log(f'  Skip {kid}: {reason}')

    if not candidates:
        log('WARN: All keys in cooldown — using least-recently-used (overriding cooldown)')
        candidates = [(stats.get(key_id(k), {}).get('last_used', 0), k, key_id(k)) for k in keys]

    candidates.sort(key=lambda x: x[0])
    _, best_key, best_kid = candidates[0]
    return best_key, best_kid

def patch_config(keys: list, selected_key: str):
    """Update openclaw.json: set selected key on cerebras provider, all keys on numbered providers."""
    if not os.path.exists(CONFIG_FILE):
        log(f'WARN: {CONFIG_FILE} not found — skipping config patch')
        return

    cfg = json.load(open(CONFIG_FILE))
    providers = cfg.setdefault('models', {}).setdefault('providers', {})

    model_defs = [
        {"id": "zai-glm-4.7",    "name": "Z.ai GLM 4.7",           "reasoning": True,  "input": ["text"], "contextWindow": 128000, "maxTokens": 8192},
        {"id": "gpt-oss-120b",   "name": "GPT OSS 120B",            "reasoning": True,  "input": ["text"], "contextWindow": 128000, "maxTokens": 8192},
        {"id": "llama3.1-8b",    "name": "Llama 3.1 8B",            "reasoning": False, "input": ["text"], "contextWindow": 128000, "maxTokens": 8192},
        {"id": "qwen-3-235b-a22b-instruct-2507", "name": "Qwen 3 235B", "reasoning": False, "input": ["text"], "contextWindow": 128000, "maxTokens": 8192},
    ]

    base_provider = {
        "baseUrl": "https://api.cerebras.ai/v1",
        "api": "openai-completions",
        "models": model_defs
    }

    # Patch each provider with its assigned key
    for i, key in enumerate(keys):
        pname = 'cerebras' if i == 0 else f'cerebras-{i+1}'
        if pname not in providers:
            providers[pname] = dict(base_provider)
            log(f'  Created provider {pname}')
        providers[pname]['apiKey'] = key

    # Always set primary cerebras to selected key (for round-robin session start)
    providers['cerebras']['apiKey'] = selected_key
    # Rotate: move selected key to position 0 perspective by updating primary
    sel_idx = keys.index(selected_key) if selected_key in keys else 0
    log(f'  Primary cerebras provider set to key index {sel_idx} ({mask(selected_key)})')

    # Rebuild fallback chain spanning all providers
    fallbacks = []
    models_priority = ['zai-glm-4.7', 'gpt-oss-120b', 'llama3.1-8b']
    for model in models_priority:
        for i in range(len(keys)):
            pname = 'cerebras' if i == 0 else f'cerebras-{i+1}'
            fid = f'{pname}/{model}'
            # Primary model on primary provider is already the primary — skip from fallbacks
            if i == 0 and model == 'zai-glm-4.7':
                continue
            fallbacks.append(fid)

    # Update agents.defaults.model
    model_cfg = cfg.setdefault('agents', {}).setdefault('defaults', {}).setdefault('model', {})
    model_cfg['primary'] = 'cerebras/zai-glm-4.7'
    model_cfg['fallbacks'] = fallbacks
    log(f'  Fallback chain: {len(fallbacks)} entries spanning {len(keys)} keys × 3 models')

    json.dump(cfg, open(CONFIG_FILE, 'w'), indent=2)
    log(f'  Config patched: {CONFIG_FILE}')

def sync_stats_to_supabase(stats: dict):
    """Optionally sync stats to Supabase longterm_memory for cross-run persistence."""
    url = os.environ.get('SUPABASE_URL', '')
    key = os.environ.get('SUPABASE_SERVICE_KEY', '')
    if not url or not key:
        return
    try:
        payload = json.dumps({
            'key': 'cerebras_key_stats',
            'value': json.dumps(stats),
            'user_id': 'system'
        }).encode()
        req = urllib.request.Request(
            f'{url}/rest/v1/longterm_memory',
            data=payload,
            method='POST',
            headers={
                'apikey': key,
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json',
                'Prefer': 'resolution=merge-duplicates,return=minimal'
            }
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            log(f'  Supabase stats sync: HTTP {r.status}')
    except Exception as e:
        log(f'  WARN: Supabase stats sync failed: {e}')

def load_stats_from_supabase() -> dict:
    """Load stats from Supabase if available, to preserve history across runs."""
    url = os.environ.get('SUPABASE_URL', '')
    key = os.environ.get('SUPABASE_SERVICE_KEY', '')
    if not url or not key:
        return {}
    try:
        req = urllib.request.Request(
            f'{url}/rest/v1/longterm_memory?key=eq.cerebras_key_stats&select=value&limit=1',
            method='GET',
            headers={'apikey': key, 'Authorization': f'Bearer {key}', 'Accept': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read())
            if rows:
                stats = json.loads(rows[0]['value'])
                log(f'  Loaded {len(stats)} key stats from Supabase')
                return stats
    except Exception as e:
        log(f'  WARN: Could not load stats from Supabase: {e}')
    return {}

def verify_selected_key(key: str) -> bool:
    """Quick API call to verify the selected key is responsive."""
    try:
        payload = json.dumps({
            'model': 'llama3.1-8b',
            'messages': [{'role': 'user', 'content': 'Say OK in 1 word.'}],
            'max_tokens': 4,
            'stream': False
        }).encode()
        req = urllib.request.Request(
            'https://api.cerebras.ai/v1/chat/completions',
            data=payload,
            method='POST',
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json'
            }
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            tokens = data.get('usage', {}).get('total_tokens', '?')
            log(f'  Key verified OK — tokens used: {tokens}')
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if e.code == 429:
            log(f'  WARN: Key returned 429 rate limit during verify')
            return False
        log(f'  WARN: Key verify HTTP {e.code}: {body[:200]}')
        return e.code < 500  # treat 4xx (except 429) as usable
    except Exception as e:
        log(f'  WARN: Key verify error: {e}')
        return False

def print_stats_table(stats: dict, keys: list):
    log('\n=== Cerebras Key Statistics ===')
    log(f'{"Key ID":<14} {"Suffix":<14} {"Requests":>9} {"RateLimits":>11} {"Failures":>9} {"Last Used":<22}')
    log('-' * 80)
    for key in keys:
        kid = key_id(key)
        st = stats.get(kid, {})
        last = st.get('last_used', 0)
        last_str = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(last)) if last else 'never'
        log(f'{kid:<14} ...{key[-8:]:<11} {st.get("request_count",0):>9} '
            f'{st.get("rate_limit_events",0):>11} {st.get("failed_requests",0):>9} {last_str:<22}')
    log('=' * 80)

# ── main ──────────────────────────────────────────────────────────────────

def main():
    log('=== Cerebras Key Rotator starting ===')
    run_id = os.environ.get('GITHUB_RUN_ID', 'local')
    log(f'Run ID: {run_id}')

    keys = collect_keys()
    if not keys:
        log('ERROR: No Cerebras API keys found in environment')
        print('ROTATOR_ERROR=no_keys')
        sys.exit(1)

    log(f'Found {len(keys)} unique key(s): ' + ' '.join(mask(k) for k in keys))

    # Load stats — prefer Supabase (cross-run persistence) then local file
    stats = load_stats_from_supabase()
    if not stats:
        stats = load_stats()

    # Initialise stats entries for new keys
    for key in keys:
        kid = key_id(key)
        if kid not in stats:
            stats[kid] = {
                'key_suffix': key[-8:],
                'request_count': 0,
                'rate_limit_events': 0,
                'failed_requests': 0,
                'consecutive_failures': 0,
                'last_used': 0,
                'last_rate_limited_at': 0
            }

    # Select key
    selected_key, selected_kid = select_key(keys, stats)
    log(f'Selected: {selected_kid} ({mask(selected_key)})')

    # Optional: verify key is live (skip if --no-verify passed)
    if '--no-verify' not in sys.argv:
        log('Verifying selected key with a probe request...')
        ok = verify_selected_key(selected_key)
        if not ok:
            log(f'Selected key {selected_kid} failed verification — marking as rate-limited and retrying')
            stats[selected_kid]['rate_limited_at'] = time.time()
            stats[selected_kid]['rate_limit_events'] = stats[selected_kid].get('rate_limit_events', 0) + 1
            save_stats(stats)
            # Retry with next best key
            selected_key, selected_kid = select_key(keys, stats)
            log(f'Fallback selected: {selected_kid} ({mask(selected_key)})')

    # Update stats
    now = time.time()
    stats[selected_kid]['last_used'] = now
    stats[selected_kid]['request_count'] = stats[selected_kid].get('request_count', 0) + 1
    stats[selected_kid]['consecutive_failures'] = 0  # reset on successful selection
    stats[selected_kid]['last_run_id'] = run_id

    save_stats(stats)
    sync_stats_to_supabase(stats)

    # Patch openclaw.json
    patch_config(keys, selected_key)

    # Print stats table
    print_stats_table(stats, keys)

    # Output for shell capture
    print(f'ROTATOR_SELECTED_KEY={selected_key}')
    print(f'ROTATOR_SELECTED_KID={selected_kid}')
    print(f'ROTATOR_KEY_COUNT={len(keys)}')
    log(f'=== Rotator complete — primary key: {selected_kid} ===')

if __name__ == '__main__':
    if '--stats' in sys.argv:
        keys = collect_keys()
        stats = load_stats()
        print_stats_table(stats, keys)
    else:
        main()
