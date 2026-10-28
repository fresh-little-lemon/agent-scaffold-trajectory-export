"""run_tasks.main()'s scheduler with a changing concurrency_file: scale-down stops the newest items and requeues
them at the front, scale-up refills, every item finishes exactly once. run_item and stop_containers are fakes.

  python3 tests/test_run_tasks_scaling.py
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "e2e"))
import run_tasks as rt  # noqa: E402

N = 12
tmp = Path(tempfile.mkdtemp())
conc = tmp / "CONCURRENCY"
conc.write_text("6\n")
(tmp / "model.json").write_text(json.dumps({"model_name": "m", "anthropic_base_url": "http://x"}))
(tmp / "goal.txt").write_text("goal")
(tmp / "items.jsonl").write_text("".join(json.dumps({"id": f"i{k:02d}"}) + "\n" for k in range(N)))
cfg = {"model_config": str(tmp / "model.json"), "image": "img", "goal_template": str(tmp / "goal.txt"),
       "output_base": str(tmp / "out"), "items": str(tmp / "items.jsonl"), "concurrent": 6,
       "concurrency_file": str(conc), "launch_interval_s": 0, "max_concurrent": 32}
(tmp / "cfg.json").write_text(json.dumps(cfg))

stopped = {}                     # item id -> threading.Event, set when a fake container is stopped
events, peak = [], [0]
running = set()
lk = threading.Lock()


def fake_run_item(item, *_):
    iid = item["id"]
    ev = stopped.setdefault(iid, threading.Event())
    ev.clear()
    with lk:
        running.add(iid)
        peak[0] = max(peak[0], len(running))
        events.append(("start", iid))
    # finishes on its own after 4 s unless stopped
    was_stopped = ev.wait(4.0)
    with lk:
        running.discard(iid)
        events.append(("stop" if was_stopped else "done", iid))
    return {}


def fake_stop(extra=""):
    iid = extra.split("rb_item=")[-1] if "rb_item=" in extra else None
    for k, ev in list(stopped.items()):
        if iid is None or k == iid:
            ev.set()


if not hasattr(rt.signal, "SIGUSR1"):   # Windows
    rt.signal.SIGUSR1 = rt.signal.SIGINT
rt.run_item = fake_run_item
rt.stop_containers = fake_stop
rt.wait_upstream = lambda *_a, **_k: True
rt.rb._cmd_out = lambda *_a, **_k: ""
orig_wait = rt.wait
rt.wait = lambda fs, timeout=None, return_when=None: orig_wait(fs, timeout=min(timeout or 1, 0.5),
                                                                   return_when=return_when)


def driver():
    time.sleep(1.0)
    conc.write_text("2\n")       # scale down while 6 run
    time.sleep(1.5)
    with lk:
        snap = set(running)
    events.append(("snap_after_down", tuple(sorted(snap))))
    conc.write_text("5\n")


threading.Thread(target=driver, daemon=True).start()
sys.argv = ["run_tasks.py", "--config", str(tmp / "cfg.json")]
t0 = time.time()
rc = rt.main()
done = [i for e, i in events if e == "done"]
stops = [i for e, i in events if e == "stop"]
snap = next(i for e, i in events if e == "snap_after_down")
print("rc", rc, "elapsed", round(time.time() - t0, 1), "peak", peak[0])
print("stopped by scale-down:", stops)
print("running right after scale-down:", snap)
assert rc == 0
assert sorted(done) == [f"i{k:02d}" for k in range(N)], done          # every item finished exactly once
assert len(done) == len(set(done))
assert sorted(stops) == ["i02", "i03", "i04", "i05"], stops            # the 4 newest of the first 6
assert set(snap) == {"i00", "i01"}, snap                                # the oldest kept running
first_after = [i for e, i in events if e == "start"][6]
assert first_after in stops, first_after                                # requeued items go first
assert peak[0] <= 6
assert not rt._scaled_down
print("ok")
