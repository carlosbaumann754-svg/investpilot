#!/usr/bin/env python3
"""Diagnostic hook: logs EVERY PreToolUse call to prove hooks fire at all."""
import json
import sys
import time
from pathlib import Path

LOG = Path(__file__).parent / "heartbeat.log"
try:
    payload = json.load(sys.stdin)
    name = payload.get("tool_name", "?")
    inp = payload.get("tool_input", {})
    # Truncate input preview
    preview = json.dumps(inp)[:200]
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {name} | {preview}\n")
except Exception as e:
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERROR {e}\n")
    except Exception:
        pass
sys.exit(0)
