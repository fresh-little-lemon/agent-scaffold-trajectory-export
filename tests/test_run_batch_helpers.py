import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
import run_batch as rb


def test_mask_secrets_masks_secret_like_keys_recursively():
    cfg = {
        "api_key": "sk-123",
        "model_name": "m",
        "nested": {"ANTHROPIC_API_KEY": "abc", "Password": "p", "auth_token": "t", "x": 1},
        "list": [{"client_secret": "s"}],
        "empty_key": "EMPTY",
        "track_tokens": False,
        "max_tokens": 4096,
    }
    out = rb.mask_secrets(cfg)
    assert out["api_key"] == rb.MASKED
    assert out["model_name"] == "m"
    assert out["nested"] == {"ANTHROPIC_API_KEY": rb.MASKED, "Password": rb.MASKED,
                             "auth_token": rb.MASKED, "x": 1}
    assert out["list"] == [{"client_secret": rb.MASKED}]
    assert out["empty_key"] == "EMPTY"
    assert out["track_tokens"] is False and out["max_tokens"] == 4096
    assert cfg["api_key"] == "sk-123"  # input untouched


def test_merge_env_overrides_and_removes():
    base = {"A": "1", "B": "2"}
    out = rb.merge_env(base, {"A": None, "B": 3, "C": True, "D": False})
    assert out == {"B": "3", "C": "1", "D": "0"}
    assert base == {"A": "1", "B": "2"}
    assert rb.merge_env(base, None) == base


def test_env_args():
    assert rb.env_args({"A": "1", "http_proxy": ""}) == ["-e", "A=1", "-e", "http_proxy="]


