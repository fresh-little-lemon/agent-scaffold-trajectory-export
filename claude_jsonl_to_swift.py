#!/usr/bin/env python3
"""Convert Claude Code transcript JSONL into ms-swift Agent JSONL.

Reads transcripts incrementally. API traces are intentionally not an input to this
converter; use the separate audit tool to compare generated rows with API logs.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

SECRET_PATTERNS = [
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]
TEXT_BLOCK_TYPES = {"text", "thinking"}
TOOL_BLOCK_TYPES = {"tool_use", "server_tool_use"}


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Converter:
    def __init__(self, output_dir: Path, redact_secrets: bool = False, include_sidechains: bool = False):
        self.output_dir = output_dir
        self.assets_dir = output_dir / "assets"
        self.redact_secrets = redact_secrets
        self.include_sidechains = include_sidechains
        self.image_paths: list[str] = []
        self.document_paths: list[str] = []
        self.asset_paths: list[str] = []
        self.image_hashes: dict[str, str] = {}
        self.redactions = 0
        self.warnings: list[str] = []
        self.control_event_stream = None

    def redact(self, text: str) -> str:
        if not self.redact_secrets:
            return text
        for pattern in SECRET_PATTERNS:
            text, count = pattern.subn("[REDACTED_SECRET]", text)
            self.redactions += count
        return text

    def convert_assets(self, value: Any) -> Any:
        """Extract embedded image/document payloads to sidecar files."""
        if isinstance(value, list):
            return [self.convert_assets(v) for v in value]
        if not isinstance(value, dict):
            return value
        block_type = value.get("type")
        if block_type in {"image", "document"}:
            # Already-normalized image references must survive nested conversions.
            if value.get("path") or (isinstance(value.get("source"), dict)
                                      and value["source"].get("type") == "file"):
                return value
            source = value.get("source") or {}
            # Claude Code's persisted user image blocks can store bytes in a
            # non-standard `file.base64` field rather than source.data.
            image_file = value.get("file")
            if (block_type == "image" and isinstance(image_file, dict)
                    and image_file.get("base64")):
                source = {
                    "type": "base64",
                    "media_type": image_file.get("type", "image/png"),
                    "data": image_file["base64"],
                }
            if isinstance(source, dict):
                data = source.get("data")
                if data and source.get("type") == "base64":
                    try:
                        raw = base64.b64decode(data, validate=False)
                        digest = hashlib.sha256(raw).hexdigest()
                        rel = self.image_hashes.get(digest)
                        if rel is None:
                            media = str(source.get("media_type", "image/png" if block_type == "image" else "application/octet-stream")).lower()
                            extensions = {
                                "application/pdf": ".pdf",
                                "text/plain": ".txt",
                                "application/json": ".json",
                                "image/jpeg": ".jpg",
                                "image/jpg": ".jpg",
                                "image/webp": ".webp",
                                "image/gif": ".gif",
                                "image/png": ".png",
                            }
                            ext = extensions.get(media, ".bin")
                            self.assets_dir.mkdir(parents=True, exist_ok=True)
                            rel = (Path("assets") / f"{digest}{ext}").as_posix()
                            if not (self.output_dir / rel).exists():
                                (self.output_dir / rel).write_bytes(raw)
                            self.image_hashes[digest] = rel
                        self.asset_paths.append(rel)
                        if block_type == "image":
                            self.image_paths.append(rel)
                            return {"type": "image", "path": rel, "data": "<image>"}
                        self.document_paths.append(rel)
                        return {
                            "type": "document",
                            "source": {"type": "file", "media_type": source.get("media_type"), "path": rel},
                        }
                    except (ValueError, OSError) as exc:
                        self.warnings.append(f"could not extract embedded {block_type}: {type(exc).__name__}")
                        return {"type": block_type, "data": f"[{block_type} omitted: decode failed]"}
                if source.get("type") == "url" and source.get("url"):
                    self.warnings.append(f"remote {block_type} URL retained as reference; not downloaded")
                    return {"type": block_type, "url": source["url"]}
            self.warnings.append(f"{block_type} block had no supported base64 source")
            return {"type": block_type, "data": f"[{block_type} omitted: source unavailable]"}
        return {k: self.convert_assets(v) for k, v in value.items()}

    def convert_images(self, value: Any) -> Any:
        """Compatibility wrapper; embedded documents are extracted too."""
        return self.convert_assets(value)

    def redact_object(self, value: Any) -> Any:
        """Redact secrets recursively before writing event records to sidecars."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.redact_object(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact_object(item) for key, item in value.items()}
        return value

    def task_notification(self, record: dict[str, Any], line: int) -> dict[str, Any] | None:
        """Extract a task-notification payload for the audit manifest."""
        typ = record.get("type")
        attachment = record.get("attachment") or {}
        if typ == "queue-operation":
            payload = record.get("content")
            kind = "queue-operation"
            state = record.get("operation")
        elif typ == "attachment":
            payload = attachment.get("prompt")
            kind = "attachment"
            state = "delivered"
        else:
            return None
        if not isinstance(payload, str) or "<task-notification" not in payload:
            return None

        decoded = html.unescape(payload)

        def field(tag: str) -> str | None:
            match = re.search(rf"<{tag}>(.*?)</{tag}>", decoded, re.DOTALL)
            return match.group(1).strip() if match else None

        event = field("event")
        summary = field("summary")
        tool_use_id = field("tool-use-id")
        status = field("status")
        output_file = field("output-file")
        if event is None and summary is None:
            return None
        return {
            "line": line,
            "kind": kind,
            "state": state,
            "task_id": field("task-id"),
            "tool_use_id": tool_use_id,
            "status": status,
            "output_file": output_file,
            "summary": self.redact(summary) if summary is not None else None,
            "event": self.redact(event) if event is not None else None,
            "payload": self.redact(decoded),
            "timestamp": record.get("timestamp"),
            "source_uuid": record.get("uuid") or attachment.get("source_uuid"),
            "parent_uuid": record.get("parentUuid"),
            "delivery_id": record.get("deliveryId") or attachment.get("delivery_id"),
            "command_uuid": record.get("commandUuid"),
            "reason": record.get("reason"),
            "agent_id": record.get("agentId"),
            "is_sidechain": bool(record.get("isSidechain", False)),
            "rendered_role": record.get("renderedRole"),
            "rendered": [self.redact(item.get("content", "")) for item in record.get("rendered", [])
                         if isinstance(item, dict) and isinstance(item.get("content"), str)],
            "rendered_in_human_turn": [
                self.redact(item.get("content", "")) for item in record.get("renderedInHumanTurn", [])
                if isinstance(item, dict) and isinstance(item.get("content"), str)
            ],
        }

    def text_from_content(self, value: Any) -> str:
        value = self.convert_assets(value)
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif isinstance(item, dict) and item.get("type") == "image":
                    parts.append("<image>")
                else:
                    parts.append(jdump(item))
            return self.redact("".join(parts))
        return self.redact(jdump(value)) if value is not None else ""

    @staticmethod
    def normalize_tools(tools: Any) -> list[dict[str, Any]] | None:
        if not isinstance(tools, list):
            return None
        result = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                result.append(tool)
            elif tool.get("name"):
                schema = tool.get("schema")
                if isinstance(schema, dict) and isinstance(schema.get("input_schema"), dict):
                    schema = schema["input_schema"]
                result.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", schema or tool.get("parameters")) or {
                            "type": "object", "properties": {}
                        },
                    },
                })
        return result or None

    @staticmethod
    def normalize_system(system: Any) -> str | None:
        if system is None:
            return None
        if isinstance(system, str):
            return system
        if isinstance(system, list):
            chunks = []
            for block in system:
                if isinstance(block, str):
                    chunks.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(str(block.get("text", "")))
            return "\n".join(x for x in chunks if x)
        return jdump(system)

    def assistant_group(self, group: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        output: list[dict[str, Any]] = []
        descriptors: list[dict[str, Any]] = []
        # Ordered semantic content blocks accumulated from successive snapshots
        blocks: list[dict[str, Any]] = []
        tool_positions: dict[str, int] = {}
        text_positions: dict[tuple[str, int], list[int]] = {}
        for snapshot in group["snapshots"]:
            message = snapshot.get("message") or {}
            content = message.get("content")
            content_blocks = content if isinstance(content, list) else ([{"type": "text", "text": content}] if isinstance(content, str) else [])
            group["thinking_blocks"] += sum(
                1 for block in content_blocks
                if isinstance(block, dict) and block.get("type") == "thinking"
            )
            # Keep readable reasoning. Empty thinking plus a signature is opaque;
            # count it in the manifest but never invent its plaintext.
            semantic_blocks = [block for block in content_blocks if isinstance(block, dict)]
            text_index = 0
            thinking_index = 0
            for content_index, block in enumerate(semantic_blocks):
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind in TOOL_BLOCK_TYPES:
                    tool_id = str(block.get("id") or "")
                    key = tool_id or hashlib.sha256(jdump(block).encode()).hexdigest()
                    normalized = {
                        "kind": "tool",
                        "id": tool_id,
                        "name": str(block.get("name", "")),
                        "input": self.convert_images(block.get("input", {})),
                        "source_uuid": snapshot.get("uuid"),
                        "parent_uuid": snapshot.get("parentUuid"),
                        "is_sidechain": bool(snapshot.get("isSidechain", False)),
                    }
                    if key in tool_positions:
                        blocks[tool_positions[key]] = normalized
                    else:
                        tool_positions[key] = len(blocks)
                        blocks.append(normalized)
                elif kind in TEXT_BLOCK_TYPES:
                    value_key = "thinking" if kind == "thinking" else "text"
                    value = self.redact(str(block.get(value_key, "")))
                    slot_index = thinking_index if kind == "thinking" else text_index
                    if kind == "thinking":
                        thinking_index += 1
                    else:
                        text_index += 1
                    if not value:
                        if kind == "thinking":
                            group["opaque_thinking_blocks"] += 1
                        continue
                    # Merge only successive versions of the same content slot. Separate
                    # text blocks in one message remain separate even when prefix-related.
                    replaced = False
                    slot = (str(kind), slot_index)
                    for pos in text_positions.get(slot, []):
                        old = blocks[pos]["text"]
                        if old == value or old.startswith(value):
                            replaced = True
                            break
                        if value.startswith(old):
                            blocks[pos]["text"] = value
                            replaced = True
                            break
                    if not replaced:
                        text_positions.setdefault(slot, []).append(len(blocks))
                        blocks.append({
                            "kind": kind, "text": value,
                            "source_uuid": snapshot.get("uuid"),
                            "parent_uuid": snapshot.get("parentUuid"),
                            "is_sidechain": bool(snapshot.get("isSidechain", False)),
                        })
                else:
                    group["ignored_block_types"][str(kind or "?")] += 1

        for block in blocks:
            if block["kind"] in TEXT_BLOCK_TYPES:
                content = block["text"]
                channel = None
                if block.get("kind") == "thinking":
                    content = f"<think>{content}</think>"
                    channel = "thinking"
                output.append({"role": "assistant", "content": content})
                descriptors.append({
                    "role": "assistant", "channel": channel, "source_uuid": block.get("source_uuid"),
                    "parent_uuid": block.get("parent_uuid"),
                    "message_id": group.get("message_id"),
                    "is_sidechain": block.get("is_sidechain", False),
                })
            elif block["kind"] == "tool":
                args = block["input"]
                output.append({
                    "role": "tool_call",
                    "content": self.redact(jdump({"name": block["name"], "arguments": args})),
                })
                descriptors.append({
                    "role": "tool_call", "source_uuid": block.get("source_uuid"),
                    "parent_uuid": block.get("parent_uuid"),
                    "message_id": group.get("message_id"),
                    "tool_use_id": block["id"], "tool_name": block["name"],
                    "is_sidechain": bool(block.get("is_sidechain", False)),
                })
        return output, descriptors

    def convert_session(self, transcript: Path, supplied_system: str | None = None,
                        supplied_tools: Any = None, sample_index: int = 0) -> tuple[dict[str, Any], dict[str, Any]]:
        # A dedicated sub-agent/workflow JSONL is itself the trajectory to train
        # on.  Its records are all marked isSidechain=true, whereas a root
        # session may contain sidechain records interleaved with the main run.
        # Keep the latter filtered by default while retaining the former.
        include_sidechains = self.include_sidechains or self.is_dedicated_sidechain(transcript)
        groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
        events: list[dict[str, Any]] = []
        total_records = 0
        event_types: dict[str, int] = {}
        sidechain_records = 0
        attachment_records = 0
        meta_records = 0
        compact_summary_records = 0
        compact_boundary_records = 0
        away_summary_records = 0
        compact_summaries: list[dict[str, Any]] = []
        compact_boundaries: list[dict[str, Any]] = []
        task_notifications: list[dict[str, Any]] = []
        queue_task_notifications: list[dict[str, Any]] = []
        seen_notification_deliveries: set[tuple[str, str, str, str]] = set()
        duplicate_notification_records = 0
        queue_operation_counts: dict[str, int] = {}
        queue_operation_events: list[dict[str, Any]] = []
        sidechain_attachment_records_excluded = 0
        rendered_attachment_count = 0
        attachment_records_with_rendered_content = 0
        attachment_records_without_rendered_content = 0
        duplicate_rendered_attachment_count = 0
        attachment_render_variants: list[dict[str, Any]] = []
        prompt_snapshots: list[dict[str, Any]] = []
        latest_snapshot_system: Any = None
        latest_snapshot_tools: Any = None
        seen_rendered_attachments: set[tuple[str, str, str]] = set()
        agent_ids: set[str] = set()
        logical_parent_uuids: set[str] = set()

        with transcript.open("r", encoding="utf-8", errors="replace") as stream:
            for lineno, line in enumerate(stream, 1):
                total_records += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    self.warnings.append(f"invalid transcript JSON at line {lineno}")
                    continue
                typ = str(record.get("type", "?"))
                event_types[typ] = event_types.get(typ, 0) + 1
                if typ in {"attachment", "queue-operation"} and self.control_event_stream is not None:
                    self.control_event_stream.write(jdump({
                        "sample_index": sample_index,
                        "source_transcript": str(transcript),
                        "line": lineno,
                        "record": self.redact_object(record),
                    }) + "\n")
                if typ == "queue-operation":
                    operation = str(record.get("operation", "unknown"))
                    queue_operation_counts[operation] = queue_operation_counts.get(operation, 0) + 1
                    operation_content = record.get("content")
                    queue_operation_events.append({
                        "line": lineno,
                        "operation": operation,
                        "timestamp": record.get("timestamp"),
                        "uuid": record.get("uuid"),
                        "parent_uuid": record.get("parentUuid"),
                        "delivery_id": record.get("deliveryId"),
                        "command_uuid": record.get("commandUuid"),
                        "reason": record.get("reason"),
                        "agent_id": record.get("agentId"),
                        "is_sidechain": bool(record.get("isSidechain", False)),
                        "content_length": len(operation_content) if isinstance(operation_content, str) else None,
                        "content_sha256": hashlib.sha256(operation_content.encode("utf-8")).hexdigest()
                        if isinstance(operation_content, str) else None,
                        "content": self.redact(operation_content) if isinstance(operation_content, str) else None,
                        "visibility": None,
                        "training_message_indices": [],
                        "linked_attachment_lines": [],
                    })
                if record.get("agentId"):
                    agent_ids.add(str(record["agentId"]))
                if record.get("logicalParentUuid"):
                    logical_parent_uuids.add(str(record["logicalParentUuid"]))
                if record.get("isMeta"):
                    meta_records += 1
                if record.get("isCompactSummary"):
                    compact_summary_records += 1
                    compact_summaries.append({
                        "line": lineno, "uuid": record.get("uuid"),
                        "parent_uuid": record.get("parentUuid"),
                    })
                if typ == "system" and record.get("subtype") == "compact_boundary":
                    compact_boundary_records += 1
                    compact_boundaries.append({
                        "line": lineno, "uuid": record.get("uuid"),
                        "parent_uuid": record.get("parentUuid"),
                        "compact_metadata": record.get("compactMetadata"),
                    })
                if typ == "system" and record.get("subtype") == "away_summary":
                    away_summary_records += 1
                notification = self.task_notification(record, lineno)
                if notification is not None:
                    if typ == "queue-operation":
                        # Keep queue lifecycle facts separate from trainable
                        # messages, but attach the task identity so an auditor
                        # can follow enqueue -> delivery -> rendered message.
                        if queue_operation_events and queue_operation_events[-1]["line"] == lineno:
                            queue_operation_events[-1].update({
                                "task_id": notification.get("task_id"),
                                "tool_use_id": notification.get("tool_use_id"),
                                "summary": notification.get("summary"),
                                "event": notification.get("event"),
                                "status": notification.get("status"),
                                "output_file": notification.get("output_file"),
                            })
                        queue_task_notifications.append(notification)
                    else:
                        delivery_id = notification.get("delivery_id")
                        # A delivery ID denotes one rendered delivery. Collapse
                        # identical serialization duplicates, not repeated text
                        # that arrived through a distinct delivery.
                        if delivery_id:
                            dedup_key = (
                                str(notification["kind"]), str(notification.get("state")),
                                str(delivery_id), hashlib.sha256(notification["payload"].encode("utf-8")).hexdigest(),
                            )
                            if dedup_key in seen_notification_deliveries:
                                duplicate_notification_records += 1
                            else:
                                seen_notification_deliveries.add(dedup_key)
                                task_notifications.append(notification)
                        else:
                            task_notifications.append(notification)
                if record.get("isSidechain"):
                    sidechain_records += 1
                    if not include_sidechains:
                        if typ == "attachment":
                            sidechain_attachment_records_excluded += 1
                        continue
                if typ == "attachment":
                    attachment_records += 1
                    attachment = record.get("attachment") or {}
                    if attachment.get("type") == "prompt_snapshot":
                        snapshot_system = attachment.get("systemPrompt")
                        snapshot_tools = attachment.get("tools")
                        if snapshot_system is not None:
                            latest_snapshot_system = snapshot_system
                        if snapshot_tools is not None:
                            latest_snapshot_tools = snapshot_tools
                        normalized_snapshot_system = self.normalize_system(snapshot_system)
                        normalized_snapshot_tools = self.normalize_tools(snapshot_tools)
                        prompt_snapshots.append({
                            "line": lineno,
                            "source_uuid": record.get("uuid"),
                            "system_present": snapshot_system is not None,
                            "system_character_count": len(self.redact(normalized_snapshot_system))
                            if normalized_snapshot_system else 0,
                            "system_sha256": hashlib.sha256(self.redact(normalized_snapshot_system).encode("utf-8")).hexdigest()
                            if normalized_snapshot_system else None,
                            "tools_present": snapshot_tools is not None,
                            "tools_count": len(normalized_snapshot_tools or []),
                            "tools_sha256": hashlib.sha256(self.redact(jdump(normalized_snapshot_tools)).encode("utf-8")).hexdigest()
                            if normalized_snapshot_tools else None,
                            "context_rendering": attachment.get("contextRendering"),
                        })
                    rendered_role = record.get("renderedRole")
                    rendered_items = record.get("rendered") or []
                    has_rendered_content = False
                    variant_index: dict[str, list[dict[str, Any]]] = {}
                    for variant_name in ("rendered", "renderedInHumanTurn", "renderedBesideToolResult"):
                        variant_items = record.get(variant_name)
                        if not isinstance(variant_items, list):
                            continue
                        index_items = []
                        for item_index, item in enumerate(variant_items):
                            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                                continue
                            variant_content = self.redact(item["content"])
                            index_items.append({
                                "item_index": item_index,
                                "character_count": len(variant_content),
                                "sha256": hashlib.sha256(variant_content.encode("utf-8")).hexdigest(),
                            })
                        if index_items:
                            variant_index[variant_name] = index_items
                    attachment_render_variants.append({
                        "line": lineno,
                        "source_uuid": record.get("uuid"),
                        "attachment_type": attachment.get("type"),
                        "delivery_id": attachment.get("delivery_id"),
                        "prompt_sha256": hashlib.sha256(attachment["prompt"].encode("utf-8")).hexdigest()
                        if isinstance(attachment.get("prompt"), str) else None,
                        "rendered_role": rendered_role,
                        "variants": variant_index,
                        "training_variant": "rendered" if variant_index.get("rendered") else None,
                    })
                    if rendered_role in {"system", "user", "assistant"} and isinstance(rendered_items, list):
                        stable_id = str(attachment.get("delivery_id") or record.get("uuid") or f"line-{lineno}")
                        for rendered_item in rendered_items:
                            if not isinstance(rendered_item, dict):
                                continue
                            rendered_text = rendered_item.get("content")
                            if not isinstance(rendered_text, str) or not rendered_text.strip():
                                continue
                            has_rendered_content = True
                            rendered_text = self.redact(rendered_text)
                            digest = hashlib.sha256(rendered_text.encode("utf-8")).hexdigest()
                            dedup_key = (stable_id, str(rendered_role), digest)
                            if dedup_key in seen_rendered_attachments:
                                duplicate_rendered_attachment_count += 1
                                continue
                            seen_rendered_attachments.add(dedup_key)
                            events.append({
                                "line": lineno, "role": rendered_role, "content": rendered_text,
                                "source_uuid": record.get("uuid"), "parent_uuid": record.get("parentUuid"),
                                "source_type": "attachment", "attachment_type": attachment.get("type"),
                                "delivery_id": attachment.get("delivery_id"),
                                "task_id": notification.get("task_id") if notification else None,
                                "rendered_variant": "rendered",
                                "is_meta": bool(record.get("isMeta", False)),
                                "is_compact_summary": bool(record.get("isCompactSummary", False)),
                                "is_sidechain": bool(record.get("isSidechain", False)),
                            })
                            rendered_attachment_count += 1
                    if has_rendered_content:
                        attachment_records_with_rendered_content += 1
                    else:
                        attachment_records_without_rendered_content += 1
                    continue
                if typ == "assistant":
                    message = record.get("message") or {}
                    mid = str(message.get("id") or record.get("uuid") or f"line-{lineno}")
                    if mid not in groups:
                        groups[mid] = {
                            "message_id": message.get("id"),
                            "first_line": lineno,
                            "last_line": lineno,
                            "snapshots": [],
                            "uuids": [],
                            "usage": None,
                            "stop_reason": None,
                            "thinking_blocks": 0,
                            "opaque_thinking_blocks": 0,
                            "ignored_block_types": {},
                        }
                    group = groups[mid]
                    group["last_line"] = lineno
                    group["snapshots"].append(record)
                    if record.get("uuid"):
                        group["uuids"].append(record["uuid"])
                    if message.get("usage"):
                        group["usage"] = message["usage"]
                    if message.get("stop_reason") is not None:
                        group["stop_reason"] = message.get("stop_reason")
                    continue
                if typ == "user":
                    message = record.get("message") or {}
                    content = message.get("content")
                    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}] if isinstance(content, str) else []
                    for block in blocks:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            result_value = {
                                "tool_use_id": block.get("tool_use_id"),
                                "content": self.convert_images(block.get("content", "")),
                                "is_error": bool(block.get("is_error", False)),
                            }
                            if record.get("sourceToolAssistantUUID"):
                                result_value["source_tool_assistant_uuid"] = record["sourceToolAssistantUUID"]
                            if record.get("toolUseResult") is not None:
                                result_value["tool_use_result"] = self.convert_images(record["toolUseResult"])
                            result_value = self.convert_images(result_value)
                            events.append({
                                "line": lineno, "role": "tool_response",
            "content": self.redact(jdump(result_value)),
                                "source_uuid": record.get("uuid"),
                                "parent_uuid": record.get("parentUuid"),
                                "tool_use_id": block.get("tool_use_id"),
                                "source_tool_assistant_uuid": record.get("sourceToolAssistantUUID"),
                                "agent_id": (record.get("toolUseResult") or {}).get("agentId")
                                            if isinstance(record.get("toolUseResult"), dict) else None,
                                "is_sidechain": bool(record.get("isSidechain", False)),
                            })
                        elif isinstance(block, dict) and block.get("type") == "text":
                            text = self.redact(str(block.get("text", "")))
                            if text.strip():
                                events.append({
                                    "line": lineno, "role": "user", "content": text,
                                    "source_uuid": record.get("uuid"),
                                    "parent_uuid": record.get("parentUuid"),
                                "is_meta": bool(record.get("isMeta", False)),
                                "is_compact_summary": bool(record.get("isCompactSummary", False)),
                                    "is_sidechain": bool(record.get("isSidechain", False)),
                                })
                        elif isinstance(block, dict) and block.get("type") == "image":
                            img = self.convert_images(block)
                            events.append({
                                "line": lineno, "role": "user", "content": "<image>",
                                "source_uuid": record.get("uuid"),
                                "parent_uuid": record.get("parentUuid"),
                                "is_meta": bool(record.get("isMeta", False)),
                                "is_compact_summary": bool(record.get("isCompactSummary", False)),
                                "is_sidechain": bool(record.get("isSidechain", False)),
                            })
                        elif block is not None:
                            events.append({
                                "line": lineno, "role": "user",
                                "content": self.redact(jdump(self.convert_images(block))),
                                "source_uuid": record.get("uuid"),
                                "parent_uuid": record.get("parentUuid"),
                                "is_meta": bool(record.get("isMeta", False)),
                                "is_compact_summary": bool(record.get("isCompactSummary", False)),
                                "is_sidechain": bool(record.get("isSidechain", False)),
                            })

        # Render assistant groups and interleave with user/tool-result records by persisted line order.
        # Attach queue lifecycle lines to their rendered delivery by delivery ID
        # and content hash. This is provenance only; queue records never become
        # synthetic model turns.
        queue_links_by_attachment_line: dict[int, list[int]] = {}
        for variant in attachment_render_variants:
            delivery_id = variant.get("delivery_id")
            prompt_hash = variant.get("prompt_sha256")
            links = []
            for queue_event in queue_operation_events:
                same_delivery = delivery_id and queue_event.get("delivery_id") == delivery_id
                same_payload = prompt_hash and queue_event.get("content_sha256") == prompt_hash
                if same_delivery or same_payload:
                    links.append(queue_event["line"])
            if links:
                variant["queue_operation_lines"] = links
                queue_links_by_attachment_line[variant["line"]] = links
        for event in events:
            if event.get("source_type") == "attachment" and event.get("line") in queue_links_by_attachment_line:
                event["queue_operation_lines"] = queue_links_by_attachment_line[event["line"]]

        rendered_groups: list[tuple[int, list[dict[str, Any]], list[dict[str, Any]]]] = []
        for group in groups.values():
            msgs, desc = self.assistant_group(group)
            if msgs:
                rendered_groups.append((group["last_line"], msgs, desc))
        timeline: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for event in events:
            timeline.append((event["line"], {
                "role": event["role"], "content": event["content"]
            }, {k: event.get(k) for k in (
                "role", "source_uuid", "parent_uuid", "tool_use_id",
                "source_tool_assistant_uuid", "agent_id", "is_meta", "is_compact_summary",
                "source_type", "attachment_type", "delivery_id", "task_id", "rendered_variant",
                "queue_operation_lines",
            )}))
        for line, msgs, desc in rendered_groups:
            for msg, descriptor in zip(msgs, desc):
                timeline.append((line, msg, descriptor))
        timeline.sort(key=lambda x: x[0])

        # The parent session stores the spawned agent ID on the tool result,
        # while the agent transcript stores it on every branch record. Link the
        # call-side manifest entry to that ID through the assistant UUID.
        agent_id_by_tool_assistant = {
            event["source_tool_assistant_uuid"]: event["agent_id"]
            for event in events
            if event.get("source_tool_assistant_uuid") and event.get("agent_id")
        }

        # Exact duplicate tool results occasionally occur in repaired/retried transcripts.
        # Deduplicate only by tool_use_id; never deduplicate natural language by text.
        output_messages: list[dict[str, str]] = []
        message_map: list[dict[str, Any]] = []
        seen_results: dict[str, set[str]] = {}
        for line, message, descriptor in timeline:
            if message["role"] == "tool_call" and descriptor.get("source_uuid") in agent_id_by_tool_assistant:
                descriptor["agent_id"] = agent_id_by_tool_assistant[descriptor["source_uuid"]]
            tid = descriptor.get("tool_use_id")
            if message["role"] == "tool_response" and tid:
                digest = hashlib.sha256(message["content"].encode("utf-8")).hexdigest()
                previous = seen_results.setdefault(str(tid), set())
                if digest in previous:
                    continue
                if previous:
                    self.warnings.append(f"conflicting duplicate tool results for tool_use_id {tid}")
                previous.add(digest)
            output_messages.append(message)
            message_map.append({
                "message_index": len(output_messages) - 1,
                "line": line,
                "role": message["role"],
                "source_uuid": descriptor.get("source_uuid"),
                "parent_uuid": descriptor.get("parent_uuid"),
                "message_id": descriptor.get("message_id"),
                "tool_use_id": tid,
                "tool_name": descriptor.get("tool_name"),
                "source_tool_assistant_uuid": descriptor.get("source_tool_assistant_uuid"),
                "agent_id": descriptor.get("agent_id"),
                "is_meta": descriptor.get("is_meta", False),
                "is_compact_summary": descriptor.get("is_compact_summary", False),
                "source_type": descriptor.get("source_type"),
                "attachment_type": descriptor.get("attachment_type"),
                "delivery_id": descriptor.get("delivery_id"),
                "task_id": descriptor.get("task_id"),
                "rendered_variant": descriptor.get("rendered_variant"),
                "queue_operation_lines": descriptor.get("queue_operation_lines"),
            })

        # Add reverse indices so an auditor can jump from an operation to the
        # exact rendered train messages it was linked with.
        queue_links: dict[int, dict[str, set[int]]] = {}
        for message in message_map:
            for queue_line in message.get("queue_operation_lines") or []:
                links = queue_links.setdefault(queue_line, {"attachments": set(), "messages": set()})
                if message.get("source_type") == "attachment" and message.get("line") is not None:
                    links["attachments"].add(message["line"])
                links["messages"].add(message["message_index"])
        for queue_event in queue_operation_events:
            links = queue_links.get(queue_event["line"], {"attachments": set(), "messages": set()})
            queue_event["linked_attachment_lines"] = sorted(links["attachments"])
            queue_event["training_message_indices"] = sorted(links["messages"])
            queue_event["model_visible_via_rendered_attachment"] = bool(links["messages"])
            if links["messages"]:
                queue_event["visibility"] = "visible_via_rendered_attachment"
            elif queue_event.get("content"):
                queue_event["visibility"] = "unresolved_without_matching_rendered_message"
            else:
                queue_event["visibility"] = "lifecycle_only_no_payload"

        tools_source = "provided file" if supplied_tools is not None else None
        tools = supplied_tools
        if tools is None and latest_snapshot_tools is not None:
            tools = latest_snapshot_tools
            tools_source = "prompt_snapshot attachment"
        tools = self.normalize_tools(tools) or (tools if isinstance(tools, list) else None)
        system_source = "provided file" if supplied_system is not None else None
        system = supplied_system
        if system is None and latest_snapshot_system is not None:
            system = self.normalize_system(latest_snapshot_system)
            system_source = "prompt_snapshot attachment"
        row: dict[str, Any] = {"messages": output_messages}
        if system:
            row["system"] = self.redact(system)
        if tools:
            row["tools"] = self.redact(jdump(tools))
        if self.image_paths:
            row["images"] = list(self.image_paths)

        page_id = self.page_id(transcript)
        meta = {
            "sample_index": sample_index,
            "source_transcript": str(transcript),
            "source_trace": None,
            "page_id": page_id,
            "session_id": next((g["snapshots"][0].get("sessionId") for g in groups.values() if g["snapshots"]), None),
            "agent_id": next(iter(agent_ids)) if len(agent_ids) == 1 else sorted(agent_ids) if agent_ids else None,
            "logical_parent_uuids": sorted(logical_parent_uuids),
            "branch_type": self.branch_type(transcript),
            "claude_code_version": next((g["snapshots"][0].get("version") for g in groups.values() if g["snapshots"]), None),
            "model": next((g["snapshots"][0].get("message", {}).get("model") for g in groups.values() if g["snapshots"] and g["snapshots"][0].get("message", {}).get("model")), None),
            "system_source": system_source or "not present in transcript; unavailable",
            "tools_source": tools_source or "not present in transcript; unavailable",
            "prompt_snapshot_count": len(prompt_snapshots),
            "prompt_snapshots": prompt_snapshots,
            "assistant_message_groups": len(groups),
            "assistant_duplicate_snapshots": sum(max(0, len(g["snapshots"]) - 1) for g in groups.values()),
            "thinking_block_count": sum(g["thinking_blocks"] for g in groups.values()),
            "opaque_thinking_block_count": sum(g.get("opaque_thinking_blocks", 0) for g in groups.values()),
            "thinking_plaintext_block_count": sum(
                g["thinking_blocks"] - g.get("opaque_thinking_blocks", 0)
                for g in groups.values()
            ),
            "ignored_block_types": self.sum_block_counts(groups.values()),
            "sidechain_record_count_excluded": sidechain_records if not include_sidechains else 0,
            "sidechain_record_count_included": sidechain_records if include_sidechains else 0,
            "dedicated_sidechain_file": self.is_dedicated_sidechain(transcript),
            "meta_record_count": meta_records,
            "compact_summary_record_count": compact_summary_records,
            "compact_boundary_record_count": compact_boundary_records,
            "compact_summaries": compact_summaries,
            "compact_boundaries": compact_boundaries,
            "away_summary_record_count": away_summary_records,
            "attachment_record_count_in_event_log": event_types.get("attachment", 0),
            "attachment_record_count_included_branch": attachment_records,
            "attachment_record_count_excluded_sidechain": sidechain_attachment_records_excluded,
            "attachment_record_count_with_rendered_content": attachment_records_with_rendered_content,
            "attachment_record_count_without_rendered_content": attachment_records_without_rendered_content,
            "rendered_attachment_message_count_included": rendered_attachment_count,
            "duplicate_rendered_attachment_message_count": duplicate_rendered_attachment_count,
            "attachment_render_variants": attachment_render_variants,
            "queue_operation_count": sum(queue_operation_counts.values()),
            "queue_operation_counts": queue_operation_counts,
            "queue_operation_events": queue_operation_events,
            "background_task_notification_count": len(task_notifications),
            "background_task_notification_duplicate_count": duplicate_notification_records,
            "background_task_notifications": task_notifications,
            "queue_operation_task_notification_count": len(queue_task_notifications),
            "queue_operation_task_notifications": queue_task_notifications,
            "transcript_record_count": total_records,
            "transcript_event_types": event_types,
            "messages": message_map,
            "image_assets": list(self.image_paths),
            "document_assets": list(self.document_paths),
            "embedded_assets": list(self.asset_paths),
            "secret_redactions": self.redactions,
            "warnings": list(dict.fromkeys(self.warnings)),
        }
        return row, meta

    @staticmethod
    def sum_block_counts(groups: Iterable[dict[str, Any]]) -> dict[str, int]:
        total: dict[str, int] = {}
        for group in groups:
            for key, value in group["ignored_block_types"].items():
                total[key] = total.get(key, 0) + value
        return total

    @staticmethod
    def page_id(path: Path) -> str:
        parts = path.resolve().parts
        # Evaluation paths commonly include /home/<user>/workspace/.../workspace/<page>.
        # The last workspace segment is the page mount; using the first resolves the
        # user's home workspace and silently misses the matching trace.
        indexes = [i for i, part in enumerate(parts) if part == "workspace"]
        if indexes:
            ix = indexes[-1]
            if ix + 1 < len(parts):
                return parts[ix + 1]
        return path.stem

    @staticmethod
    def is_dedicated_sidechain(path: Path) -> bool:
        """Return true for Claude's separately persisted agent/workflow logs."""
        parts = {part.lower() for part in path.resolve().parts}
        # Include singular names as well because an exported/copy directory
        # often flattens Claude's `subagents/workflows` hierarchy.
        return bool(parts & {"subagent", "subagents", "workflow", "workflows"})

    @staticmethod
    def branch_type(path: Path) -> str:
        parts = {part.lower() for part in path.resolve().parts}
        if parts & {"workflow", "workflows"}:
            return "workflow_agent"
        if parts & {"subagent", "subagents"}:
            return "subagent"
        return "main_session"

    def convert_paths(self, paths: list[Path]) -> tuple[Path, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        train_path = self.output_dir / "train.jsonl"
        manifest_path = self.output_dir / "manifest.jsonl"
        control_events_path = self.output_dir / "control-events.jsonl"
        with (train_path.open("w", encoding="utf-8") as train,
              manifest_path.open("w", encoding="utf-8") as manifest,
              control_events_path.open("w", encoding="utf-8") as control_events):
            self.control_event_stream = control_events
            for index, path in enumerate(paths):
                self.image_paths = []
                self.document_paths = []
                self.asset_paths = []
                self.warnings = []
                redactions_before = self.redactions
                row, meta = self.convert_session(path, sample_index=index)
                meta["secret_redactions"] = self.redactions - redactions_before
                # Workflow journals and other control-only JSONL files are valid
                # provenance inputs but cannot be Swift training rows.
                if row.get("messages"):
                    train.write(jdump(row) + "\n")
                    meta["training_row_written"] = True
                else:
                    meta["training_row_written"] = False
                    meta["warnings"] = list(dict.fromkeys(
                        [*meta.get("warnings", []), "empty conversational session omitted from train.jsonl"]
                    ))
                manifest.write(jdump(meta) + "\n")
        self.control_event_stream = None
        return train_path, manifest_path


def collect_paths(items: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in items:
        path = Path(item)
        if path.is_dir():
            paths.extend(p for p in path.rglob("*.jsonl") if ".claude" in p.parts and "projects" in p.parts)
        elif path.is_file():
            paths.append(path)
        else:
            print(f"warning: input does not exist: {item}", file=sys.stderr)
    # Explicit files must look like Claude session logs; directories are scoped to
    # .claude/projects so traces and MCP caches cannot become empty training rows.
    accepted: list[Path] = []
    for p in paths:
        if p.suffix != ".jsonl":
            continue
        if ".claude" in p.parts and "projects" in p.parts:
            accepted.append(p)
            continue
        try:
            with p.open("r", encoding="utf-8", errors="replace") as stream:
                first = {}
                # Real Claude sessions may begin with mode/permission/atis
                # control records before the first user turn.  Inspect only a
                # small prefix so directory scans remain cheap and bounded.
                for _ in range(64):
                    line = next(stream, "")
                    if not line:
                        break
                    if not line.strip():
                        continue
                    try:
                        candidate = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if candidate.get("type") in {"user", "assistant"}:
                        first = candidate
                        break
            if first.get("type") in {"user", "assistant"} and (first.get("uuid") or first.get("sessionId")):
                accepted.append(p)
            else:
                print(f"warning: skipping non-session JSONL: {p}", file=sys.stderr)
        except (OSError, json.JSONDecodeError):
            print(f"warning: skipping unreadable/non-session JSONL: {p}", file=sys.stderr)
    return sorted(set(p.resolve() for p in accepted))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="session JSONL file(s) or directories")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--redact-secrets", action="store_true",
                        help="opt in to basic API-key/Bearer redaction; raw export is the default")
    parser.add_argument("--no-redact-secrets", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--include-sidechains", action="store_true",
                        help="include Claude sidechain records in append order; may mix parallel branches")
    args = parser.parse_args()
    paths = collect_paths(args.inputs)
    if not paths:
        parser.error("no JSONL inputs found")
    converter = Converter(Path(args.output_dir), redact_secrets=args.redact_secrets and not args.no_redact_secrets,
                          include_sidechains=args.include_sidechains)
    train, manifest = converter.convert_paths(
        paths,
    )
    print(f"converted {len(paths)} sessions")
    print(f"train: {train}")
    print(f"manifest: {manifest}")
    print(f"control events: {Path(args.output_dir) / 'control-events.jsonl'}")
    print(f"image assets: {len(converter.image_hashes)}")
    print(f"secret redactions: {converter.redactions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

