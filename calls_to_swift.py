#!/usr/bin/env python3
"""Turn rebuilt Claude Code calls (transcript_to_calls.py) into ms-swift agent SFT rows.

One row per model call: the exact request of that call as context (system, tools, every message,
including earlier thinking exactly as it was sent) and that call's response as the only trained turn
(``"loss": true``; every earlier assistant/tool_call message has ``"loss": false``). With
``--merge-chains``, consecutive calls of one transcript are folded into a single row when the later
request is exactly the earlier request + its response + new messages and system/tools/params are equal;
then every response in the row is trained once, with the context it really had.

Rendering (ms-swift agent format, as consumed by e2e/prepare_megatron_dataset.py):
  system blocks             -> row["system"], joined with "\\n"
  tools                     -> row["tools"], OpenAI function schema as a JSON string
  user text / image blocks  -> one "user" message, blocks joined with "\\n\\n", images as <image>
  tool_result               -> "tool_response" with the tool output text the model saw (images as <image>)
  role "system" message     -> "system" message (prepare_megatron_dataset.py folds it into the turn before)
  thinking                  -> assistant "<think>...</think>"; text -> assistant; tool_use -> "tool_call"
Images (base64) are written once to <assets>/<sha256>.<ext> and listed in row["images"] in order.
Provenance per row (workspace, call ids, query sources, stop reasons) goes to <output>.meta.jsonl, same order.

The calls JSONL stays the exact, lossless record; this view fixes one text rendering of it. To train
with a model's own chat encoding (e.g. deepseek-recipe), encode the calls JSONL directly instead.

    python3 calls_to_swift.py calls.jsonl train.jsonl --assets assets [--merge-chains] [--include-source sdk]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from transcript_to_calls import message_hash, strip_cache

EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
              "application/pdf": "pdf"}


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Renderer:
    def __init__(self, assets: Path):
        self.assets = assets
        self.images: list[str] = []
        self.stats: Counter = Counter()

    def media(self, block: dict[str, Any]) -> str:
        """Store an image/document block and return its placeholder text."""
        source = block.get("source") or {}
        if source.get("type") == "base64":
            data = base64.b64decode(source.get("data") or "")
            ext = EXTENSIONS.get(source.get("media_type", ""), "bin")
            path = self.assets.resolve() / f"{hashlib.sha256(data).hexdigest()}.{ext}"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        elif source.get("type") == "url":
            path = source.get("url")
        else:
            self.stats["media_without_source"] += 1
            return f"[{block.get('type')} omitted: unsupported source]"
        if block.get("type") == "image":
            self.images.append(str(path))
            return "<image>"
        self.stats[f"{block.get('type')}_as_reference"] += 1
        return f"[{block.get('type')}: {path}]"

    def text_of(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        parts = []
        for block in content or []:
            kind = block.get("type")
            if kind == "text":
                parts.append(block.get("text", ""))
            elif kind in {"image", "document"}:
                parts.append(self.media(block))
            elif kind == "tool_reference":
                parts.append(f"[tool reference: {block.get('tool_name')}]")
            else:
                self.stats[f"unrendered_block:{kind}"] += 1
                parts.append(jdump(block))
        return "\n\n".join(parts)

    def message(self, message: dict[str, Any], loss: bool) -> list[dict[str, Any]]:
        role = message.get("role")
        content = message.get("content")
        out: list[dict[str, Any]] = []
        if role == "assistant":
            for block in content if isinstance(content, list) else [{"type": "text", "text": content}]:
                kind = block.get("type")
                if kind == "thinking":
                    out.append({"role": "assistant", "content": f"<think>{block.get('thinking', '')}</think>",
                                "loss": loss})
                elif kind == "redacted_thinking":
                    self.stats["redacted_thinking_dropped"] += 1
                elif kind == "text":
                    out.append({"role": "assistant", "content": block.get("text", ""), "loss": loss})
                elif kind in {"tool_use", "server_tool_use"}:
                    out.append({"role": "tool_call", "content": jdump({"name": block.get("name"),
                                                                       "arguments": block.get("input")}),
                                "loss": loss})
                else:
                    self.stats[f"unrendered_assistant_block:{kind}"] += 1
            return out
        if role == "system":
            return [{"role": "system", "content": self.text_of(content)}]
        if isinstance(content, str):
            return [{"role": "user", "content": content}]
        pending: list[dict[str, Any]] = []
        for block in content or []:
            if block.get("type") == "tool_result":
                if pending:
                    out.append({"role": "user", "content": self.text_of(pending)})
                    pending = []
                if block.get("is_error"):
                    self.stats["tool_result_is_error"] += 1
                out.append({"role": "tool_response", "content": self.text_of(block.get("content"))})
            else:
                pending.append(block)
        if pending:
            out.append({"role": "user", "content": self.text_of(pending)})
        return out


def normalize_tools(tools: Any) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t.get("name"), "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in tools or [] if isinstance(t, dict) and t.get("name")]


def system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    return "\n".join(b.get("text", "") for b in system or [] if b.get("type") == "text")


def context_key(request: dict[str, Any]) -> str:
    rest = {k: v for k, v in request.items() if k != "messages"}
    return hashlib.sha256(jdump(strip_cache(rest)).encode()).hexdigest()


def same_text_key(message: dict[str, Any]) -> str:
    """Hash that ignores wire-only form: a single text block and the plain string carry the same text."""
    content = strip_cache(message.get("content"))
    if isinstance(content, list) and len(content) == 1 and content[0].get("type") == "text":
        content = content[0].get("text", "")
    return message_hash({"role": message.get("role"), "content": content})


def extends(prev: dict[str, Any], call: dict[str, Any]) -> bool:
    """call's request == prev request + prev response + more, under the same system/tools/params."""
    if context_key(prev["request"]) != context_key(call["request"]):
        return False
    before = [same_text_key(m) for m in prev["request"]["messages"]]
    before.append(same_text_key({"role": "assistant", "content": prev["response"]["content"]}))
    after = [same_text_key(m) for m in call["request"]["messages"]]
    return len(after) > len(before) and after[:len(before)] == before


