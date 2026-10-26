import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from e2e.prepare_megatron_dataset import merge_system_reminders


def test_reminder_after_user_is_preserved_in_user_context():
    messages = [
        {"role": "user", "content": "task"},
        {"role": "system", "content": "reminder"},
        {"role": "assistant", "content": "answer"},
    ]

    normalized, audit = merge_system_reminders(messages)

    assert [(m["role"], m["content"]) for m in normalized] == [
        ("user", "task\n\nreminder"), ("assistant", "answer")
    ]
    assert audit[0]["placement"] == "preceding_user"


def test_reminder_after_tool_response_keeps_json_valid():
    messages = [
        {"role": "tool_response", "content": json.dumps({"tool_use_id": "t1", "content": "result"})},
        {"role": "system", "content": "reminder"},
        {"role": "assistant", "content": "answer"},
    ]

    normalized, audit = merge_system_reminders(messages)

    payload = json.loads(normalized[0]["content"])
    assert payload["content"] == "result"
    assert payload["system_reminders"] == ["reminder"]
    assert [m["role"] for m in normalized] == ["tool_response", "assistant"]
    assert audit[0]["placement"] == "preceding_tool_response"


def test_reminder_between_tool_call_and_result_is_attached_to_result():
    messages = [
        {"role": "tool_call", "content": json.dumps({"name": "Read", "arguments": {}})},
        {"role": "system", "content": "reminder"},
        {"role": "tool_response", "content": json.dumps({"tool_use_id": "t1", "content": "result"})},
    ]

    normalized, audit = merge_system_reminders(messages)

    call = json.loads(normalized[0]["content"])
    result = json.loads(normalized[1]["content"])
    assert call["name"] == "Read"
    assert result["system_reminders"] == ["reminder"]
    assert audit[0]["placement"] == "following_tool_response"


def test_user_followup_after_tool_response_stays_visible_in_result_context():
    messages = [
        {"role": "tool_response", "content": json.dumps({"tool_use_id": "t1", "content": "result"})},
        {"role": "user", "content": "continue from here"},
        {"role": "assistant", "content": "answer"},
    ]

    normalized, audit = merge_system_reminders(messages)

    payload = json.loads(normalized[0]["content"])
    assert payload["content"] == "result"
    assert payload["followup_user_messages"] == ["continue from here"]
    assert [m["role"] for m in normalized] == ["tool_response", "assistant"]
    assert audit[0]["kind"] == "user_message"
