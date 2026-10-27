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


def from_shots(args, skip):
    """One clip per video, taken inside a single detected shot (catalog `clips` table: t0/t1 + quality scores).

    A shot qualifies when it is at least --min-shot long and is not mostly black, text (titles, slides),
    faces (lecturer close-ups) or static. Per video the longest qualifying shot is used, trimmed to its middle
    --clip-seconds. Item id = shot id, so every output maps to one shot of one source video.
    """
    import sqlite3
    catalogs = sorted((args.root / "_state" / "backup").glob("catalog-*.sqlite"))
    db = sqlite3.connect(f"file:{catalogs[-1]}?mode=ro", uri=True)
    title_filter = " and ".join(["i.title like ?"] * len(args.keyword)) or "1"
    rows = db.execute(f"""
        select c.clip_id, c.key, c.f0, c.f1, c.scores, c.path, i.title, i.duration, i.width, i.height, i.platform, i.meta,
               c.t0, c.t1
        from clips c join items i on i.key = c.key
        where {title_filter} and i.duration between ? and ?""",
        [f"%{k}%" for k in args.keyword] + [args.min_duration, args.max_duration]).fetchall()
    best: dict[str, tuple] = {}
    # A video can be stored as several partial files (raw/.../<id>.s<offset>.mp4); a clip's f0/f1 are seconds
    # inside its own file c.path, t0/t1 seconds in the whole video. Cut with f0/f1 from c.path.
    for clip_id, key, t0, t1, scores, path, title, duration, width, height, platform, meta, v0, v1 in rows:
        if key in skip:
            continue
        s = json.loads(scores or "{}")
        dur = float(t1) - float(t0)
        if (dur < args.min_shot or s.get("black_frac", 0) > 0.1 or s.get("text_area", 0) > 0.06
                or s.get("face_area_max", 0) > 0.05 or s.get("static_frac", 0) > 0.95 or s.get("motion", 0) < 0.05):
            continue
        if not (args.root / path).is_file():
            continue
        if key not in best or dur > best[key][3] - best[key][2]:
            best[key] = (clip_id, key, float(t0), float(t1), s, path, title, duration, width, height, platform, meta,
                         float(v0), float(v1))
    picked = sorted(best.values(), key=lambda b: (b[1], b[0]))
    if args.limit:
        picked = picked[:args.limit]
    for clip_id, key, t0, t1, s, path, title, duration, width, height, platform, meta, v0, v1 in picked:
        clip = min(args.clip_seconds, t1 - t0)
        start = t0 + (t1 - t0 - clip) / 2
        url = (json.loads(meta or "{}") or {}).get("url")
        yield {"id": clip_id, "video_key": key, "video": str(args.root / path), "title": title,
               "duration_s": duration, "width": width, "height": height, "platform": platform, "url": url,
               "file_t0": t0, "file_t1": t1, "video_t0": v0, "video_t1": v1, "shot_scores": s,
               "clip_start": round(start, 3), "clip_seconds": round(clip, 3)}


def from_videos(args, skip):
    """Whole videos: triage passed (lab content), downloaded as one file covering the full duration.

    Partial downloads (several raw/.../<id>.s<offset>.mp4 files) are skipped: they are not a complete video.
    Shortest first, so results arrive early. Item id = video key; the clip is the whole file.
    """
    import sqlite3
    catalogs = sorted((args.root / "_state" / "backup").glob("catalog-*.sqlite"))
    db = sqlite3.connect(f"file:{catalogs[-1]}?mode=ro", uri=True)
    rows = db.execute("""
        select i.key, i.title, i.duration, i.width, i.height, i.platform, i.meta, s.result, f.path, f.duration
        from items i join stages s on s.key = i.key and s.stage = 'triage' and s.status = 'done'
        join files f on f.key = i.key
        where i.key in (select key from files group by key having count(*) = 1)""").fetchall()
    picked = []
    for key, title, duration, width, height, platform, meta, triage, path, fdur in rows:
        if key in skip or not fdur or not duration or float(fdur) < 0.95 * float(duration):
            continue
        if not (args.min_duration <= float(fdur) <= args.max_duration):
            continue
        tri = json.loads(triage or "{}")
        if not tri.get("pass", True):
            continue
        picked.append({"id": key, "video_key": key, "video": str(args.root / path), "title": title,
                       "duration_s": float(fdur), "width": width, "height": height, "platform": platform,
                       "url": (json.loads(meta or "{}") or {}).get("url"),
                       "lab_segments": tri.get("segments"), "lab_fraction": tri.get("lab_fraction"),
                       "clip_start": 0.0, "clip_seconds": round(float(fdur), 3)})
    picked.sort(key=lambda r: (r["duration_s"], r["id"]))
    return picked[:args.limit] if args.limit else picked


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
    ap.add_argument("--from-shots", action="store_true",
                    help="one shot per video from the dataset catalog's clips table (no scene cut inside a clip)")
    ap.add_argument("--min-shot", type=float, default=4.0, help="shortest usable shot, seconds")
    ap.add_argument("--from-videos", action="store_true",
                    help="whole videos (triage passed, one complete file), shortest first")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    skip = {json.loads(l).get("video_key", json.loads(l)["id"])
            for f in args.exclude for l in f.read_text(encoding="utf-8").splitlines() if l.strip()}
    if args.from_shots or args.from_videos:
        rows = list(from_videos(args, skip) if args.from_videos else from_shots(args, skip))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8", newline="\n") as out:
            for r in rows:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{len(rows)} items -> {args.out}")
        return 0

    pool = {}
    for row in list(from_index(args.root)) + (list(from_raw(args.root)) if args.scan_raw else []):
        pool.setdefault(row["id"], row)
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
