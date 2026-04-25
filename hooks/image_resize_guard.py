#!/usr/bin/env python3
"""
PreToolUse hook: shrinks image files in-place BEFORE they are read by Claude.

Targets file-read tools (Read, mcp__Desktop_Commander__read_file).
If the tool_input file_path points to an image > MAX_W x MAX_H or > MAX_BYTES,
the file is resized + recompressed in-place. The read then loads the small file.

Strategy:
1. Resize to fit within MAX_W x MAX_H (preserve aspect ratio).
2. Save as PNG. If still > MAX_BYTES, re-save as JPEG q=85.
3. Backup original ONCE to <name>.orig.<ext> if not already backed up.

Output contract:
- Exit 0 + empty stdout -> allow (tool proceeds, reads the now-small file)
- Exit 0 + JSON {"decision":"block","reason":"..."} -> block (only on hard errors)

Fail-open: any exception -> allow original through.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

LOG_FILE = Path(__file__).parent / "image_resize_guard.log"
MAX_W, MAX_H = 1280, 960
MAX_BYTES = 1_000_000  # 1 MB
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

READ_TOOL_NAMES = {
    "Read",
    "mcp__Desktop_Commander__read_file",
}


def log(msg: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def extract_path(tool_input: dict) -> str | None:
    for key in ("file_path", "path", "filepath", "filename"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def needs_processing(path: Path) -> tuple[bool, str]:
    if not path.exists() or not path.is_file():
        return False, "not a file"
    if path.suffix.lower() not in IMAGE_EXTS:
        return False, "not an image ext"
    size = path.stat().st_size
    if size <= MAX_BYTES:
        # Still check dimensions
        try:
            from PIL import Image
            with Image.open(path) as im:
                w, h = im.size
            if w <= MAX_W and h <= MAX_H:
                return False, f"ok ({w}x{h}, {size}B)"
            return True, f"big dims {w}x{h}"
        except Exception as e:
            return False, f"PIL probe failed: {e}"
    return True, f"big size {size}B"


def resize_in_place(path: Path) -> str:
    from PIL import Image

    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        try:
            backup.write_bytes(path.read_bytes())
        except Exception as e:
            log(f"backup failed for {path}: {e}")

    with Image.open(path) as im:
        im.load()
        orig_w, orig_h = im.size
        # Convert palette/alpha modes that JPEG won't accept later
        if im.mode in ("P", "RGBA"):
            im = im.convert("RGB") if im.mode == "P" else im
        im.thumbnail((MAX_W, MAX_H), Image.LANCZOS)
        new_w, new_h = im.size

        # First try PNG (lossless)
        if path.suffix.lower() == ".png":
            im.save(path, format="PNG", optimize=True)
            if path.stat().st_size <= MAX_BYTES:
                return f"resized {orig_w}x{orig_h} -> {new_w}x{new_h} PNG ({path.stat().st_size}B)"
            # Fall through to JPEG
            jpeg_path = path.with_suffix(".jpg")
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.save(jpeg_path, format="JPEG", quality=85, optimize=True)
            # Replace original with JPEG content but keep original filename
            path.write_bytes(jpeg_path.read_bytes())
            jpeg_path.unlink(missing_ok=True)
            return f"resized {orig_w}x{orig_h} -> {new_w}x{new_h} JPEG-as-PNG ({path.stat().st_size}B)"
        else:
            fmt = "JPEG" if path.suffix.lower() in (".jpg", ".jpeg") else "PNG"
            if fmt == "JPEG" and im.mode != "RGB":
                im = im.convert("RGB")
            kwargs = {"quality": 85, "optimize": True} if fmt == "JPEG" else {"optimize": True}
            im.save(path, format=fmt, **kwargs)
            return f"resized {orig_w}x{orig_h} -> {new_w}x{new_h} {fmt} ({path.stat().st_size}B)"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"bad stdin: {e}")
        return 0

    tool_name = payload.get("tool_name", "")
    if tool_name not in READ_TOOL_NAMES:
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    raw_path = extract_path(tool_input)
    if not raw_path:
        return 0

    try:
        path = Path(raw_path)
        do_it, why = needs_processing(path)
        if not do_it:
            log(f"skip {path.name}: {why}")
            return 0
        msg = resize_in_place(path)
        log(f"OK {path.name}: {msg}")
    except Exception as e:
        log(f"resize failed for {raw_path}: {e}")
        # Fail-open: let the tool read original
    return 0


if __name__ == "__main__":
    sys.exit(main())
