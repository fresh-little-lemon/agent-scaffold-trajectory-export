#!/usr/bin/env python3
"""Measure silent gaps in captured agent runs, to calibrate hang detection.

Per item, from the proxy trace: model call duration (request start -> response end), tool gap (response end ->
next request start: the agent working locally), and the time-to-first-byte of each call. Prints pooled quantiles
and, per item, the largest gaps, so a timeout derived from a session's own history can be compared with what
really happens.

    python3 tests/serving_perf/silence_gaps.py 'e2e/3d-code-gen/claude_code/*/items/*/traces/*.jsonl'
"""
import glob
import json
import statistics
import sys


def quantiles(xs, qs=(0.5, 0.9, 0.95, 0.99, 1.0)):
    xs = sorted(xs)
    return {f"p{int(q * 100)}": round(xs[min(len(xs) - 1, int(q * (len(xs) - 1)))], 1) for q in qs} if xs else {}


def main(pattern):
    durations, gaps, ttfb, ratios = [], [], [], []
    for path in glob.glob(pattern):
        recs = []
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (r.get("timestamp") and r.get("duration_s") is not None and "/v1/messages" in (r.get("request_target") or "")
                    and "count_tokens" not in r["request_target"]):
                start = r.get("started_at") or (r["timestamp"] - r["duration_s"])
                recs.append((start, r["timestamp"], r.get("response_head_at")))
        recs.sort()
        if len(recs) < 5:
            continue
        d = [e - s for s, e, _ in recs]
        g = [max(0.0, recs[i + 1][0] - recs[i][1]) for i in range(len(recs) - 1)]
        durations += d
        gaps += g
        ttfb += [h - s for s, _, h in recs if h]
        # How far the largest gap is above what the session's own history (mean + 4 sd of earlier samples) predicts.
        hist = []
        for x in d + g:
            if len(hist) >= 5:
                m, sd = statistics.mean(hist), statistics.pstdev(hist)
                ratios.append(x / (m + 4 * sd))
            hist.append(x)
    print("model call duration s:", quantiles(durations), "n", len(durations))
    print("tool gap s (agent working between calls):", quantiles(gaps), "n", len(gaps))
    print("time to response head s:", quantiles(ttfb))
    print("sample / (running mean + 4 sd):", quantiles(ratios), " share > 1:",
          round(sum(r > 1 for r in ratios) / max(1, len(ratios)), 4))


if __name__ == "__main__":
    main(sys.argv[1])
