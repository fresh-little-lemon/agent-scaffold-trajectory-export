import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "e2e" / "tasks" / "hooks" / "attempt_outcome.py"


def run(tmp_path, events, deliver=False, refinement=True, prior=None):
    stream = tmp_path / "s.jsonl"
    stream.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    results = tmp_path / "r"
    results.mkdir(exist_ok=True)
    if deliver:
        for name, size in (("whitebox.mp4", 20000), ("compare.mp4", 20000), ("compare.jpg", 2000)):
            (results / name).write_bytes(b"x" * size)
        (results / "README.md").write_text("# model\n" + "modeled the burner and flask. " * 3
                                           + ("\n## Refinement\nfixed camera\n" if refinement else ""))
    outcomes = tmp_path / "o.jsonl"
    if prior:
        outcomes.write_text("".join(json.dumps(p) + "\n" for p in prior))
    out = subprocess.run([sys.executable, str(SCRIPT), str(stream), "0", str(results), str(outcomes), "1", "0"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def tool_turns(n):
    return [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}] * n


def test_success_when_deliverables_present(tmp_path):
    assert run(tmp_path, tool_turns(5), deliver=True, refinement=False) == "success"


def test_fatal_api_error(tmp_path):
    events = tool_turns(5) + [{"type": "result", "is_error": True, "api_error_status": 401, "result": "API Error: 401"}]
    assert run(tmp_path, events) == "fail_fatal"


def test_transient_error_resumes(tmp_path):
    events = tool_turns(5) + [{"type": "result", "is_error": True, "api_error_status": None,
                               "result": "API Error: The response stopped arriving."}]
    assert run(tmp_path, events) == "resume"


def test_two_idle_attempts_are_stalled(tmp_path):
    assert run(tmp_path, tool_turns(1)) == "resume"
    assert run(tmp_path, tool_turns(1), prior=[{"tool_calls": 0}]) == "fail_stalled"


def test_repeated_api_errors_are_not_a_stall(tmp_path):
    events = [{"type": "result", "is_error": True, "api_error_status": None, "result": "API Error: The operation timed out."}]
    assert run(tmp_path, events, prior=[{"tool_calls": 0, "is_error": True}]) == "resume"
