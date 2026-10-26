#!/usr/bin/env python3
"""Validate converted Claude rows and rewrite image paths for a mounted Swift dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def merge_system_reminders(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep model-visible reminders while respecting Swift's turn/message schema."""
    normalized = []
    audit = []
    for index, original in enumerate(messages):
        message = dict(original)
        if message["role"] != "system":
            if message["role"] == "user" and normalized and normalized[-1]["role"] == "tool_response":
                target = normalized[-1]
                response = json.loads(target["content"])
                response.setdefault("followup_user_messages", []).append(message["content"])
                target["content"] = json.dumps(response, ensure_ascii=False)
                audit.append({
                    "source_message_index": index,
                    "placement": "preceding_tool_response",
                    "kind": "user_message",
                    "content_sha256": hashlib.sha256(message["content"].encode("utf-8")).hexdigest(),
                })
                continue
            normalized.append(message)
            continue

        content = message["content"]
        placement = "user_message"
        if normalized and normalized[-1]["role"] == "user":
            normalized[-1]["content"] += "\n\n" + content
            placement = "preceding_user"
        elif normalized and normalized[-1]["role"] == "tool_response":
            target = normalized[-1]
            placement = "preceding_tool_response"
        elif normalized and normalized[-1]["role"] == "tool_call":
            target = next((candidate for candidate in messages[index + 1:]
                           if candidate["role"] == "tool_response"), None)
            if target is None:
                normalized.append({"role": "user", "content": content})
            else:
                placement = "following_tool_response"
        else:
            normalized.append({"role": "user", "content": content})

        if placement in {"preceding_tool_response", "following_tool_response"}:
            response = json.loads(target["content"])
            response.setdefault("system_reminders", []).append(content)
            target["content"] = json.dumps(response, ensure_ascii=False)
        audit.append({
            "source_message_index": index,
            "placement": placement,
            "kind": "system_reminder",
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        })
    return normalized, audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--asset-root", type=Path, required=True,
                        help="host directory containing the converted assets/ directory")
    parser.add_argument("--image-prefix", required=True,
                        help="container path where asset-root is mounted")
    parser.add_argument("--text-only-smoke", action="store_true",
                        help="replace image markers with an explicit omission note for a text-only training smoke")
    args = parser.parse_args()

    allowed_roles = {"system", "user", "assistant", "tool_call", "tool_response", "tool"}
    rows = 0
    image_refs = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    audit_path = args.output.with_suffix(args.output.suffix + ".audit.jsonl")
    with args.input.open(encoding="utf-8") as source, args.output.open("w", encoding="utf-8") as target, \
            audit_path.open("w", encoding="utf-8") as audit:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            messages = row.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError(f"row {line_number} has no conversational messages")

            image_tokens = 0
            for message in messages:
                role = message.get("role")
                content = message.get("content")
                if role not in allowed_roles or not isinstance(content, str):
                    raise ValueError(f"row {line_number} has an invalid message: {message!r}")
                image_tokens += content.count("<image>")
                if role in {"tool_call", "tool_response"}:
                    json.loads(content)
            messages[:], system_reminder_records = merge_system_reminders(messages)

            # Swift Agent SFT needs a model response at the end. A terminal tool
            # acknowledgement with no following model turn is audit data, not a
            # prediction target; keep it in the source manifest and audit its ID/hash.
            omitted_terminal_tool_responses = []
            while messages and messages[-1].get("role") == "tool_response":
                content = messages.pop().get("content", "")
                response = json.loads(content)
                omitted_terminal_tool_responses.append({
                    "tool_use_id": response.get("tool_use_id"),
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                })
            if not messages or messages[-1].get("role") not in {"assistant", "tool_call"}:
                raise ValueError(f"row {line_number} does not end at a model response")

            images = row.get("images", [])
            if image_tokens != len(images):
                raise ValueError(
                    f"row {line_number} has {image_tokens} image markers and {len(images)} image paths"
                )
            omitted_images = 0
            if args.text_only_smoke:
                omitted_images = len(images)
                for message in messages:
                    message["content"] = message["content"].replace(
                        "<image>", "[Image omitted in text-only training smoke]"
                    )
                row.pop("images", None)
            container_images = []
            for image in ([] if args.text_only_smoke else images):
                path = Path(image)
                if path.is_absolute():
                    try:
                        relative = path.relative_to(args.asset_root)
                    except ValueError as exc:
                        raise ValueError(f"image path is outside asset root: {image}") from exc
                else:
                    relative = path
                if ".." in relative.parts or not (args.asset_root / relative).is_file():
                    raise FileNotFoundError(f"row {line_number} image asset is missing: {image}")
                container_images.append((Path(args.image_prefix) / relative).as_posix())
            if container_images:
                row["images"] = container_images
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
            audit.write(json.dumps({
                "row_index": rows,
                "source_line": line_number,
                "system_reminder_records": system_reminder_records,
                "text_only_smoke_images_omitted": omitted_images,
                "omitted_terminal_tool_responses": omitted_terminal_tool_responses,
                "final_training_role": messages[-1]["role"],
            }, ensure_ascii=False) + "\n")
            rows += 1
            image_refs += len(container_images)

    print(f"validated {rows} training rows; {image_refs} image references")
    print(f"dataset: {args.output}")
    print(f"preparation audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
