import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transcript_to_calls import message_hash, rebuild_session, session_files  # noqa: E402
import calls_to_swift  # noqa: E402

SHAPE = {"system": [{"type": "text", "text": "You are an agent."}],
         "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]}
PARAMS = {"model": "m", "betas": ["b1"], "max_tokens": 100, "thinking": {"type": "adaptive"}}
ENV = {"role": "system", "content": [{"type": "text", "text": "# Environment"}]}
USER = {"role": "user", "content": [{"type": "text", "text": "do it"}]}


def assistant(msg_id, blocks, stop="tool_use"):
    return {"type": "assistant", "uuid": f"u-{msg_id}-{len(blocks)}",
            "message": {"id": msg_id, "role": "assistant", "model": "m", "content": blocks,
                        "stop_reason": stop, "usage": {"output_tokens": 1}}}


def write_session(tmp_path, records):
    path = tmp_path / ".claude" / "projects" / "-home-node" / "s1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def two_call_session():
    first_answer = [{"type": "thinking", "thinking": "plan", "signature": "s"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]
    result = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}]}}
    reply = {"role": "assistant", "content": first_answer}
    return [
        {"type": "api-request-shape", "shapeHash": "sh1", "shape": SHAPE},
        {"type": "api-request-blob", "hash": message_hash(USER), "message": USER},
        {"type": "api-request-blob", "hash": message_hash(ENV), "message": ENV},
        {"type": "api-request", "id": "c1", "base": None, "keep": 0, "shapeHash": "sh1", "params": PARAMS,
         "tail": [message_hash(USER), message_hash(ENV)], "querySource": "sdk",
         "cacheControl": [{"path": ["messages", 1, "content", 0], "value": {"type": "ephemeral"}}]},
        # the response arrives as two records sharing one message id (thinking, then tool_use)
        assistant("msg1", first_answer[:1]),
        assistant("msg1", first_answer[1:]),
        result,
        {"type": "api-request", "id": "c2", "base": "c1", "keep": 2, "shapeHash": "sh1", "params": PARAMS,
         "tail": [message_hash(reply), message_hash(result["message"])], "querySource": "sdk",
         "cacheControl": [{"path": ["messages", 3, "content", 0], "value": {"type": "ephemeral"}}]},
        assistant("msg2", [{"type": "text", "text": "done"}], stop="end_turn"),
    ]


def test_rebuild_chain_from_blobs_and_records(tmp_path):
    write_session(tmp_path, two_call_session())
    calls, manifest = rebuild_session(session_files(tmp_path), base_dir=tmp_path)

    assert [c["status"] for c in calls] == ["ok", "ok"]
    first, second = (c["request"] for c in calls)
    assert first["system"] == SHAPE["system"] and first["tools"] == SHAPE["tools"]
    assert first["max_tokens"] == 100 and "betas" not in first and calls[0]["betas"] == ["b1"]
    # cache_control placed on the system message keeps it as a block list ...
    assert first["messages"][1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # ... and once the marker moves on, Claude Code sends that system message as a plain string.
    assert second["messages"][1] == {"role": "system", "content": "# Environment"}
    assert second["messages"][2]["role"] == "assistant"
    assert second["messages"][3]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert [b["type"] for b in calls[0]["response"]["content"]] == ["thinking", "tool_use"]
    assert calls[1]["response"]["stop_reason"] == "end_turn"
    assert manifest["stats"]["messages_from_records"] == 2


def test_missing_message_is_reported_not_guessed(tmp_path):
    records = two_call_session()
    records[7]["tail"][1] = "f" * 64  # a tail hash no record or blob can produce
    write_session(tmp_path, records)
    calls, _ = rebuild_session(session_files(tmp_path), base_dir=tmp_path)
    assert calls[1]["status"] == "unresolved_messages"
    assert calls[1]["unresolved"] == ["f" * 64]


def test_call_without_response_is_kept_as_no_response(tmp_path):
    records = two_call_session()[:-1]  # process died while the second call was streaming
    write_session(tmp_path, records)
    calls, _ = rebuild_session(session_files(tmp_path), base_dir=tmp_path)
    assert [c["status"] for c in calls] == ["ok", "no_response"]


def test_swift_rows_train_only_each_calls_response(tmp_path):
    write_session(tmp_path, two_call_session())
    calls, _ = rebuild_session(session_files(tmp_path), base_dir=tmp_path)
    renderer = calls_to_swift.Renderer(tmp_path / "assets")

    row, meta = calls_to_swift.build_row([calls[1]], renderer)
    trained = [m for m in row["messages"] if m.get("loss")]
    assert trained == [{"role": "assistant", "content": "done", "loss": True}]
    assert [m["role"] for m in row["messages"]] == [
        "user", "system", "assistant", "tool_call", "tool_response", "assistant"]
    assert row["messages"][2] == {"role": "assistant", "content": "<think>plan</think>", "loss": False}
    assert row["messages"][4]["content"] == "a.txt"
    assert json.loads(row["tools"])[0]["function"]["name"] == "Bash"
    assert meta["call_ids"] == ["c2"]


def test_merge_chains_trains_every_response_once(tmp_path):
    write_session(tmp_path, two_call_session())
    calls, _ = rebuild_session(session_files(tmp_path), base_dir=tmp_path)
    assert calls_to_swift.extends(calls[0], calls[1])
    row, meta = calls_to_swift.build_row(calls, calls_to_swift.Renderer(tmp_path / "assets"))
    assert [m["role"] for m in row["messages"] if m.get("loss")] == ["assistant", "tool_call", "assistant"]
    assert meta["call_ids"] == ["c1", "c2"]
