#!/usr/bin/env python3
"""Rebuild every model call Claude Code made, from its saved ``~/.claude`` alone (no API trace).

Claude Code (verified on 2.1.310) writes, next to the user/assistant records of a session transcript,
content-addressed records describing each Messages API request it sends:

* ``api-request-shape``: ``{shapeHash, shape: {system, tools}}``, one per distinct system prompt + tool list.
* ``api-request``: one per call: ``{id, base, keep, tail, shapeHash, params, cacheControl, querySource}``.
  Its messages are ``messages(base)[:keep] + [message(h) for h in tail]``: an incremental chain.
* ``api-request-blob``: ``{hash, message}`` for tail messages that the regular records cannot reproduce.

A message hash is ``sha256(json.dumps(message without cache_control, sort_keys=True, compact))``. Tail
messages without a blob are rebuilt from the transcript (an assistant message = the records sharing one
``message.id``; a user message = one user record or a run of consecutive ones) and accepted only when the
hash matches, so every rebuilt message is exact or reported as unresolved. The response of a call is the
assistant message whose records follow its ``api-request`` record in the same file.

Not recoverable from the transcript, by design of Claude Code: the compaction call (no ``api-request``
record; its ``<analysis>`` is stripped from the stored summary), calls whose response never finished
(in flight when the process died, or HTTP errors), and the per-request attribution header block
(disable it with ``CLAUDE_CODE_ATTRIBUTION_HEADER=0``). See docs/transcript_to_calls.md.

    python3 transcript_to_calls.py --workspace e2e/outputs-X/claude_code/M/workspace/13 --out calls.jsonl
    python3 transcript_to_calls.py --workspace-root e2e/outputs-X/claude_code/M/workspace --out calls.jsonl
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "claude-api-call-v1"
MAX_USER_RUN = 6  # longest run of consecutive user records tried as one API message


def strip_cache(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_cache(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [strip_cache(v) for v in value]
    return value


def message_hash(message: dict[str, Any]) -> str:
    text = json.dumps(strip_cache(message), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[tuple[int, dict[str, Any]]]:
    rows = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"warning: {path}:{line_number}: invalid JSON skipped", file=sys.stderr)
                continue
            if isinstance(record, dict):
                rows.append((line_number, record))
    return rows


def as_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": content}] if content not in (None, "") else []


def candidate_messages(records: list[tuple[int, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Every API message the transcript records could stand for, keyed by message hash."""
    candidates: dict[str, dict[str, Any]] = {}

    def add(message: dict[str, Any]) -> None:
        candidates.setdefault(message_hash(message), message)

    groups: dict[str, list[dict[str, Any]]] = {}
    run: list[dict[str, Any]] = []

    def flush_run() -> None:
        for start in range(len(run)):
            for end in range(start + 1, min(len(run), start + MAX_USER_RUN) + 1):
                window = run[start:end]
                if len(window) == 1:
                    add({"role": "user", "content": window[0]["message"].get("content")})
                add({"role": "user", "content": [b for r in window for b in as_blocks(r["message"].get("content"))]})
        run.clear()

    for _, record in records:
        kind = record.get("type")
        if kind == "assistant" and isinstance(record.get("message"), dict):
            flush_run()
            groups.setdefault(str(record["message"].get("id")), []).append(record)
        elif kind == "user" and isinstance(record.get("message"), dict):
            run.append(record)
    flush_run()
    for group in groups.values():
        add({"role": "assistant", "content": [b for r in group for b in as_blocks(r["message"].get("content"))]})
    return candidates


