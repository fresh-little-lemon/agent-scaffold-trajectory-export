import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from claude_jsonl_to_swift import Converter, collect_paths


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


class ConverterTests(unittest.TestCase):
    def test_raw_export_preserves_secrets_by_default_and_redaction_is_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Converter(Path(temp) / "raw")
            self.assertEqual(raw.redact("Bearer abcdefghijklmnop"), "Bearer abcdefghijklmnop")
            redacted = Converter(Path(temp) / "redacted", redact_secrets=True)
            self.assertEqual(redacted.redact("Bearer abcdefghijklmnop"), "[REDACTED_SECRET]")

    def test_control_only_session_is_kept_in_manifest_not_training_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / ".claude" / "projects" / "p" / "subagents" / "workflows" / "wf_x" / "journal.jsonl"
            write_jsonl(session, [{
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "workflow control event",
                "sessionId": "s",
            }])

            train_path, manifest_path = Converter(root / "out").convert_paths([session])

            self.assertEqual(train_path.read_text(encoding="utf-8"), "")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(manifest["training_row_written"])
            self.assertEqual(manifest["queue_operation_count"], 1)
            self.assertTrue(any("empty conversational session" in warning for warning in manifest["warnings"]))

    def test_unknown_named_trace_tools_are_preserved_as_function_schema(self):
        normalized = Converter.normalize_tools([{"type": "computer_20250124", "name": "computer"}])
        self.assertEqual(normalized[0]["function"]["name"], "computer")
        self.assertEqual(normalized[0]["function"]["parameters"]["type"], "object")

    def test_nested_prompt_snapshot_schema_is_unwrapped(self):
        normalized = Converter.normalize_tools([{
            "name": "Read", "description": "read",
            "schema": {"name": "Read", "description": "read",
                        "input_schema": {"type": "object", "properties": {"file": {"type": "string"}}}},
        }])
        self.assertEqual(normalized[0]["function"]["parameters"]["properties"]["file"]["type"], "string")

    def test_plaintext_thinking_is_kept_and_deduplicated_by_its_own_block_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.jsonl"
            write_jsonl(session, [
                {"type": "assistant", "uuid": "a1", "message": {"id": "m1", "content": [
                    {"type": "thinking", "thinking": "Let me inspect"},
                ]}},
                {"type": "assistant", "uuid": "a2", "message": {"id": "m1", "content": [
                    {"type": "thinking", "thinking": "Let me inspect the page"},
                    {"type": "text", "text": "I will inspect it."},
                ]}},
                {"type": "assistant", "uuid": "a3", "message": {"id": "m1", "content": [
                    {"type": "text", "text": "I will inspect it."},
                ]}},
            ])
            row, manifest = Converter(root / "out").convert_session(session, sample_index=0)
            self.assertEqual(row["messages"], [
                {"role": "assistant", "content": "<think>Let me inspect the page</think>"},
                {"role": "assistant", "content": "I will inspect it."},
            ])
            self.assertEqual(manifest["thinking_block_count"], 2)
            self.assertEqual(manifest["opaque_thinking_block_count"], 0)
            self.assertEqual(manifest["thinking_plaintext_block_count"], 2)

    def test_dedup_trace_alignment_and_sidechain_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "workspace" / "14" / ".claude" / "projects" / "-home-node" / "s.jsonl"
            trace_root = root / "traces"
            first_text = "Done"
            second_text = "Done now"
            rows = [
                {"type": "user", "uuid": "u1", "sessionId": "s", "message": {"role": "user", "content": "Do task"}},
                {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": "s", "message": {"id": "m1", "content": [
                    {"type": "thinking", "thinking": "", "signature": "opaque"},
                ]}},
                {"type": "assistant", "uuid": "a1-text", "parentUuid": "a1", "sessionId": "s", "message": {"id": "m1", "content": [
                    {"type": "text", "text": first_text},
                    {"type": "text", "text": second_text},
                ]}},
                {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "sessionId": "s", "message": {"id": "m1", "content": [
                    {"type": "text", "text": "Done completely"},
                    {"type": "text", "text": second_text},
                    {"type": "tool_use", "id": "t1", "name": "click", "input": {"x": 1}},
                ]}},
                {"type": "user", "uuid": "r1", "parentUuid": "a2", "sessionId": "s", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
                ]}, "sourceToolAssistantUUID": "a2", "toolUseResult": {"agentId": "agent-x"}},
                {"type": "user", "uuid": "r1-copy", "parentUuid": "a2", "sessionId": "s", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
                ]}, "sourceToolAssistantUUID": "a2", "toolUseResult": {"agentId": "agent-x"}},
                {"type": "user", "uuid": "r1-conflict", "parentUuid": "a2", "sessionId": "s", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "different"}
                ]}, "sourceToolAssistantUUID": "a2", "toolUseResult": {"agentId": "agent-x"}},
                {"type": "assistant", "uuid": "sc", "parentUuid": "u1", "isSidechain": True, "sessionId": "s", "message": {"id": "side", "content": [{"type": "text", "text": "subagent"}]}}
            ]
            write_jsonl(session, rows)
            write_jsonl(trace_root / "14.jsonl", [{
                "n": 9,
                "request": {"model": "test-model", "system": "visible system", "tools": [{"name": "click", "input_schema": {"type": "object", "properties": {}}}]},
                "response": {"id": "m1", "model": "test-model", "choices": [{"message": {"tool_calls": [{"id": "t1"}]}}]},
            }])
            converter = Converter(root / "out")
            row, manifest = converter.convert_session(session, sample_index=0)
            roles = [m["role"] for m in row["messages"]]
            self.assertEqual(roles, ["user", "assistant", "assistant", "tool_call", "tool_response", "tool_response"])
            self.assertEqual(row["messages"][1]["content"], "Done completely")
            self.assertEqual(row["messages"][2]["content"], second_text)
            call_index = roles.index("tool_call")
            self.assertEqual(manifest["messages"][call_index]["agent_id"], "agent-x")
            self.assertNotIn("system", row)
            self.assertEqual(manifest["opaque_thinking_block_count"], 1)
            self.assertEqual(manifest["sidechain_record_count_excluded"], 1)
            self.assertTrue(any("conflicting duplicate" in warning for warning in manifest["warnings"]))

    def test_directory_scan_excludes_trace_jsonl(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "workspace" / "2" / ".claude" / "projects" / "p" / "s.jsonl"
            trace = root / "workspace" / "2" / "traces" / "2.jsonl"
            write_jsonl(session, [{"type": "user", "uuid": "u", "message": {"content": "x"}}])
            write_jsonl(trace, [{"request": {}, "response": {}}])
            found = collect_paths([str(root / "workspace")])
            self.assertEqual(found, [session.resolve()])

    def test_control_prefix_and_dedicated_workflow_compaction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workflow = root / ".claude" / "projects" / "p" / "subagents" / "workflows" / "wf_x" / "agent-a1.jsonl"
            write_jsonl(workflow, [
                {"type": "mode", "sessionId": "s"},
                {"type": "system", "subtype": "compact_boundary", "isSidechain": True,
                 "compactMetadata": {"trigger": "auto"}},
                {"type": "user", "uuid": "u", "sessionId": "s", "agentId": "a1",
                 "isSidechain": True, "isCompactSummary": True,
                 "message": {"role": "user", "content": [{"type": "text", "text": "summary"}]}},
                {"type": "assistant", "uuid": "a", "sessionId": "s", "agentId": "a1",
                 "isSidechain": True,
                 "message": {"id": "m", "content": [{"type": "text", "text": "result"}]}},
            ])
            self.assertEqual(collect_paths([str(workflow)]), [workflow.resolve()])
            row, manifest = Converter(root / "out").convert_session(workflow, sample_index=0)
            self.assertEqual([m["content"] for m in row["messages"]], ["summary", "result"])
            self.assertEqual(manifest["branch_type"], "workflow_agent")
            self.assertEqual(manifest["agent_id"], "a1")
            self.assertEqual(manifest["compact_summary_record_count"], 1)
            self.assertEqual(manifest["compact_boundary_record_count"], 1)
            self.assertEqual(manifest["compact_boundaries"][0]["compact_metadata"]["trigger"], "auto")
            self.assertTrue(manifest["messages"][0]["is_compact_summary"])
            self.assertEqual(manifest["sidechain_record_count_included"], 3)
            self.assertEqual(manifest["sidechain_record_count_excluded"], 0)

    def test_rendered_background_notifications_are_trained_and_queue_events_audited(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / ".claude" / "projects" / "p" / "subagents" / "agent-x.jsonl"
            payload = (
                "<task-notification><task-id>task-1</task-id>"
                "<tool-use-id>toolu-123</tool-use-id>"
                "<summary>Monitor event: clean_v2 pipeline progress</summary>"
                "<event>multiprocessing.pool.RemoteTraceback: failed</event>"
                "</task-notification>"
            )
            write_jsonl(session, [
                {"type": "user", "uuid": "u1", "sessionId": "s", "agentId": "agent-x",
                 "isSidechain": True, "message": {"role": "user", "content": "Run the job"}},
                {"type": "queue-operation", "operation": "enqueue", "timestamp": "t1",
                 "sessionId": "s", "content": payload},
                {"type": "attachment", "uuid": "att1", "timestamp": "t1",
                 "isSidechain": True, "agentId": "agent-x",
                 "renderedRole": "system",
                 "rendered": [{"content": "<system-reminder>" + payload + "</system-reminder>"}],
                 "renderedInHumanTurn": [{"content": "<system-reminder>human turn: " + payload + "</system-reminder>"}],
                 "attachment": {"type": "queued_command", "prompt": payload,
                                "delivery_id": "delivery-1", "source_uuid": "source-1"}},
                {"type": "attachment", "uuid": "att1-copy", "timestamp": "t1",
                 "isSidechain": True, "agentId": "agent-x",
                 "renderedRole": "system",
                 "rendered": [{"content": "<system-reminder>" + payload + "</system-reminder>"}],
                 "attachment": {"type": "queued_command", "prompt": payload,
                                "delivery_id": "delivery-1", "source_uuid": "source-1"}},
                {"type": "assistant", "uuid": "a1", "sessionId": "s", "agentId": "agent-x",
                 "isSidechain": True,
                 "message": {"id": "m1", "content": [{"type": "text", "text": "Job failed."}]}},
            ])
            row, manifest = Converter(root / "out").convert_session(session, sample_index=0)
            self.assertEqual([m["role"] for m in row["messages"]], ["user", "system", "assistant"])
            self.assertIn("multiprocessing.pool.RemoteTraceback", row["messages"][1]["content"])
            self.assertNotIn("human turn:", row["messages"][1]["content"])
            self.assertEqual(manifest["background_task_notification_count"], 1)
            self.assertEqual(manifest["background_task_notification_duplicate_count"], 1)
            self.assertEqual(manifest["background_task_notifications"][0]["task_id"], "task-1")
            self.assertEqual(manifest["queue_operation_task_notification_count"], 1)
            self.assertEqual(manifest["queue_operation_events"][0]["task_id"], "task-1")
            self.assertEqual(manifest["queue_operation_events"][0]["tool_use_id"], "toolu-123")
            self.assertEqual(manifest["queue_operation_events"][0]["summary"], "Monitor event: clean_v2 pipeline progress")
            self.assertEqual(manifest["queue_operation_events"][0]["content"], payload)
            self.assertEqual(manifest["queue_operation_events"][0]["visibility"], "visible_via_rendered_attachment")
            self.assertEqual(manifest["queue_operation_events"][0]["linked_attachment_lines"], [3])
            self.assertEqual(manifest["queue_operation_events"][0]["training_message_indices"], [1])
            self.assertEqual(manifest["attachment_render_variants"][0]["queue_operation_lines"], [2])
            self.assertEqual(manifest["messages"][1]["queue_operation_lines"], [2])
            self.assertEqual(manifest["queue_operation_counts"], {"enqueue": 1})
            self.assertEqual(manifest["rendered_attachment_message_count_included"], 1)
            self.assertEqual(manifest["duplicate_rendered_attachment_message_count"], 1)
            self.assertEqual(manifest["messages"][1]["rendered_variant"], "rendered")

    def test_control_event_sidecar_keeps_raw_attachment_and_queue_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.jsonl"
            payload = "<task-notification><task-id>task-2</task-id><summary>done</summary></task-notification>"
            write_jsonl(session, [
                {"type": "user", "uuid": "u", "sessionId": "s",
                 "message": {"role": "user", "content": "start"}},
                {"type": "queue-operation", "operation": "enqueue", "timestamp": "t1",
                 "sessionId": "s", "content": payload, "deliveryId": "d1"},
                {"type": "attachment", "uuid": "a", "timestamp": "t2",
                 "attachment": {"type": "queued_command", "prompt": payload,
                                "delivery_id": "d1"},
                 "renderedRole": "system", "rendered": [{"content": "<system-reminder>" + payload + "</system-reminder>"}]},
            ])
            converter = Converter(root / "out")
            converter.convert_paths([session])
            events = [json.loads(line) for line in (root / "out" / "control-events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([item["record"]["type"] for item in events], ["queue-operation", "attachment"])
            self.assertEqual(events[0]["record"]["operation"], "enqueue")
            self.assertEqual(events[1]["record"]["renderedRole"], "system")

    def test_prompt_snapshot_supplies_system_and_tools_without_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.jsonl"
            tools = [{"name": "Read", "description": "read", "input_schema":
                      {"type": "object", "properties": {}}}]
            write_jsonl(session, [
                {"type": "attachment", "uuid": "snap", "timestamp": "t0",
                 "attachment": {"type": "prompt_snapshot",
                                "systemPrompt": ["system A", "system B"],
                                "tools": tools, "contextRendering": "announced"}},
                {"type": "user", "uuid": "u", "message": {"role": "user", "content": "go"}},
            ])
            row, manifest = Converter(root / "out").convert_session(session, sample_index=0)
            self.assertEqual(row["system"], "system A\nsystem B")
            self.assertIn('"name":"Read"', row["tools"])
            self.assertEqual(manifest["system_source"], "prompt_snapshot attachment")
            self.assertEqual(manifest["tools_source"], "prompt_snapshot attachment")
            self.assertEqual(manifest["prompt_snapshot_count"], 1)

    def test_prompt_snapshot_schema_is_normalized_as_tool_parameters(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.jsonl"
            write_jsonl(session, [{
                "type": "attachment", "uuid": "snap", "attachment": {
                    "type": "prompt_snapshot", "tools": [{
                        "name": "Workflow", "description": "run", "schema": {
                            "type": "object", "properties": {"script": {"type": "string"}}
                        }
                    }]
                }
            }])
            row, _ = Converter(root / "out").convert_session(session, sample_index=0)
            parsed = json.loads(row["tools"])
            self.assertEqual(parsed[0]["function"]["parameters"]["properties"]["script"]["type"], "string")

    def test_trace_is_ignored_by_transcript_converter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.jsonl"
            trace = root / "trace.jsonl"
            write_jsonl(session, [
                {"type": "user", "uuid": "u", "message": {"role": "user", "content": "go"}},
            ])
            write_jsonl(trace, [
                {"n": 1, "request": {"system": "trace system", "tools": [
                    {"name": "Read", "input_schema": {"type": "object", "properties": {}}}
                ]}, "response": {}},
            ])
            row, manifest = Converter(root / "out").convert_session(session, sample_index=0)
            self.assertNotIn("system", row)
            self.assertNotIn("tools", row)
            self.assertEqual(manifest["system_source"], "not present in transcript; unavailable")
            self.assertEqual(manifest["tools_source"], "not present in transcript; unavailable")

    def test_unmatched_queue_payload_is_retained_and_marked_unresolved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.jsonl"
            payload = (
                "<task-notification><task-id>task-9</task-id>"
                "<summary>Monitor event: still running</summary>"
                "</task-notification>"
            )
            write_jsonl(session, [
                {"type": "queue-operation", "operation": "enqueue", "content": payload},
                {"type": "user", "uuid": "u", "message": {"role": "user", "content": "continue"}},
            ])
            row, manifest = Converter(root / "out").convert_session(session, sample_index=0)
            event = manifest["queue_operation_events"][0]
            self.assertEqual(event["content"], payload)
            self.assertEqual(event["visibility"], "unresolved_without_matching_rendered_message")
            self.assertEqual(event["training_message_indices"], [])
            self.assertEqual([item["role"] for item in row["messages"]], ["user"])

    def test_image_references_are_idempotent_and_occurrences_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            converter = Converter(Path(temp))
            source = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(b"same-image").decode()}}
            converted = converter.convert_images(source)
            again = converter.convert_images(converted)
            converter.convert_images(source)
            self.assertEqual(converted, again)
            self.assertEqual(converter.image_paths, [converted["path"], converted["path"]])
            self.assertEqual(len(list((Path(temp) / "assets").glob("*"))), 1)

    def test_embedded_pdf_document_is_extracted_from_tool_result(self):
        with tempfile.TemporaryDirectory() as temp:
            converter = Converter(Path(temp))
            raw_pdf = b"%PDF-1.7 test payload"
            source = {"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.b64encode(raw_pdf).decode(),
            }}
            converted = converter.convert_assets({"content": [source]})
            document = converted["content"][0]
            self.assertEqual(document["type"], "document")
            self.assertEqual(document["source"]["media_type"], "application/pdf")
            asset = Path(temp) / document["source"]["path"]
            self.assertEqual(asset.suffix, ".pdf")
            self.assertEqual(asset.read_bytes(), raw_pdf)
            self.assertNotIn("data", document["source"])
            self.assertEqual(converter.image_paths, [])
            self.assertEqual(converter.document_paths, [document["source"]["path"]])

    def test_claude_file_base64_image_is_extracted(self):
        with tempfile.TemporaryDirectory() as temp:
            converter = Converter(Path(temp))
            raw_image = b"fake-png-payload"
            source = {"type": "image", "file": {
                "type": "image/png", "base64": base64.b64encode(raw_image).decode(),
            }}
            converted = converter.convert_assets(source)
            self.assertEqual(converted["type"], "image")
            self.assertEqual(converted["data"], "<image>")
            asset = Path(temp) / converted["path"]
            self.assertEqual(asset.read_bytes(), raw_image)
            self.assertEqual(converter.image_paths, [converted["path"]])


if __name__ == "__main__":
    unittest.main()
