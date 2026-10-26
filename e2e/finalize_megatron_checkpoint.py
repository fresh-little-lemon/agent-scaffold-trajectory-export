#!/usr/bin/env python3
"""Finalize a Swift Qwen3.5 checkpoint after its weights have been saved."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("base_model", type=Path)
    args = parser.parse_args()

    config = json.loads((args.base_model / "config.json").read_text(encoding="utf-8"))
    text = config["text_config"]
    text["max_position_embeddings"] = 1_000_000
    text["rope_parameters"] = {
        "rope_type": "yarn",
        "mrope_interleaved": True,
        "mrope_section": [11, 11, 10],
        "rope_theta": 10_000_000,
        "partial_rotary_factor": 0.25,
        "factor": 4.0,
        "original_max_position_embeddings": 262_144,
    }
    (args.checkpoint / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for name in ["chat_template.jinja", "tokenizer.json", "tokenizer_config.json",
                 "merges.txt", "vocab.json", "preprocessor_config.json",
                 "video_preprocessor_config.json"]:
        source = args.base_model / name
        target = args.checkpoint / name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
    print(f"finalized {args.checkpoint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
