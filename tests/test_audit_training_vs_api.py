from audit_training_vs_api import (
    canonical_tool_content,
    digest,
    image_hashes,
    verify_image_assets,
    request_events,
    response_events,
    tool_result_event,
)


def test_tool_reference_content_is_not_equal_to_compatibility_placeholder():
    reference = [{"type": "tool_reference", "tool_name": "mcp__browser__click"}]
    placeholder = [{"type": "text", "text": "[Tool references removed - tool search not enabled]"}]

    assert digest(canonical_tool_content(reference)) != digest(canonical_tool_content(placeholder))
    assert tool_result_event("tool-1", reference) != tool_result_event("tool-1", placeholder)


def test_messages_response_retains_thinking_text_and_tool_input():
    response_id, events = response_events({
        "id": "msg-1",
        "content": [
            {"type": "thinking", "thinking": "check the page"},
            {"type": "text", "text": "Opening it."},
            {"type": "tool_use", "id": "tool-1", "name": "Bash",
             "input": {"command": "pwd"}},
        ],
    })

    assert response_id == "msg-1"
    assert [event["kind"] for event in events] == [
        "assistant_thinking", "assistant_text", "tool_call"
    ]
    assert events[-1]["arguments"] == {"command": "pwd"}


def test_request_history_keeps_tool_result_ids_and_content():
    events = request_events({"messages": [{
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "tool-1",
                     "content": [{"type": "tool_reference", "tool_name": "mcp__browser__click"}]}],
    }]})

    assert len(events) == 1
    assert events[0]["id"] == "tool-1"
    assert events[0]["content_sha256"] == digest([
        {"type": "tool_reference", "tool_name": "mcp__browser__click"}
    ])


def test_base64_image_and_extracted_asset_path_share_image_digest():
    import base64
    import hashlib

    raw = b"image bytes"
    image_digest = hashlib.sha256(raw).hexdigest()
    encoded = base64.b64encode(raw).decode("ascii")
    api_image = {"type": "image", "source": {"type": "base64", "data": encoded}}
    train_image = {"type": "image", "path": f"assets/{image_digest}.png", "data": "<image>"}

    assert canonical_tool_content(api_image) == canonical_tool_content(train_image)
    assert image_hashes(api_image) == image_hashes(train_image) == [image_digest]


def test_verify_image_asset_reads_extracted_file(tmp_path):
    raw = b"image bytes"
    import hashlib
    image_digest = hashlib.sha256(raw).hexdigest()
    asset = tmp_path / "assets" / f"{image_digest}.png"
    asset.parent.mkdir()
    asset.write_bytes(raw)

    assert verify_image_assets({"type": "image", "path": asset.relative_to(tmp_path).as_posix()}, tmp_path) == {
        "paths": [asset.relative_to(tmp_path).as_posix()],
        "sha256": [image_digest],
        "missing": [],
    }
