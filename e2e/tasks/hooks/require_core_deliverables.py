#!/usr/bin/env python3
"""Stop hook for the model-facing white-model loop.

The model must leave a runnable scene and a white-model video. Comparison
videos, README formatting, and other audit packaging are post-processing.
"""
import json
import os
import sys
from pathlib import Path


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        event = {}
    results = Path(os.environ.get("M2T_RESULTS_DIR", ""))
    scene = Path(os.environ.get("M2T_SCENE_DIR", "/home/node/project/scene"))
    missing = []
    video = results / "whitebox.mp4"
    if not video.is_file() or video.stat().st_size < 10_000:
        missing.append("whitebox.mp4 (needs >= 10000 bytes)")
    source_files = list(scene.glob("*.ts")) + list(scene.glob("*.js")) + list(scene.glob("*.mjs"))
    if not source_files:
        missing.append("scene source (*.ts/*.js/*.mjs)")
    if not missing:
        return 0
    # Keep the same bounded Stop-hook behavior as the original hook.
    counter = Path("/tmp") / f"stop_blocks_core_{event.get('session_id', 'x')}"
    blocks = int(counter.read_text()) if counter.is_file() else 0
    if blocks >= int(os.environ.get("M2T_MAX_STOP_BLOCKS", "12")):
        return 0
    counter.write_text(str(blocks + 1))
    print(json.dumps({"decision": "block", "reason": (
        f"Core model-facing artifacts are missing: {', '.join(missing)}. "
        "Build and inspect the Three.js scene, render whitebox.mp4, and finish the applicable skill state. "
        "Comparison video/JPEG/README are post-processing and are not required in this session.")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
