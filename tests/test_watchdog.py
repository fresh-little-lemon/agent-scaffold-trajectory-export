import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
import watchdog  # noqa: E402


def make(tmp_path, mode="shadow"):
    root = tmp_path / "item"
    (root / "traces" / "log").mkdir(parents=True)
    (tmp_path / "project").mkdir()
    reg = watchdog.Registry(None)
    wd = watchdog.ItemWatchdog("X", "no-such-container", root, tmp_path / "project", reg, mode=mode,
                               floor_s=600, cap_s=3600)
    return wd, root / "traces" / "log" / "X.claude.stream.jsonl"


def append(stream, *events):
    with stream.open("a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_stream_events_drive_state(tmp_path):
    wd, stream = make(tmp_path)
    append(stream, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python3 render.py"}}]}},
        {"type": "system", "subtype": "task_started", "task_id": "k1", "is_backgrounded": False})
    assert wd._read_stream() is False
    assert "t1" in wd.pending_tools
    append(stream, {"type": "system", "subtype": "task_updated", "task_id": "k1", "patch": {"is_backgrounded": True}},
           {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}})
    assert wd._read_stream() is True          # the tool returned: progress
    assert wd.tasks["k1"]["background"] is True
    append(stream, {"type": "system", "subtype": "task_notification", "task_id": "k1", "status": "completed"})
    assert wd._read_stream() is True and not wd.tasks


def test_partial_last_line_is_kept_for_next_poll(tmp_path):
    wd, stream = make(tmp_path)
    stream.write_bytes(b'{"type": "result"')
    assert wd._read_stream() is False
    with stream.open("ab") as f:
        f.write(b"}\n")
    assert wd._read_stream() is True


def test_threshold_adapts_to_this_items_silences(tmp_path):
    wd, _ = make(tmp_path)
    assert wd._threshold("tool") == 600                        # floor until there is history
    wd.history["tool"] = [900, 1000, 1100]
    assert 1000 < wd._threshold("tool") <= 3600                # mean + 4 sd, capped
    wd.history["tool"] = [5000, 9000, 20000]
    assert wd._threshold("tool") == 3600


def test_shadow_mode_never_reports_hung(tmp_path, monkeypatch):
    wd, _ = make(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(watchdog.time, "time", lambda: clock[0])
    wd.silence_start = clock[0]
    for _ in range(40):
        clock[0] += 900
        assert wd.poll(30) is None
    events = [json.loads(l) for l in wd.events_path.read_text().splitlines()]
    assert any(e["event"] == "suspect" and e["action"] == "none" for e in events)
    assert wd.hung is True                                     # it would have been hung, but only logged


def test_global_stall_freezes_escalation(tmp_path):
    reg = watchdog.Registry(None)
    for i in range(6):
        reg.set(f"i{i}", True)
    assert reg.global_stall() is True
    reg.set("i0", False)
    reg.set("i1", False)
    reg.set("i2", False)
    assert reg.global_stall() is False


def test_in_flight_counts_only_the_current_proxy_run(tmp_path):
    wd, _ = make(tmp_path)
    journal = wd.journal
    lines = [{"event": "proxy_start", "proxy_run_id": "old"}, {"event": "exchange_start", "proxy_run_id": "old", "n": 1},
             {"event": "proxy_start", "proxy_run_id": "new"}, {"event": "exchange_start", "proxy_run_id": "new", "n": 1},
             {"event": "exchange_start", "proxy_run_id": "new", "n": 2}]
    journal.write_text("".join(json.dumps(l) + "\n" for l in lines))
    (wd.item_root / "traces" / "X.jsonl").write_text(json.dumps({"proxy_run_id": "new", "n": 1}) + "\n")
    assert wd._in_flight() == 1               # old run's unfinished call is ignored; new n=2 is in flight


def test_model_state_gets_its_own_floor(tmp_path):
    wd, _ = make(tmp_path)
    assert wd._threshold("model") == wd.model_floor_s == 7200
    assert wd._threshold("tool") == 600
