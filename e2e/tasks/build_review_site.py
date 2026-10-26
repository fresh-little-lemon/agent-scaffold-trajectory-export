#!/usr/bin/env python3
"""Export a small review site (data.json + media) for a run_tasks.py output, after postprocess_run.py.

For each sample item: the side-by-side compare video (re-encoded small), the still, metrics, and a few calls
shown three ways, linked one to one:
  wire request  -> the request the proxy captured (system/tools summarized, new messages in full)
  wire response -> the response, SSE folded into the non-streaming shape
  training      -> the ms-swift row containing that response, with the trained span marked
plus run-level stats and an error summary.

    python3 e2e/tasks/build_review_site.py --model-dir <...>/claude_code/<model> --ids A B C --out /tmp/site
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from audit_calls_vs_trace import messages_key  # noqa: E402

TEXT_LIMIT = 2500


def clip_text(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… [{len(text) - limit} more characters]"


def readable_block(block: dict) -> dict:
    kind = block.get("type")
    if kind == "text":
        return {"type": "text", "text": clip_text(block.get("text", ""))}
    if kind == "thinking":
        return {"type": "thinking", "text": clip_text(block.get("thinking", ""))}
    if kind == "tool_use":
        return {"type": "tool_use", "name": block.get("name"), "id": block.get("id"),
                "input": clip_text(json.dumps(block.get("input"), ensure_ascii=False, indent=1))}
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content = "\n".join(b.get("text", f"[{b.get('type')}]") for b in content)
        return {"type": "tool_result", "id": block.get("tool_use_id"), "is_error": bool(block.get("is_error")),
                "text": clip_text(str(content))}
    if kind in ("image", "document"):
        return {"type": kind, "text": f"[{kind}: {(block.get('source') or {}).get('media_type', '')}]"}
    return {"type": str(kind), "text": clip_text(json.dumps(block, ensure_ascii=False))}


def readable_message(message: dict) -> dict:
    content = message.get("content")
    blocks = [{"type": "text", "text": clip_text(content)}] if isinstance(content, str) else \
        [readable_block(b) for b in content or []]
    return {"role": message.get("role"), "blocks": blocks}


def readable_request(body: dict, prev_len: int) -> dict:
    system = body.get("system") or []
    messages = body.get("messages") or []
    return {
        "model": body.get("model"),
        "params": {k: body[k] for k in ("max_tokens", "thinking", "output_config", "context_management", "stream")
                   if k in body},
        "system": [{"chars": len(b.get("text", "")), "head": clip_text(b.get("text", ""), 600)} for b in system],
        "tools": [t.get("name") for t in body.get("tools") or []],
        "message_count": len(messages),
        "earlier_messages": prev_len,
        "new_messages": [readable_message(m) for m in messages[prev_len:]],
    }


def transcode(src: Path, dst: Path, image: str) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{src.parent}:/i:ro",
                        "-v", f"{dst.parent}:/o", "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "ffmpeg", image, "-nostdin",
                        "-loglevel", "error", "-y", "-i", f"/i/{src.name}", "-vf", "scale=-2:360", "-c:v", "libx264",
                        "-crf", "30", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
                        f"/o/{dst.name}"], capture_output=True, text=True, timeout=600)
    return r.returncode == 0 and dst.is_file()


def pick_calls(calls: list[dict], n: int) -> list[dict]:
    ok = [c for c in calls if c["status"] == "ok" and c.get("response")]
    if len(ok) <= n:
        return ok
    idx = sorted({0, len(ok) // 2, len(ok) - 1} | {round(i * (len(ok) - 1) / (n - 1)) for i in range(n)})
    return [ok[i] for i in idx][:n]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model-dir", type=Path, required=True)
    ap.add_argument("--ids", nargs="+", required=True)
    ap.add_argument("--calls-per-item", type=int, default=3)
    ap.add_argument("--image", default="agent-scaffold-media2threejs:lean")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    md = args.model_dir.resolve()
    out = args.out
    (out / "media").mkdir(parents=True, exist_ok=True)
    index = {json.loads(l)["id"]: json.loads(l) for l in (md / "dataset" / "index.jsonl").read_text().splitlines() if l.strip()}
    stats = json.loads((md / "dataset" / "stats.json").read_text())
    errors = [json.loads(l) for l in (md / "dataset" / "errors.jsonl").read_text().splitlines() if l.strip()]
    items = []
    for iid in args.ids:
        e = index[iid]
        item_root = md / "items" / iid
        results = item_root / "results" / iid
        media = {}
        for name in ("compare.mp4", "whitebox.mp4"):
            if transcode(results / name, out / "media" / f"{iid}_{name}", args.image):
                media[name] = f"media/{iid}_{name}"
        if (results / "compare.jpg").is_file():
            shutil.copy(results / "compare.jpg", out / "media" / f"{iid}_compare.jpg")
            media["compare.jpg"] = f"media/{iid}_compare.jpg"
        readme = (results / "README.md").read_text(errors="replace")[:4000] if (results / "README.md").is_file() else ""
        calls = [json.loads(l) for l in Path(e["calls"]).read_text().splitlines() if l.strip()]
        rows = Path(e["train"]).read_text().splitlines()
        metas = [json.loads(l) for l in Path(e["train"] + ".meta.jsonl").read_text().splitlines()]
        with tempfile.TemporaryDirectory() as tmp:
            recon = Path(tmp) / "recon.jsonl"
            subprocess.run([sys.executable, str(REPO / "rebuild_wire_jsonl.py"), "--trace", e["trace"],
                            "--output", str(recon)], check=True, capture_output=True)
            wire = {}
            for line in recon.open(encoding="utf-8"):
                x = json.loads(line)
                b = x["request"].get("body")
                if isinstance(b, dict) and isinstance(b.get("messages"), list) and "count_tokens" not in x["request"]["target"]:
                    wire.setdefault(messages_key(b["messages"]), x)
        shown = []
        prev_len_by_call = {}
        by_id = {c["call_id"]: c for c in calls}
        for call in pick_calls(calls, args.calls_per_item):
            x = wire.get(messages_key(call["request"]["messages"]))
            base = by_id.get(call.get("base_call_id"))
            prev_len = len(base["request"]["messages"]) + 1 if base else 0
            prev_len = min(prev_len, len(call["request"]["messages"]))
            row_i = next((i for i, m in enumerate(metas) if call["call_id"] in m["call_ids"]), None)
            training = None
            if row_i is not None:
                row = json.loads(rows[row_i])
                k = metas[row_i]["call_ids"].index(call["call_id"])
                groups, current, group_id = [], [], -1
                marked = []
                in_group = False
                for msg in row["messages"]:
                    if msg.get("loss") and not in_group:
                        group_id += 1
                    in_group = bool(msg.get("loss"))
                    marked.append({"role": msg["role"], "loss": bool(msg.get("loss")),
                                   "this_call": bool(msg.get("loss")) and group_id == k,
                                   "content": clip_text(msg["content"] if isinstance(msg["content"], str)
                                                        else json.dumps(msg["content"], ensure_ascii=False), 1200)})
                first = next((i for i, m in enumerate(marked) if m["this_call"]), 0)
                training = {"row_index": row_i, "row_calls": len(metas[row_i]["call_ids"]), "position_in_row": k,
                            "row_messages": len(marked), "system_chars": len(row.get("system", "")),
                            "tools": len(json.loads(row.get("tools") or "[]")),
                            "window": marked[max(0, first - 6): first + 8], "window_start": max(0, first - 6)}
            shown.append({
                "call_id": call["call_id"], "line": call["line"], "query_source": call["query_source"],
                "stop_reason": call["response"].get("stop_reason"), "usage": call["response"].get("usage"),
                "wire_sequence": x.get("sequence") if x else None,
                "wire_capture": (x or {}).get("capture", {}).get("capture_status"),
                "wire_duration_s": round((x or {}).get("capture", {}).get("duration_s") or 0, 1),
                "request": readable_request(x["request"]["body"] if x else call["request"], prev_len),
                "response": [readable_block(b) for b in ((x or {}).get("response", {}).get("body") or call["response"]).get("content", [])],
                "training": training,
            })
        items.append({"id": iid, "title": e["title"], "url": e.get("url"), "video": e["video"],
                      "clip_start": e["clip_start"], "clip_seconds": e["clip_seconds"], "status": e["status"],
                      "metrics": e.get("metrics"), "media": media, "readme": readme, "calls": shown,
                      "paths": {k: e[k] for k in ("results_dir", "scene_dir", "trace", "calls", "train", "transcripts")}})
    data = {"stats": stats, "errors": {"by_source": dict(Counter(x["source"] for x in errors)), "examples": errors[:40]},
            "items": items}
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"site data: {out / 'data.json'} ({(out / 'data.json').stat().st_size // 1024} KB), media: "
          f"{sum(p.stat().st_size for p in (out / 'media').iterdir()) // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
