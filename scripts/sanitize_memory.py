#!/usr/bin/env python3
"""
sanitize_memory.py — Hard-cap all memory/context files before Hermes starts.

WHY THIS EXISTS (May 2026):
  Hermes's memory and skills files accumulate across sessions. The agent can
  edit SOUL.md, create new skill files, and add memory entries. Without size
  caps at start-up, the restored files can grow to hundreds of KB and cause
  Groq 413 errors even on the very first user message of a fresh session.

  The proxy (groq_key_proxy.py) truncates at the HTTP level, but if Hermes's
  internal token-counter fires BEFORE sending the request, it can enter its own
  compression loop without ever reaching the proxy. Sanitizing the source files
  prevents that internal loop from ever triggering.

LIMITS (conservative — designed for Groq free-tier TPM/request budgets):
  SOUL.md      : 1 200 chars  (~300 tokens)
  Each skill   :   800 chars  (~200 tokens)
  MEMORY.md    : 1 000 chars  (~250 tokens)
  USER.md      :   600 chars  (~150 tokens)
  Max skills   : 5 files      (~1 000 tokens total)

  Text memory budget is kept under ~1.6K tokens so Hermes built-in instructions,
  tool schemas, recent messages, and output budget can fit below low Groq TPM caps.
"""
import os
import sys

HERMES_HOME  = os.path.expanduser("~/.hermes")
SOUL_MD      = os.path.join(HERMES_HOME, "SOUL.md")
MEMORIES_DIR = os.path.join(HERMES_HOME, "memories")
SKILLS_DIR   = os.path.join(HERMES_HOME, "skills")
MEMORY_MD    = os.path.join(MEMORIES_DIR, "MEMORY.md")
USER_MD      = os.path.join(MEMORIES_DIR, "USER.md")

SOUL_LIMIT   = 1_200
SKILL_LIMIT  = 800
MAX_SKILLS   = 5
MEMORY_LIMIT = 1_000
USER_LIMIT   = 600

NOTICE = "\n\n[…content truncated by sanitize_memory.py to prevent Groq 413]"

def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return None

def _write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _truncate(path, limit, label):
    content = _read(path)
    if content is None:
        print(f"  {label}: not found — skipping")
        return
    orig_len = len(content)
    if orig_len <= limit:
        print(f"  {label}: {orig_len} chars — OK (under {limit})")
        return
    trimmed = content[:limit] + NOTICE
    _write(path, trimmed)
    print(f"  {label}: TRUNCATED {orig_len} → {limit} chars (saved {orig_len - limit} chars)")


def sanitize():
    print("=" * 60)
    print("sanitize_memory.py — context size pre-flight check")
    print("=" * 60)

    # ── SOUL.md ────────────────────────────────────────────────────
    print("\n[SOUL.md]")
    _truncate(SOUL_MD, SOUL_LIMIT, "SOUL.md")

    # ── MEMORY.md ──────────────────────────────────────────────────
    print("\n[MEMORY.md]")
    _truncate(MEMORY_MD, MEMORY_LIMIT, "MEMORY.md")

    # ── USER.md ────────────────────────────────────────────────────
    print("\n[USER.md]")
    _truncate(USER_MD, USER_LIMIT, "USER.md")

    # ── Skills ─────────────────────────────────────────────────────
    print(f"\n[Skills — max {MAX_SKILLS} files, {SKILL_LIMIT} chars each]")
    if not os.path.isdir(SKILLS_DIR):
        print("  Skills dir not found — skipping")
    else:
        skill_files = sorted(
            f for f in os.listdir(SKILLS_DIR)
            if os.path.isfile(os.path.join(SKILLS_DIR, f))
        )
        total_skill_chars = 0

        if len(skill_files) > MAX_SKILLS:
            # Remove the smallest (least content) to stay under MAX_SKILLS
            sized = [(f, os.path.getsize(os.path.join(SKILLS_DIR, f))) for f in skill_files]
            sized.sort(key=lambda x: x[1])          # sort ascending by size
            to_remove = sized[:len(skill_files) - MAX_SKILLS]
            for fname, fsize in to_remove:
                os.remove(os.path.join(SKILLS_DIR, fname))
                print(f"  REMOVED {fname} ({fsize}b) — too many skill files (limit={MAX_SKILLS})")
            skill_files = sorted(
                f for f in os.listdir(SKILLS_DIR)
                if os.path.isfile(os.path.join(SKILLS_DIR, f))
            )

        for fname in skill_files:
            fpath = os.path.join(SKILLS_DIR, fname)
            _truncate(fpath, SKILL_LIMIT, f"skills/{fname}")
            content = _read(fpath)
            if content:
                total_skill_chars += len(content)

        print(f"  Skills total: {len(skill_files)} files, {total_skill_chars} chars")

    # ── Summary ────────────────────────────────────────────────────
    print("\n[Context size estimate]")
    soul_size   = len(_read(SOUL_MD)   or "")
    mem_size    = len(_read(MEMORY_MD) or "")
    user_size   = len(_read(USER_MD)   or "")
    skill_total = sum(
        len(_read(os.path.join(SKILLS_DIR, f)) or "")
        for f in os.listdir(SKILLS_DIR)
        if os.path.isfile(os.path.join(SKILLS_DIR, f))
    ) if os.path.isdir(SKILLS_DIR) else 0

    total_chars  = soul_size + mem_size + user_size + skill_total
    total_tokens = total_chars // 4   # rough estimate
    print(f"  SOUL.md       : {soul_size:>6} chars")
    print(f"  MEMORY.md     : {mem_size:>6} chars")
    print(f"  USER.md       : {user_size:>6} chars")
    print(f"  Skills total  : {skill_total:>6} chars")
    print(f"  ─────────────────────────")
    print(f"  TOTAL         : {total_chars:>6} chars  ≈ {total_tokens} tokens")
    print(f"  Budget used   : {total_tokens}/6000 free-tier TPM tokens ({total_tokens/6000*100:.1f}%)")
    if total_chars > 6_000:
        print("  WARNING: memory/system files exceed 6K chars — may still cause Groq TPM 413")
        sys.exit(1)
    else:
        print("  OK: system prompt within safe limits")
    print("=" * 60)


if __name__ == "__main__":
    sanitize()
