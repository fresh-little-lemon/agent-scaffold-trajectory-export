#!/usr/bin/env python3
"""Replay captured agent traffic against a model server to measure serving throughput.

Each stream takes one captured item trace (e2e/api_proxy.py output) and re-sends its /v1/messages requests in
their original order, back to back: real prompts, real context growth, real prefix-cache reuse and real images,
without running agents. N streams run at once; when a stream finishes a trace it takes the next one.
max_tokens is capped near the captured response length so generation volume matches the real run.

Throughput is read from the server's Prometheus counters (vLLM /metrics) over a measurement window that starts
after a warm-up, so it counts every token the server processed, including prefill and speculative tokens.

    python3 tests/serving_perf/replay_agent_traces.py --traces 'e2e/3d-code-gen/claude_code/*/items/*/traces/*.jsonl' \
        --url http://127.0.0.1:18005 --concurrency 96 --warmup 120 --measure 480 --out result.json
"""
from __future__ import annotations

import argparse
import glob
import itertools
import json
import random
import re
import sys
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rebuild_wire_jsonl import resolve_sidecar  # noqa: E402

COUNTERS = ("generation_tokens_total", "prompt_tokens_total", "prefix_cache_queries_total", "prefix_cache_hits_total",
            "spec_decode_num_accepted_tokens_total", "spec_decode_num_draft_tokens_total", "num_preemptions_total",
            "request_success_total")
OUT_TOKENS = re.compile(rb'"output_tokens":\s*(\d+)')


def load_trace(path: Path) -> list[dict]:
    calls = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        target = r.get("request_target") or ""
        if not target.startswith("/v1/messages") or "count_tokens" in target or r.get("response_status") != 200:
            continue
        body = resolve_sidecar(r.get("request_body_file"), None, path)
        resp = resolve_sidecar(r.get("response_body_file"), None, path)
        if not body or not body.is_file() or not resp or not resp.is_file():
            continue
        found = OUT_TOKENS.findall(resp.read_bytes()[-4000:])
        headers = {k: v for k, v in r.get("request_headers") or []
                   if k.lower() in ("anthropic-version", "anthropic-beta", "content-type")}
        calls.append({"key": (str(r.get("proxy_run_id")), int(r.get("n", 0))), "body": body,
                      "out_tokens": int(found[-1]) if found else 512, "headers": headers})
    return [c for c in sorted(calls, key=lambda c: c["key"])]


def metrics(url: str) -> dict:
    text = requests.get(f"{url}/metrics", timeout=30, proxies={"http": None, "https": None}).text
    out = {}
    for line in text.splitlines():
        if line.startswith("vllm:"):
            name = line.split("{", 1)[0].split(" ", 1)[0][5:]
            if name in COUNTERS or name in ("num_requests_running", "num_requests_waiting", "kv_cache_usage_perc"):
                out[name] = out.get(name, 0.0) + float(line.rsplit(" ", 1)[1])
    return out


class Replay:
    def __init__(self, args, traces):
        self.args = args
        self.traces = itertools.cycle(traces)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.done = 0
        self.errors = 0
        self.session_cap = args.max_out_cap

    def next_trace(self):
        with self.lock:
            return next(self.traces)

    def worker(self):
        s = requests.Session()
        s.trust_env = False
        while not self.stop.is_set():
            for call in self.next_trace():
                if self.stop.is_set():
                    return
                body = json.loads(call["body"].read_bytes())
                body["stream"] = True
                body["max_tokens"] = min(int(body.get("max_tokens") or 32000),
                                         max(64, int(call["out_tokens"] * 1.15) + 64), self.session_cap)
                headers = {"content-type": "application/json", "x-api-key": "EMPTY", **call["headers"]}
                try:
                    with s.post(f"{self.args.url}/v1/messages?beta=true", json=body, headers=headers,
                                stream=True, timeout=1800) as r:
                        for _ in r.iter_content(65536):
                            if self.stop.is_set():
                                return
                        ok = r.status_code == 200
                except requests.RequestException:
                    ok = False
                with self.lock:
                    self.done += ok
                    self.errors += not ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--traces", required=True, help="glob of proxy trace .jsonl files")
    ap.add_argument("--url", default="http://127.0.0.1:18005")
    ap.add_argument("--concurrency", type=int, default=96)
    ap.add_argument("--warmup", type=int, default=120, help="seconds before the measurement window")
    ap.add_argument("--measure", type=int, default=480, help="measurement window, seconds")
    ap.add_argument("--max-out-cap", type=int, default=8192)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--label", default="")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    paths = sorted(Path(p) for p in glob.glob(args.traces))
    random.Random(args.seed).shuffle(paths)
    traces = [t for t in (load_trace(p) for p in paths) if len(t) >= 5]
    print(f"{len(traces)} traces, {sum(len(t) for t in traces)} requests", flush=True)
    rep = Replay(args, traces)
    threads = [threading.Thread(target=rep.worker, daemon=True) for _ in range(args.concurrency)]
    for t in threads:
        t.start()
        time.sleep(0.2)
    time.sleep(args.warmup)
    m0, t0, d0 = metrics(args.url), time.time(), rep.done
    samples = []
    while time.time() - t0 < args.measure:
        time.sleep(10)
        m = metrics(args.url)
        samples.append((m.get("num_requests_running", 0), m.get("kv_cache_usage_perc", 0)))
    m1, t1, d1 = metrics(args.url), time.time(), rep.done
    rep.stop.set()
    dt = t1 - t0
    delta = {k: m1.get(k, 0) - m0.get(k, 0) for k in COUNTERS}
    result = {
        "label": args.label, "concurrency": args.concurrency, "window_s": round(dt),
        "gen_tok_s": round(delta["generation_tokens_total"] / dt, 1),
        "prompt_tok_s": round(delta["prompt_tokens_total"] / dt, 1),
        "prefill_computed_tok_s": round((delta["prefix_cache_queries_total"] - delta["prefix_cache_hits_total"]) / dt, 1),
        "prefix_hit_rate": round(delta["prefix_cache_hits_total"] / max(1, delta["prefix_cache_queries_total"]), 3),
        "requests_per_min": round((d1 - d0) / dt * 60, 1),
        "spec_accept_rate": round(delta["spec_decode_num_accepted_tokens_total"]
                                  / max(1, delta["spec_decode_num_draft_tokens_total"]), 3),
        "preemptions": delta["num_preemptions_total"], "client_errors": rep.errors,
        "mean_running": round(sum(s[0] for s in samples) / max(1, len(samples)), 1),
        "mean_kv_usage": round(sum(s[1] for s in samples) / max(1, len(samples)), 3),
    }
    print(json.dumps(result), flush=True)
    if args.out:
        with args.out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
