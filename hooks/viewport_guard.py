#!/usr/bin/env python3
"""
PreToolUse hook: enforces small browser viewport before screenshots.

Strategy:
- On screenshot tools: check state file. If browser was resized to <=1280x960
  within the last 5 minutes, allow. Otherwise block with instruction.
- On resize tools: inspect tool_input width/height. If <=1280x960, write
  timestamp to state file so subsequent screenshots pass.

Output contract (Claude Code PreToolUse hook):
- Exit 0 + empty stdout  -> allow
- Exit 0 + JSON {"decision": "block", "reason": "..."} -> block with reason
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

STATE_FILE = Path(__file__).parent / ".viewport_state.json"
LOG_FILE = Path(__file__).parent / "viewport_guard.log"
MAX_W, MAX_H = 1280, 960
TTL_SECONDS = 300  # 5 minutes

SCREENSHOT_TOOL_PATTERNS = (
    "take_screenshot",
    "screenshot",
    "gif_creator",  # also produces images
)
RESIZE_TOOL_PATTERNS = (
    "resize_window",
    "resize_page",
    "resize",
)


def log(msg: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception as e:
        log(f"save_state failed: {e}")


def is_screenshot_tool(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in SCREENSHOT_TOOL_PATTERNS)


def is_resize_tool(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in RESIZE_TOOL_PATTERNS)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"bad stdin: {e}")
        return 0  # fail-open

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    # Resize tool: record timestamp if dimensions are within limits
    if is_resize_tool(tool_name):
        w = tool_input.get("width") or tool_input.get("w") or 0
        h = tool_input.get("height") or tool_input.get("h") or 0
        try:
            w, h = int(w), int(h)
        except (TypeError, ValueError):
            w, h = 0, 0
        if 0 < w <= MAX_W and 0 < h <= MAX_H:
            state = load_state()
            state["last_small_resize"] = time.time()
            state["last_dims"] = [w, h]
            save_state(state)
            log(f"viewport marked small: {w}x{h} via {tool_name}")
        else:
            log(f"resize {tool_name} ignored (dims {w}x{h} not within {MAX_W}x{MAX_H})")
        return 0

    # Screenshot tool: check state
    if is_screenshot_tool(tool_name):
        state = load_state()
        last = state.get("last_small_resize", 0)
        age = time.time() - last
        if last and age <= TTL_SECONDS:
            log(f"screenshot allowed: {tool_name} (viewport set {age:.0f}s ago)")
            return 0
        # Block
        reason = (
            f"Screenshot blockiert: Browser-Viewport wurde nicht innerhalb der letzten "
            f"{TTL_SECONDS}s auf <= {MAX_W}x{MAX_H} gesetzt. "
            f"Rufe ZUERST resize_window/resize_page mit width=1280, height=960 auf, "
            f"DANACH den Screenshot erneut. (Schutz gegen 'Could not process image' API-Fehler.)"
        )
        log(f"screenshot BLOCKED: {tool_name} (age={age:.0f}s, last={last})")
        print(json.dumps({"decision": "block", "reason": reason}))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
