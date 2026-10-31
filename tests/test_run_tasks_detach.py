"""run_tasks detach_on_signal: signals leave containers running, a restarted runner re-adopts labelled containers,
default mode is unchanged. All docker/subprocess calls are mocked.

  python -I -m pytest -q tests/test_run_tasks_detach.py
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "e2e"))
import run_tasks as rt  # noqa: E402


class FakeProc:
    def __init__(self, polls=(0,), out="0\n"):
        self.polls, self.out, self.returncode = list(polls), out, 0
        self.killed = False

    def poll(self):
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

    def communicate(self, timeout=None):
        return self.out, ""

    def kill(self):
        self.killed = True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (rt.DETACH, rt.BATCH, rt.LABEL, rt.IMAGE, rt.GOAL_TEMPLATE)
        rt._stop.clear()
        rt.IMAGE, rt.GOAL_TEMPLATE = "img", "goal {id}"

    def tearDown(self):
        rt.DETACH, rt.BATCH, rt.LABEL, rt.IMAGE, rt.GOAL_TEMPLATE = self._saved
        rt._stop.clear()

    def detach(self):
        rt.DETACH, rt.BATCH, rt.LABEL = True, "abc", "run_tasks_batch=abc"


class SignalTests(Base):
    def test_detach_signal_does_not_stop_containers(self):
        self.detach()
        with mock.patch.object(rt, "stop_containers") as stop:
            rt.on_signal(15, None)
        stop.assert_not_called()
        self.assertTrue(rt._stop.is_set())

    def test_default_signal_stops_containers(self):
        called = threading.Event()
        with mock.patch.object(rt, "stop_containers", side_effect=lambda *a: called.set()):
            rt.on_signal(15, None)
            self.assertTrue(called.wait(5))
        self.assertTrue(rt._stop.is_set())


class FindContainersTests(Base):
    def test_parses_running_and_exited_only(self):
        self.detach()
        out = "i1 rt_abc_i1 running\ni2 rt_abc_i2 exited\ni3 rt_abc_i3 created\n \n"
        with mock.patch.object(rt.subprocess, "run", return_value=mock.Mock(stdout=out)) as run:
            found = rt.find_containers()
        self.assertEqual(found, {"i1": ("rt_abc_i1", "running"), "i2": ("rt_abc_i2", "exited")})
        self.assertIn("label=run_tasks_batch=abc", run.call_args[0][0])
        self.assertIn("-a", run.call_args[0][0])


class RunItemTests(Base):
    cfg = {"detach_on_signal": True}
    model_cfg = {"model_name": "m", "anthropic_base_url": "http://x"}

    def run_item(self, proc, adopt=None, popen_calls=None):
        item = {"id": "i1"}
        run_calls = []

        def fake_run(cmd, *a, **k):
            run_calls.append(cmd)
            return mock.Mock(stdout="", stderr="")

        with mock.patch.object(rt.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(rt.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(rt, "ItemWatchdog") as wd, \
                mock.patch.object(rt, "build_container", return_value=(["docker", "run", "-d"], False)) as build:
            wd.return_value.silence_start = 1e18
            wd.return_value.poll.return_value = None
            rec = rt.run_item(item, self.cfg, self.model_cfg, self.tmp, adopt)
        return rec, run_calls, popen, build

    def test_adopt_supervises_without_starting_and_logs(self):
        self.detach()
        rec, run_calls, popen, build = self.run_item(FakeProc(out="0\n"), adopt="rt_abc_i1")
        build.assert_not_called()
        self.assertEqual([c[:2] for c in run_calls if c[:2] == ["docker", "run"]], [])
        self.assertEqual(popen.call_args[0][0], ["docker", "wait", "rt_abc_i1"])
        self.assertIn(["docker", "rm", "-f", "rt_abc_i1"], run_calls)
        self.assertTrue(rec["adopted"])
        self.assertEqual(rec["exit_code"], 0)
        logged = [json.loads(l) for l in (self.tmp / "batch_log.jsonl").read_text().splitlines()]
        self.assertEqual([r["id"] for r in logged], ["i1"])

    def test_detach_start_uses_run_d_then_wait(self):
        self.detach()
        rec, run_calls, popen, build = self.run_item(FakeProc())
        self.assertEqual(run_calls[0], ["docker", "run", "-d"])
        self.assertEqual(popen.call_args[0][0], ["docker", "wait", "rt_abc_i1"])
        self.assertNotIn("adopted", rec)

    def test_signal_while_waiting_leaves_container(self):
        self.detach()
        proc = FakeProc(polls=(None,))
        proc.poll = lambda: rt._stop.set()   # signal arrives while the container is running; poll() is None
        rec, run_calls, popen, build = self.run_item(proc, adopt="rt_abc_i1")
        self.assertTrue(proc.killed)
        self.assertNotIn(["docker", "rm", "-f", "rt_abc_i1"], run_calls)
        self.assertFalse((self.tmp / "batch_log.jsonl").exists())

    def test_default_mode_cmd_is_rm_not_detached(self):
        cfg = {"resume_unfinished": False}
        item = {"id": "i1", "video": "/v.mp4", "clip_seconds": 5}
        with mock.patch.object(rt.rb, "move_to_attempts"), mock.patch.object(rt.os, "chmod"), \
                mock.patch.object(rt.os, "getuid", create=True, return_value=1000), \
                mock.patch.object(rt.os, "getgid", create=True, return_value=1000):
            cmd, _ = rt.build_container(item, cfg, self.model_cfg, self.tmp)
        self.assertIn("--rm", cmd)
        self.assertNotIn("-d", cmd)
        self.assertEqual(cmd[cmd.index("--name") + 1], f"rt_{rt.os.getpid()}_i1")
        self.assertIn("rb_item=i1", cmd)

    def test_detach_cmd_is_detached_without_rm_and_stable_name(self):
        self.detach()
        item = {"id": "i1", "video": "/v.mp4", "clip_seconds": 5}
        with mock.patch.object(rt.rb, "move_to_attempts"), mock.patch.object(rt.os, "chmod"), \
                mock.patch.object(rt.os, "getuid", create=True, return_value=1000), \
                mock.patch.object(rt.os, "getgid", create=True, return_value=1000):
            cmd, _ = rt.build_container(item, {"resume_unfinished": False}, self.model_cfg, self.tmp)
        self.assertIn("-d", cmd)
        self.assertNotIn("--rm", cmd)
        self.assertEqual(cmd[cmd.index("--name") + 1], "rt_abc_i1")
        self.assertIn("run_tasks_batch=abc", cmd)


class MainTests(Base):
    def run_main(self, detach, found):
        tmp = self.tmp
        (tmp / "model.json").write_text(json.dumps({"model_name": "m", "anthropic_base_url": "http://x"}))
        (tmp / "goal.txt").write_text("goal")
        (tmp / "items.jsonl").write_text("".join(json.dumps({"id": f"i{k}"}) + "\n" for k in range(3)))
        cfg = {"model_config": str(tmp / "model.json"), "image": "img", "goal_template": str(tmp / "goal.txt"),
               "output_base": str(tmp / "out"), "items": str(tmp / "items.jsonl"), "concurrent": 3,
               "launch_interval_s": 0, "detach_on_signal": detach}
        (tmp / "cfg.json").write_text(json.dumps(cfg))
        calls = []

        def fake_run_item(item, cfg, model_cfg, model_out, adopt=None):
            calls.append((item["id"], adopt))
            return {}

        with mock.patch.object(sys, "argv", ["run_tasks", "--config", str(tmp / "cfg.json")]), \
                mock.patch.object(rt, "wait_upstream", return_value=True), \
                mock.patch.object(rt.rb, "_cmd_out", return_value=""), \
                mock.patch.object(rt.signal, "signal"), \
                mock.patch.object(rt.signal, "SIGUSR1", 10, create=True), \
                mock.patch.object(rt, "find_containers", return_value=found) as find, \
                mock.patch.object(rt, "stop_containers") as stop, \
                mock.patch.object(rt, "run_item", side_effect=fake_run_item):
            self.assertEqual(rt.main(), 0)
        return calls, find, stop

    def test_detach_readopts_and_excludes_from_queue(self):
        calls, find, stop = self.run_main(True, {"i1": ("rt_x_i1", "running"), "gone": ("rt_x_gone", "running")})
        self.assertEqual(sorted(calls), [("i0", None), ("i1", "rt_x_i1"), ("i2", None)])
        self.assertEqual(len([c for c in calls if c[0] == "i1"]), 1)
        stop.assert_not_called()
        self.assertTrue(rt.LABEL.startswith("run_tasks_batch="))

    def test_default_mode_does_not_look_for_containers(self):
        calls, find, stop = self.run_main(False, {"i1": ("rt_x_i1", "running")})
        find.assert_not_called()
        self.assertEqual(sorted(calls), [("i0", None), ("i1", None), ("i2", None)])
        self.assertEqual(rt.LABEL, f"run_tasks_pid={rt.os.getpid()}")


if __name__ == "__main__":
    unittest.main()
