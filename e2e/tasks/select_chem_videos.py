#!/usr/bin/env python3
"""Pick chemistry-experiment videos and write a run_tasks.py items file.

Sources: the dataset's index.jsonl, and optionally every raw/<platform>/<id>/<id>.info.json (yt-dlp metadata)
so videos not yet in the index are included. Each item gets a clip window (default: 10 s starting at 35% of
the video, past title cards) that the container cuts from the original.

    python3 e2e/tasks/select_chem_videos.py --ids BV1QW4y1v7iU,... --out e2e/tasks/chem_test10.jsonl
    python3 e2e/tasks/select_chem_videos.py --scan-raw --keyword 实验 --max-duration 300 --limit 300 \
        --exclude e2e/tasks/chem_test10.jsonl --out e2e/tasks/chem_scale.jsonl
"""
import argparse
import json
from pathlib import Path

DATASET = Path("/slow_share2/chenmf/lwm/datasets/chem_video_v1")


def from_index(root: Path):
    for line in (root / "index.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            yield {"id": r["id"], "video": str(root / r["path"]), "title": r.get("title", ""),
                   "duration_s": r.get("duration_seconds"), "width": r.get("width"), "height": r.get("height"),
                   "platform": r.get("platform"), "url": r.get("url")}


def from_raw(root: Path):
    for info in sorted((root / "raw").glob("*/*/*.info.json")):
        mp4 = info.with_name(info.name.replace(".info.json", ".mp4"))
        if not mp4.is_file():
            continue
        try:
            r = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        yield {"id": mp4.stem, "video": str(mp4), "title": r.get("title") or r.get("fulltitle") or "",
               "duration_s": r.get("duration"), "width": r.get("width"), "height": r.get("height"),
               "platform": info.parent.parent.name, "url": r.get("webpage_url")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DATASET)
    ap.add_argument("--ids", help="comma-separated ids, in this order")
    ap.add_argument("--scan-raw", action="store_true")
    ap.add_argument("--keyword", action="append", default=[])
    ap.add_argument("--min-duration", type=float, default=20)
    ap.add_argument("--max-duration", type=float, default=300)
    ap.add_argument("--clip-seconds", type=float, default=10)
    ap.add_argument("--clip-at", type=float, default=0.35, help="clip start as a fraction of the duration")
    ap.add_argument("--exclude", type=Path, action="append", default=[], help="items files whose ids to skip")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    pool = {}
    for row in list(from_index(args.root)) + (list(from_raw(args.root)) if args.scan_raw else []):
        pool.setdefault(row["id"], row)
    skip = {json.loads(l)["id"] for f in args.exclude for l in f.read_text(encoding="utf-8").splitlines() if l.strip()}
    if args.ids:
        rows = [pool[i] for i in args.ids.split(",") if i in pool]
    else:
        rows = [r for r in pool.values()
                if r["id"] not in skip and r.get("duration_s")
                and args.min_duration <= float(r["duration_s"]) <= args.max_duration
                and all(k in r["title"] for k in args.keyword)]
        rows.sort(key=lambda r: (float(r["duration_s"]), r["id"]))
    rows = rows[:args.limit] if args.limit else rows
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as out:
        for r in rows:
            duration = float(r["duration_s"])
            clip = min(args.clip_seconds, duration)
            start = max(0.0, min(duration * args.clip_at, duration - clip))
            out.write(json.dumps({**r, "clip_start": round(start, 2), "clip_seconds": clip},
                                 ensure_ascii=False) + "\n")
    print(f"{len(rows)} items -> {args.out} (pool {len(pool)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
