#!/usr/bin/env python3
"""Post-process a run_tasks.py output: training data, audit, metrics, errors and an index back to the sources.

Per item (in items/<id>/):
  train/calls.jsonl            every model call rebuilt from the transcript alone (transcript_to_calls.py)
  train/train.jsonl            ms-swift rows (calls_to_swift.py --merge-chains), images in train/assets/
  train/audit.json             rebuilt calls vs the proxy trace (audit_calls_vs_trace.py)
  metrics.json                 wall time, model time, calls, tokens, capture/HTTP outcomes
Per run (in <model dir>/dataset/):
  index.jsonl                  one line per item: source video, clip, title, deliverables, source dir, trace,
                               calls, train rows, metrics -> every artifact maps back to its source video
  train.jsonl (+ .meta.jsonl)  all items' rows concatenated, meta gives item id and call ids per row
  stats.json                   averages over successful items
  errors.jsonl                 runner, capture, HTTP and vLLM server errors

    python3 e2e/tasks/postprocess_run.py --model-dir e2e/3d-code-gen/claude_code/deepseek-v4.1-flash \
        [--vllm-container dsv41_flash] [--keep-reconstructed ID ...]
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def run(*args: str) -> str:
    r = subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=REPO)
    if r.returncode:
        raise RuntimeError(f"{args[0]} failed: {r.stderr[-800:]}")
    return r.stdout


def last_records(batch_log: Path) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for line in batch_log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            latest[r["id"]] = r
    return latest


def trace_metrics(reconstructed: Path, errors: list[dict], iid: str) -> dict:
    m: Counter = Counter()
    model_time = 0.0
    for line in reconstructed.open(encoding="utf-8"):
        x = json.loads(line)
        target = x["request"].get("target") or ""
        if "/v1/messages" not in target:
            continue
        kind = "count_tokens" if "count_tokens" in target else "messages"
        m[f"{kind}_requests"] += 1
        status = x["response"].get("status")
        cap = x["capture"]
        stream = x["response"].get("stream") or {}
        if kind == "messages":
            model_time += float(cap.get("duration_s") or 0)
        m[f"capture_{cap.get('capture_status')}"] += 1
        if status and int(status) >= 400:
            m[f"http_{status}"] += 1
            body = x["response"].get("body")
            errors.append({"id": iid, "source": "http", "status": status, "sequence": x.get("sequence"),
                           "error": json.dumps(body, ensure_ascii=False)[:500]})
        elif stream and stream.get("termination") not in (None, "message_stop"):
            m[f"stream_{stream.get('termination')}"] += 1
            errors.append({"id": iid, "source": "stream", "sequence": x.get("sequence"),
                           "termination": stream.get("termination"), "errors": stream.get("errors"),
                           "capture_status": cap.get("capture_status")})
    return {**dict(m), "model_time_s": round(model_time, 1)}


def call_metrics(calls_path: Path) -> dict:
    usage: Counter = Counter()
    status: Counter = Counter()
    sources: Counter = Counter()
    for line in calls_path.open(encoding="utf-8"):
        c = json.loads(line)
        status[c["status"]] += 1
        sources[str(c.get("query_source"))] += 1
        for k, v in ((c.get("response") or {}).get("usage") or {}).items():
            if isinstance(v, (int, float)):
                usage[k] += v
    return {"calls": sum(status.values()), "call_status": dict(status), "query_sources": dict(sources),
            "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model-dir", type=Path, required=True)
    ap.add_argument("--vllm-container", help="docker container of the model server, to collect its errors")
    ap.add_argument("--keep-reconstructed", nargs="*", default=[], help="item ids whose readable trace to keep")
    ap.add_argument("--only", nargs="*", help="only these item ids")
    args = ap.parse_args()
    md = args.model_dir.resolve()
    records = last_records(md / "batch_log.jsonl")
    out = md / "dataset"
    out.mkdir(exist_ok=True)
    errors: list[dict] = []
    index = []
    for iid, rec in sorted(records.items()):
        if args.only and iid not in args.only:
            continue
        item_root, ws = md / "items" / iid, md / "workspace" / iid
        if rec.get("status") != "success":
            errors.append({"id": iid, "source": "runner", "status": rec.get("status"),
                           "stopped_by": rec.get("stopped_by"), "exit_code": rec.get("exit_code"),
                           "claude_exit": rec.get("claude_exit"), "stderr": rec.get("docker_stderr_tail")})
        train = item_root / "train"
        train.mkdir(exist_ok=True)
        trace = item_root / "traces" / f"{iid}.jsonl"
        recon = item_root / "traces" / f"{iid}.reconstructed.jsonl"
        entry = {"id": iid, "status": rec.get("status"), "title": rec.get("title"), "video": rec.get("video"),
                 "url": rec.get("url"), "clip_start": rec.get("clip_start"), "clip_seconds": rec.get("clip_seconds"),
                 "clip": str(ws / "project" / "input" / f"{iid}.mp4"),
                 "results_dir": str(item_root / "results" / iid), "scene_dir": str(ws / "project" / "scene"),
                 "transcripts": str(ws / ".claude" / "projects"), "trace": str(trace),
                 "calls": str(train / "calls.jsonl"), "train": str(train / "train.jsonl")}
        try:
            run("transcript_to_calls.py", "--workspace", str(ws), "--out", str(train / "calls.jsonl"))
            conv = json.loads(run("calls_to_swift.py", str(train / "calls.jsonl"), str(train / "train.jsonl"),
                                  "--assets", str(train / "assets"), "--merge-chains").strip().splitlines()[-1])
            metrics = {"elapsed_s": rec.get("elapsed_s"), **call_metrics(train / "calls.jsonl"),
                       "train_rows": conv.get("rows", 0), "trained_responses": conv.get("trained_responses", 0)}
            if trace.is_file():
                run("rebuild_wire_jsonl.py", "--trace", str(trace), "--output", str(recon))
                audit = json.loads(run("audit_calls_vs_trace.py", "--calls", str(train / "calls.jsonl"),
                                       "--trace", str(recon), "--out", str(train / "audit.json")))
                metrics.update(trace_metrics(recon, errors, iid))
                c = audit["counts"]
                metrics["audit"] = {"calls": audit["calls"], "matched": c.get("matched", 0),
                                    "messages_exact": c.get("messages_exact_incl_cache_control:ok", 0),
                                    "system_exact": c.get("system_exact:ok", 0), "tools_exact": c.get("tools_exact:ok", 0),
                                    "response_exact": c.get("response_content_exact:ok", 0),
                                    "response_diff": c.get("response_content_exact:DIFF", 0),
                                    "wire_without_call": audit["wire_requests_without_rebuilt_call"]}
                if iid not in args.keep_reconstructed:
                    recon.unlink()  # regenerable; 10s of MB per item
            entry["metrics"] = metrics
            (item_root / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
        except Exception as exc:
            errors.append({"id": iid, "source": "postprocess", "error": repr(exc)[:800]})
        index.append(entry)
        print(f"{iid}: {entry['status']} {json.dumps(entry.get('metrics', {}).get('audit', {}), ensure_ascii=False)}")

    with (out / "index.jsonl").open("w", encoding="utf-8") as f:
        for e in index:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    with (out / "train.jsonl").open("w", encoding="utf-8") as rows, \
            (out / "train.jsonl.meta.jsonl").open("w", encoding="utf-8") as metas:
        n = 0
        for e in index:
            t = Path(e["train"])
            if e["status"] != "success" or not t.is_file():
                continue
            for row, meta in zip(t.read_text(encoding="utf-8").splitlines(),
                                 Path(str(t) + ".meta.jsonl").read_text(encoding="utf-8").splitlines()):
                rows.write(row + "\n")
                metas.write(json.dumps({"row_index": n, "item_id": e["id"], **json.loads(meta)},
                                       ensure_ascii=False) + "\n")
                n += 1
    if args.vllm_container:
        logs = subprocess.run(["docker", "logs", "--since", "24h", args.vllm_container],
                              capture_output=True, text=True, errors="replace")
        # One record per error event: a traceback is many ERROR lines with the same timestamp; keep its
        # first line and its exception message, and count repeats of the same message.
        events: dict[str, dict] = {}
        current = None
        for line in (logs.stdout + logs.stderr).splitlines():
            if " ERROR " not in line and "Traceback" not in line:
                current = None
                continue
            stamp = line.split(" ERROR ")[0][-24:]
            if current is None or current["stamp"] != stamp:
                current = {"stamp": stamp, "first": line[:300], "message": ""}
            text = line.split("] ", 1)[-1].strip()
            if text and ("Error" in text or "Exception" in text) and not text.startswith("File "):
                current["message"] = text[:400]
                key = current["message"] or current["first"]
                rec = events.setdefault(key, {"source": "vllm", "message": key, "count": 0, "first_at": stamp})
                rec["count"] += 1
                rec["last_at"] = stamp
        errors.extend(events.values())
    with (out / "errors.jsonl").open("w", encoding="utf-8") as f:
        for e in errors:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    ok = [e["metrics"] for e in index if e["status"] == "success" and "metrics" in e]
    def mean(key):
        vals = [m[key] for m in ok if isinstance(m.get(key), (int, float))]
        return round(statistics.mean(vals), 1) if vals else None
    stats = {"items": len(index), "status": dict(Counter(e["status"] for e in index)),
             "successful_items": len(ok), "train_rows": n,
             "mean_per_successful_item": {k: mean(k) for k in (
                 "elapsed_s", "model_time_s", "calls", "messages_requests", "input_tokens",
                 "output_tokens", "cache_read_input_tokens", "train_rows", "trained_responses")},
             "errors_by_source": dict(Counter(e["source"] for e in errors))}
    (out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
