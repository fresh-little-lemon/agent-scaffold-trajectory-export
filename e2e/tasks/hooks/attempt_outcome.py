#!/usr/bin/env python3
"""Decide what to do after one `claude -p` attempt ends: done, retry (resume the session), or a real failure.

Reads the attempt's events from the stream-json log (from byte offset START to the end), checks the deliverables,
appends one record to OUTCOMES and prints the decision word on stdout:
  success       all deliverables present (how much to refine beyond that is the agent's call)
  fail_fatal    an error Claude Code itself cannot get past (auth, 4xx request errors, goal cleared after an
                unrecoverable error)
  fail_stalled  this attempt and the one before ended without an error and made (almost) no tool calls
  resume        anything else, e.g. the turn ended early or a transient API error: continue the same session

    attempt_outcome.py STREAM START RESULTS_DIR OUTCOMES ATTEMPT EXIT_CODE
"""
import json
import sys
from pathlib import Path

REQUIRED = {"whitebox.mp4": 10_000, "compare.mp4": 10_000, "compare.jpg": 1_000, "README.md": 50}
FATAL_HTTP = {400, 401, 403, 404, 413}
MIN_TOOL_CALLS = 3


def main() -> int:
    stream, start, results, outcomes, attempt, rc = sys.argv[1:7]
    data = Path(stream).read_bytes()[int(start):] if Path(stream).is_file() else b""
    events = []
    for line in data.splitlines():
        if line.startswith(b"{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    tool_calls = sum(1 for e in events if e.get("type") == "assistant"
                     for b in (e.get("message") or {}).get("content") or [] if b.get("type") == "tool_use")
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    text = str(result.get("result") or "")
    status = result.get("api_error_status")
    results = Path(results)
    deliverables = all((results / n).is_file() and (results / n).stat().st_size >= s for n, s in REQUIRED.items())
    previous = [json.loads(l) for l in Path(outcomes).read_text().splitlines()] if Path(outcomes).is_file() else []

    if deliverables:
        decision = "success"
    elif (result.get("is_error") and status in FATAL_HTTP) or "authentication" in text.lower() \
            or "unrecoverable error" in text.lower():
        decision = "fail_fatal"
    elif (tool_calls < MIN_TOOL_CALLS and not result.get("is_error") and previous
          and previous[-1].get("tool_calls", 99) < MIN_TOOL_CALLS and not previous[-1].get("is_error")):
        # Stalled = attempts end cleanly without doing anything. Repeated API errors are not a stall: they are
        # resumed with backoff until max_resumes (then gave_up).
        decision = "fail_stalled"
    else:
        decision = "resume"
    record = {"attempt": int(attempt), "exit_code": int(rc), "decision": decision, "tool_calls": tool_calls,
              "is_error": result.get("is_error"), "terminal_reason": result.get("terminal_reason"),
              "api_error_status": status, "result": text[:400], "num_turns": result.get("num_turns"),
              "duration_ms": result.get("duration_ms"), "deliverables": deliverables}
    with open(outcomes, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
