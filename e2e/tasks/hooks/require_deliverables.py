#!/usr/bin/env python3
"""Claude Code Stop hook: do not let the agent end while the deliverables are missing.

/goal's evaluator only reads the conversation, so an agent that claims success without producing the files can
pass it (seen 2026-10-26: after a compaction the agent wrote "all conditions met" with an empty results dir).
This hook checks the files themselves. It blocks at most MAX_BLOCKS times per session, then lets the turn end
so a stuck run still finishes; Claude Code's own cap (8 consecutive blocks without a tool call) also applies.

Env: M2T_RESULTS_DIR (required files live there), M2T_MAX_STOP_BLOCKS (default 12).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REQUIRED = {"whitebox.mp4": 10_000, "compare.mp4": 10_000, "compare.jpg": 1_000, "README.md": 50}


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        event = {}
    results = Path(os.environ.get("M2T_RESULTS_DIR", ""))
    missing = [f"{name} (needs >= {size} bytes)" for name, size in REQUIRED.items()
               if not (results / name).is_file() or (results / name).stat().st_size < size]
    if not missing or not str(results):
        return 0
    counter = Path(tempfile.gettempdir()) / f"stop_blocks_{event.get('session_id', 'x')}"
    blocks = int(counter.read_text()) if counter.is_file() else 0
    if blocks >= int(os.environ.get("M2T_MAX_STOP_BLOCKS", "12")):
        return 0
    counter.write_text(str(blocks + 1))
    print(json.dumps({"decision": "block", "reason": (
        f"The deliverables are not on disk yet. Missing in {results}: {', '.join(missing)}. "
        "Statements in the conversation do not count: produce these files (render the Three.js scene, encode "
        "with ffmpeg, write the README), check them with ls and ffprobe, then finish.")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