def build_row(chain: list[dict[str, Any]], renderer: Renderer) -> tuple[dict[str, Any], dict[str, Any]]:
    renderer.images = []
    first = chain[0]
    messages: list[dict[str, Any]] = []
    for m in first["request"]["messages"]:
        messages.extend(renderer.message(m, loss=False))
    for index, call in enumerate(chain):
        if index:
            seen = len(chain[index - 1]["request"]["messages"]) + 1
            for m in call["request"]["messages"][seen:]:
                messages.extend(renderer.message(m, loss=False))
        messages.extend(renderer.message({"role": "assistant", "content": call["response"]["content"]}, loss=True))
    row: dict[str, Any] = {"messages": messages, "system": system_text(first["request"].get("system")),
                           "tools": jdump(normalize_tools(first["request"].get("tools")))}
    if renderer.images:
        row["images"] = list(renderer.images)
    meta = {"workspace": first.get("workspace"), "session_id": first.get("session_id"),
                   "source_file": first.get("source_file"), "call_ids": [c["call_id"] for c in chain],
                   "query_sources": sorted({str(c.get("query_source")) for c in chain}),
                   "model": first["request"].get("model"), "claude_code_version": first.get("claude_code_version"),
                   "stop_reasons": [c["response"].get("stop_reason") for c in chain]}
    return row, meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("calls", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--assets", type=Path, required=True, help="directory for extracted images/documents")
    parser.add_argument("--merge-chains", action="store_true",
                        help="fold exact-prefix call chains into one row (fewer tokens, same supervision)")
    parser.add_argument("--include-source", action="append",
                        help="only keep calls whose query_source starts with this (repeatable; default: all)")
    parser.add_argument("--keep-truncated", action="store_true",
                        help="also train on responses that stopped at max_tokens")
    args = parser.parse_args()

    counts: Counter = Counter()
    renderer = Renderer(args.assets)
    calls_by_file: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for line in args.calls.open(encoding="utf-8"):
        call = json.loads(line)
        if call["status"] != "ok" or not call.get("response"):
            counts[f"skipped_{call['status']}"] += 1
            continue
        if args.include_source and not any(str(call.get("query_source", "")).startswith(s)
                                           for s in args.include_source):
            counts["skipped_query_source"] += 1
            continue
        if call["response"].get("stop_reason") == "max_tokens" and not args.keep_truncated:
            counts["skipped_max_tokens"] += 1
            continue
        calls_by_file.setdefault((str(call.get("workspace")), call["source_file"]), []).append(call)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    meta_path = args.output.with_suffix(args.output.suffix + ".meta.jsonl")
    with args.output.open("w", encoding="utf-8", newline="\n") as out, \
            meta_path.open("w", encoding="utf-8", newline="\n") as meta_out:
        for calls in calls_by_file.values():
            chains: list[list[dict[str, Any]]] = []
            for call in calls:
                if args.merge_chains and chains and extends(chains[-1][-1], call):
                    chains[-1].append(call)
                else:
                    chains.append([call])
            for chain in chains:
                row, meta = build_row(chain, renderer)
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                meta_out.write(json.dumps({"row_index": counts["rows"], **meta}, ensure_ascii=False) + "\n")
                counts["rows"] += 1
                counts["trained_responses"] += len(chain)
    counts.update(renderer.stats)
    print(json.dumps(dict(sorted(counts.items())), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
