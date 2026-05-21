#!/usr/bin/env python3
"""
send_startup_notification.py — DISABLED
========================================
Startup notifications have been removed to prevent Telegram spam.

Previously this sent "🟢 Zypher online | Run #xxx" to Telegram on every gateway
start (every 3.5 hours). This flooded the user's chat with noise.

The OpenClaw gateway communicates directly with Telegram via its own channel
system. No wrapper script notifications are needed.

Agent tasks complete, partial, and progress messages are all handled natively
by the OpenClaw streaming.mode:block delivery system.
"""
import sys
# Startup notification permanently disabled — silent exit
sys.exit(0)