def test_claude_code_env_defaults_and_overrides():
    env = rb.build_claude_code_env("qwen", "EMPTY", "http://127.0.0.1:9999")
    for k, v in rb.CLAUDE_CODE_DEFAULT_ENV.items():
        assert env[k] == v
    assert env["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "qwen"
    env2 = rb.build_claude_code_env("qwen", "EMPTY", "u",
                                    {"DISABLE_TELEMETRY": None, "CLAUDE_CODE_ATTRIBUTION_HEADER": "1"})
    assert "DISABLE_TELEMETRY" not in env2
    assert env2["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "1"


def test_common_env_tasks_decoupled_from_debug():
    env = rb.build_common_env({"context_window": None}, claude_debug=True, enable_tasks=False)
    assert env["CLAUDE_CODE_DEBUG_LOG_LEVEL"] == "verbose"
    assert "CLAUDE_CODE_ENABLE_TASKS" not in env
    assert env["TINI_KILL_PROCESS_GROUP"] == "1"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env
    env = rb.build_common_env({"context_window": 131072}, claude_debug=False, enable_tasks=True)
    assert env["CLAUDE_CODE_ENABLE_TASKS"] == "1"
    assert "CLAUDE_CODE_DEBUG_LOG_LEVEL" not in env
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "131072"


def test_output_mount_is_per_item():
    out = Path("/o/claude_code/m")
    root = rb.item_root_dir(out, "401")
    assert root == out / "items" / "401"
    assert rb.build_output_mount(root, "claude_code", "m") == [
        "-v", f"{root}:/app/outputs/claude_code/m"]
    mounts = rb.build_mounts(
        run_home=Path("/w/401"), html_id="401", rubric_view=Path("/tmp/r"), item_root=root,
        scaffolding="claude_code", model="m", prompt_file=Path("/tmp/p.md"),
        review_mcp_file=Path("/tmp/rv.json"), score_mcp_file=Path("/tmp/s.json"),
        proxy_script=Path("/e2e/api_proxy.py"), score_server_script=Path("/ss.py"),
        htmls_dir=Path("/h"), contexts_dir=Path("/c"))
    vols = [mounts[i + 1] for i, a in enumerate(mounts) if a == "-v"]
    assert not any(v.endswith(":/app/outputs") for v in vols)
    assert f"{root}:/app/outputs/claude_code/m" in vols
    assert f"{Path('/w/401')}:/home/node" in vols


def test_proxy_start_fragment():
    frag = rb.build_proxy_start("claude_code", "m", "401", "http://host.docker.internal:8001")
    assert "sleep 1" not in frag
    assert "TRACE_FILE=/app/outputs/claude_code/m/traces/401.jsonl" in frag
    assert "2>/app/outputs/claude_code/m/traces/log/401.proxy.log" in frag
    assert rb.PROXY_NOT_READY_MARKER in frag
    assert f"exit {rb.PROXY_NOT_READY_EXIT}" in frag
    assert "range(60)" in frag  # 15 s at 0.25 s per try


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_proxy_start_fragment_is_valid_bash():
    frag = rb.build_proxy_start("codex", "m", "1", "http://x:1")
    cmd = f"umask 0000 && mkdir -p /tmp/x && {frag}cd /tmp && echo agent"
    r = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr


def test_proxy_wait_snippet_detects_listener_and_absence():
    import socket
    frag = rb.build_proxy_start("x", "m", "1", "u", port=1, ready_timeout=1)
    wait_py = frag.split('python3 -c "', 1)[1].split('" ||', 1)[0]
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        ok = subprocess.run([sys.executable, "-c", wait_py.replace("127.0.0.1',1)", f"127.0.0.1',{port})")])
        assert ok.returncode == 0
    finally:
        srv.close()
    bad = subprocess.run([sys.executable, "-c", wait_py.replace("127.0.0.1',1)", f"127.0.0.1',{port})")])
    assert bad.returncode == 1


def test_classify_end():
    assert rb.classify_end(True, True) == "timeout"
    assert rb.classify_end(False, True) == "batch_stop"
    assert rb.classify_end(False, False) == "exited"


def test_archive_cmd_excludes_derived_files():
    cmd = rb.build_archive_cmd(Path("/o/m"), ["workspace/401", "items/401"], Path("/o/m/archives/401/t.tar.zst"))
    assert "--exclude='*.raw.jsonl'" in cmd
    assert "--exclude='*.reconstructed.jsonl'" in cmd
    assert "zstd -q -19 --long=27 -T4" in cmd
    assert "workspace/401 items/401" in cmd
    assert cmd.startswith("set -o pipefail;")


def test_image_version_tag():
    assert rb.image_version_tag("agent-scaffold-claude-code:2.1.310") == "2.1.310"
    assert rb.image_version_tag("reg:5000/agent") is None
    assert rb.image_version_tag("eval-interact-review-codex") is None


def test_unique_dir(tmp_path):
    base = tmp_path / "20260101T000000Z"
    assert rb.unique_dir(base) == base
    base.mkdir()
    assert rb.unique_dir(base) == tmp_path / "20260101T000000Z-1"


def test_move_to_attempts_keeps_everything(tmp_path):
    out = tmp_path / "claude_code" / "m"
    ws = out / "workspace" / "401"
    item = rb.item_root_dir(out, "401")
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "s.jsonl").write_text("x")
    (item / "traces").mkdir(parents=True)
    (item / "traces" / "401.jsonl").write_text("t")
    dest = rb.move_to_attempts(out, "401", ws, item, reason="test")
    assert dest.parent == out / "attempts" / "401"
    assert (dest / "workspace" / ".claude" / "s.jsonl").read_text() == "x"
    assert (dest / "item" / "traces" / "401.jsonl").read_text() == "t"
    assert json.loads((dest / "attempt_info.json").read_text())["reason"] == "test"
    assert not ws.exists() and not item.exists()
    assert rb.move_to_attempts(out, "401", ws, item) is None


def test_prepare_item_root_and_sync_score(tmp_path):
    item = tmp_path / "items" / "7"
    rb.prepare_item_root(item)
    assert (item / "traces" / "log").is_dir()
    assert (item / "results" / "scores" / ".progress").is_dir()
    central = tmp_path / "results" / "scores"
    assert rb.sync_score_to_central(item, central, "7") is False
    (item / "results" / "scores" / "7.json").write_text('{"s": 1}')
    assert rb.sync_score_to_central(item, central, "7") is True
    assert json.loads((central / "7.json").read_text()) == {"s": 1}


def test_proxy_not_ready(tmp_path):
    item = tmp_path / "items" / "9"
    rb.prepare_item_root(item)
    assert rb.proxy_not_ready(item, "9") is False
    (item / "traces" / "log" / "9.proxy.log").write_text(f"{rb.PROXY_NOT_READY_MARKER} port=9999\n")
    assert rb.proxy_not_ready(item, "9") is True