def assistant_groups(records: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Assistant messages in order of their first record, with the line span they occupy."""
    order: list[str] = []
    groups: dict[str, dict[str, Any]] = {}
    for line, record in records:
        if record.get("type") != "assistant" or not isinstance(record.get("message"), dict):
            continue
        message = record["message"]
        key = str(message.get("id"))
        if key not in groups:
            order.append(key)
            groups[key] = {"id": message.get("id"), "first_line": line, "records": []}
        groups[key]["records"].append(record)
    result = []
    for key in order:
        group = groups[key]
        last = group["records"][-1]["message"]
        result.append({
            "first_line": group["first_line"],
            "message": {
                "id": group["id"],
                "type": "message",
                "role": "assistant",
                "model": last.get("model"),
                "content": [b for r in group["records"] for b in as_blocks(r["message"].get("content"))],
                "stop_reason": last.get("stop_reason"),
                "stop_sequence": last.get("stop_sequence"),
                "usage": last.get("usage"),
            },
            "uuids": [r.get("uuid") for r in group["records"]],
        })
    return result


def apply_cache_control(request: dict[str, Any], entries: Iterable[dict[str, Any]]) -> list[list]:
    """Put cache_control markers back where Claude Code placed them; return paths that did not resolve.

    The wire form of a message depends on its markers: a ``role: system`` message is sent as a plain
    string unless a cache_control sits on it, in which case it is a one-element list of text blocks.
    """
    missing = []
    for entry in entries or []:
        path = entry.get("path") or []
        if (len(path) >= 4 and path[0] == "messages" and path[2] == "content"
                and isinstance(path[1], int) and path[1] < len(request.get("messages") or [])):
            message = request["messages"][path[1]]
            if isinstance(message.get("content"), str):
                message["content"] = [{"type": "text", "text": message["content"]}]
        node: Any = request
        try:
            for step in path:
                node = node[step]
            if isinstance(node, dict):
                node["cache_control"] = entry.get("value")
            else:
                missing.append(path)
        except (KeyError, IndexError, TypeError):
            missing.append(path)
    for message in request.get("messages") or []:
        content = message.get("content")
        if (message.get("role") == "system" and isinstance(content, list) and len(content) == 1
                and content[0].get("type") == "text" and "cache_control" not in content[0]):
            message["content"] = content[0].get("text", "")
    return missing


def session_files(workspace: Path) -> list[Path]:
    """Main session transcripts under a workspace (or .claude dir), each followed by its subagent files."""
    roots = [workspace / ".claude" / "projects", workspace / "projects", workspace]
    projects = next((r for r in roots if r.is_dir() and r.name == "projects"), None)
    if workspace.is_file():
        mains = [workspace]
    elif projects is not None:
        mains = sorted(projects.glob("*/*.jsonl"))
    else:
        mains = sorted(workspace.glob("*.jsonl"))
    files = []
    for main in mains:
        files.append(main)
        side = main.with_suffix("")
        if side.is_dir():
            files.extend(sorted(p for p in side.rglob("*.jsonl")
                                if p.name.startswith("agent-") and "journal" not in p.name))
    return files


def rebuild_session(files: list[Path], base_dir: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    parsed = {path: read_jsonl(path) for path in files}
    shapes: dict[str, dict[str, Any]] = {}
    blobs: dict[str, dict[str, Any]] = {}
    for rows in parsed.values():
        for _, record in rows:
            if record.get("type") == "api-request-shape":
                shapes[record["shapeHash"]] = record.get("shape") or {}
            elif record.get("type") == "api-request-blob":
                blobs[record["hash"]] = record.get("message")

    calls: list[dict[str, Any]] = []
    stats: Counter = Counter()
    gaps: list[dict[str, Any]] = []
    for path, rows in parsed.items():
        source = str(path.relative_to(base_dir)) if base_dir and path.is_relative_to(base_dir) else str(path)
        candidates = candidate_messages(rows)
        groups = assistant_groups(rows)
        request_rows = [(line, r) for line, r in rows if r.get("type") == "api-request"]
        messages_by_call: dict[str, list[dict[str, Any]]] = {}
        for index, (line, record) in enumerate(request_rows):
            unresolved: list[str] = []
            base = record.get("base")
            if base and base not in messages_by_call:
                unresolved.append(f"base:{base}")
                prefix: list[dict[str, Any]] = []
            else:
                prefix = messages_by_call.get(base, [])[: int(record.get("keep") or 0)]
            tail = []
            for digest in record.get("tail") or []:
                message = blobs.get(digest) or candidates.get(digest)
                if message is None:
                    unresolved.append(digest)
                    message = {"role": "unresolved", "content": [], "_hash": digest}
                    stats["unresolved_messages"] += 1
                else:
                    stats["messages_from_blob" if digest in blobs else "messages_from_records"] += 1
                tail.append(strip_cache(message))
            messages = prefix + tail
            messages_by_call[record["id"]] = messages
            unresolved += [m["_hash"] for m in prefix if m.get("role") == "unresolved"]

            shape = shapes.get(record.get("shapeHash"))
            if shape is None:
                unresolved.append(f"shape:{record.get('shapeHash')}")
            params = dict(record.get("params") or {})
            betas = params.pop("betas", None)
            request = {"model": params.pop("model", None),
                       "system": copy.deepcopy((shape or {}).get("system")),
                       "tools": copy.deepcopy((shape or {}).get("tools")),
                       "messages": copy.deepcopy(messages), **params}
            missing_cache = apply_cache_control(request, record.get("cacheControl"))

            next_line = request_rows[index + 1][0] if index + 1 < len(request_rows) else float("inf")
            group = next((g for g in groups if line < g["first_line"] < next_line), None)
            status = "unresolved_messages" if unresolved else ("ok" if group else "no_response")
            stats[status] += 1
            calls.append({
                "schema": SCHEMA,
                "session_id": record.get("sessionId"),
                "source_file": source,
                "line": line,
                "call_id": record.get("id"),
                "base_call_id": base,
                "query_source": record.get("querySource"),
                "timestamp": record.get("timestamp"),
                "claude_code_version": record.get("version"),
                "request_digest": record.get("requestDigest"),
                "betas": betas,
                "request": request,
                "response": group["message"] if group else None,
                "response_transcript_uuids": group["uuids"] if group else [],
                "status": status,
                "unresolved": unresolved,
                "unplaced_cache_control": missing_cache,
            })
        for line, record in rows:
            if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
                gaps.append({"kind": "compaction_call", "source_file": source, "line": line,
                             "trigger": (record.get("compactMetadata") or {}).get("trigger"),
                             "why": "compaction requests are not logged as api-request; the stored summary "
                                    "drops the response's <analysis> block"})
        stats["files"] += 1
    manifest = {"calls": len(calls), "shapes": len(shapes), "blobs": len(blobs),
                "stats": dict(stats), "known_gaps": gaps}
    return calls, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, action="append", default=[],
                        help="item workspace (contains .claude/), a .claude dir, or a session .jsonl; repeatable")
    parser.add_argument("--workspace-root", type=Path,
                        help="directory whose subdirectories are item workspaces")
    parser.add_argument("--out", type=Path, required=True, help="calls JSONL to write")
    args = parser.parse_args()
    workspaces = list(args.workspace)
    if args.workspace_root:
        workspaces += sorted(p for p in args.workspace_root.iterdir() if p.is_dir())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    totals: Counter = Counter()
    report = []
    with args.out.open("w", encoding="utf-8", newline="\n") as out:
        for workspace in workspaces:
            files = session_files(workspace)
            if not files:
                continue
            calls, manifest = rebuild_session(files, base_dir=workspace if workspace.is_dir() else None)
            for call in calls:
                call["workspace"] = str(workspace)
                out.write(json.dumps(call, ensure_ascii=False, separators=(",", ":")) + "\n")
            totals.update(manifest["stats"])
            report.append({"workspace": str(workspace), **manifest})
    manifest_path = args.out.with_suffix(args.out.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps({"totals": dict(totals), "workspaces": report},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(args.out), "manifest": str(manifest_path), **dict(totals)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
