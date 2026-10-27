#!/usr/bin/env python3
"""Run Claude Code on generic file-based tasks (one container per item), with full API capture.

First task: media2threejs. Each item is a source video; the container cuts a clip, installs the
media2threejs skill, and runs `claude -p "/goal <task + completion condition>"`. Capture, isolation,
attempt preservation and stopping reuse the verified pieces of run_batch.py (docs/run_layout.md):

  <output_base>/claude_code/<model>/
    workspace/<id>/         the agent's /home/node (transcripts under .claude/, the project under project/)
    items/<id>/             mounted at /app/outputs/claude_code/<model>: traces/, results/<id>/ (deliverables)
    attempts/ archives/ manifests/ batch_log.jsonl

    python3 e2e/run_tasks.py --config e2e/tasks/media2threejs_test10.json

Items file (JSONL): {"id", "video" (host path), "title", "clip_start", "clip_seconds", ...}; every field is
copied into batch_log.jsonl so each output maps back to its source video.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_batch as rb  # noqa: E402  (helpers: proxy start, env, attempts, archives)
from watchdog import ItemWatchdog, Registry  # noqa: E402

log = rb.log
REPO = Path(__file__).resolve().parent.parent
SCAFFOLD = "claude_code"
LABEL = f"run_tasks_pid={os.getpid()}"
_stop = threading.Event()
_lock = threading.Lock()


def stop_containers(extra: str = "") -> None:
    try:
        subprocess.run(f"docker ps --filter label={LABEL} {extra} -q | xargs -r -n1 -P20 "
                       f"timeout {rb.DOCKER_STOP_TIMEOUT + 60} docker stop -t {rb.DOCKER_STOP_TIMEOUT}",
                       shell=True, capture_output=True, text=True, timeout=rb.DOCKER_STOP_TIMEOUT + 120)
    except subprocess.TimeoutExpired:
        log.warning(f"docker stop did not return ({extra}); container may be stuck in the daemon")


def on_signal(signum, _frame):
    log.warning(f"signal {signum}: stop dispatching, stopping containers")
    _stop.set()
    threading.Thread(target=stop_containers, daemon=True).start()


def render_goal(template: str, item: dict, model: str, timeout_s: int = 5400) -> str:
    now = time.time()
    hhmm = lambda t: time.strftime("%H:%M", time.gmtime(t))  # noqa: E731
    fields = {**item, "model": model, "results": f"/app/outputs/{SCAFFOLD}/{model}/results/{item['id']}",
              "clip": f"/home/node/project/input/{item['id']}.mp4", "start_utc": hhmm(now),
              "first_deliverable_utc": hhmm(now + timeout_s * 0.55), "hard_stop_utc": hhmm(now + timeout_s)}
    text = template.format(**fields)
    if len(text) > 4000:
        raise ValueError(f"goal condition for {item['id']} is {len(text)} chars; /goal allows 4000")
    return text


def container_command(item: dict, model: str, upstream: str, skill: str, extra_flags: str = "",
                      max_resumes: int = 5, continue_session: bool = False) -> str:
    """Prepare the input, then run Claude in an attempt loop.

    Attempt 1 starts the session with the /goal; later attempts resume the same session (`--continue`) and set
    the goal again. After each attempt hooks/attempt_outcome.py decides: success, fail_fatal, fail_stalled, or
    resume (with backoff 60 s, 120 s, ... up to 15 min). The outcome of every attempt goes to <id>.outcomes.jsonl.
    """
    out = f"/app/outputs/{SCAFFOLD}/{model}"
    iid = item["id"]
    log_dir = f"{out}/traces/log"
    stream = f"{log_dir}/{iid}.claude.stream.jsonl"
    clip = (f"ffmpeg -nostdin -loglevel error -y -ss {float(item.get('clip_start', 0))} "
            f"-t {float(item['clip_seconds'])} -i /data/input/source.mp4 -an -c:v libx264 -preset veryfast "
            f"-crf 20 -pix_fmt yuv420p /home/node/project/input/{iid}.mp4")
    claude = f"claude --dangerously-skip-permissions {extra_flags}--output-format stream-json --verbose"
    return (
        "umask 0000 && "
        + rb.build_proxy_start(SCAFFOLD, model, iid, upstream)
        + f"mkdir -p /home/node/.claude/skills /home/node/project/input {out}/results/{iid} && "
        f"rm -rf /home/node/.claude/skills/{skill} && cp -r /opt/skills/{skill} /home/node/.claude/skills/ && "
        f"{{ [ -s /home/node/project/input/{iid}.mp4 ] || {clip}; }} && cd /home/node/project && "
        f"attempt={1 if continue_session else 0}; delay=60; decision=resume; "
        f"while [ \"$decision\" = resume ] && [ $attempt -le {max_resumes} ]; do "
        f"attempt=$((attempt+1)); start=$(stat -c %s {stream} 2>/dev/null || echo 0); "
        f"if [ $attempt -eq 1 ]; then {claude} -p \"$(cat {out}/goal.txt)\" >> {stream} 2>&1; "
        f"else sleep $delay; delay=$((delay*2 > 900 ? 900 : delay*2)); "
        f"{claude} --continue -p \"$(cat {out}/goal.txt)\" >> {stream} 2>&1; fi; rc=$?; "
        f"decision=$(python3 /opt/hooks/attempt_outcome.py {stream} $start {out}/results/{iid} "
        f"{log_dir}/{iid}.outcomes.jsonl $attempt $rc); "
        f"done; [ \"$decision\" = resume ] && decision=gave_up; "
        f"echo \"$decision\" > {log_dir}/{iid}.exit"
    )


def wait_upstream(url: str, timeout: int = 30) -> bool:
    host_port = url.split("//", 1)[-1].split("/", 1)[0]
    host, _, port = host_port.partition(":")
    host = "127.0.0.1" if host in {"host.docker.internal", "localhost"} else host
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection((host, int(port or 80)), timeout=3).close()
            return True
        except OSError:
            time.sleep(1)
    return False


def ffprobe_duration(path: Path) -> float | None:
    if not path.is_file():
        return None
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{path.parent}:/v:ro",
                        "--entrypoint", "ffprobe", IMAGE, "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", f"/v/{path.name}"],
                       capture_output=True, text=True, timeout=120)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def run_item(item: dict, cfg: dict, model_cfg: dict, model_out: Path) -> dict:
    iid = item["id"]
    model = model_cfg["model_name"]
    run_home = model_out / "workspace" / iid
    item_root = model_out / "items" / iid
    # An unfinished earlier run of this item (batch restarted) is continued in place: same workspace, same Claude
    # session (`--continue`), traces appended (a new proxy run id keeps its sidecars apart).
    sessions = list((run_home / ".claude" / "projects").glob("*/*.jsonl")) if run_home.is_dir() else []
    continue_session = bool(cfg.get("resume_unfinished", True) and sessions)
    if not continue_session:
        rb.move_to_attempts(model_out, iid, run_home, item_root, reason="rerun")
    for sub in (run_home / ".claude", item_root / "traces" / "log", item_root / "results" / iid):
        sub.mkdir(parents=True, exist_ok=True)
    for p in (run_home, item_root, item_root / "traces", item_root / "traces" / "log",
              item_root / "results", item_root / "results" / iid):
        os.chmod(p, 0o777)
    (item_root / "goal.txt").write_text(render_goal(GOAL_TEMPLATE, item, model, cfg.get("container_timeout", 5400)), encoding="utf-8")
    if cfg.get("stop_hook"):
        # Deterministic completion check next to /goal's conversation-only evaluator.
        (run_home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": f"python3 /opt/hooks/{Path(cfg['stop_hook']).name}"}]}]}}, indent=2))
    (item_root / "item.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")

    upstream = model_cfg["anthropic_base_url"].replace("127.0.0.1", "host.docker.internal").replace(
        "localhost", "host.docker.internal")
    env = {"HOME": "/home/node", "TINI_KILL_PROCESS_GROUP": "1",
           "http_proxy": "", "https_proxy": "", "HTTP_PROXY": "", "HTTPS_PROXY": "", "all_proxy": "", "ALL_PROXY": "",
           "npm_config_registry": cfg.get("npm_registry", "https://registry.npmmirror.com"),
           "PIP_INDEX_URL": cfg.get("pip_index", "https://mirrors.aliyun.com/pypi/simple/")}
    window = cfg.get("auto_compact_window") or model_cfg.get("context_window")
    if window:
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(cfg.get("autocompact_pct", 60))
    env["M2T_RESULTS_DIR"] = f"/app/outputs/{SCAFFOLD}/{model}/results/{iid}"
    env.update(rb.build_claude_code_env(model, model_cfg.get("api_key", "EMPTY"),
                                        f"http://127.0.0.1:{rb.IN_CONTAINER_PROXY_PORT}", cfg.get("claude_env")))
    cmd = ["docker", "run", "--rm", "--init", "--name", f"rt_{os.getpid()}_{iid}"[:63],
           "--label", LABEL, "--label", f"rb_item={iid}",
           "--add-host", "host.docker.internal:host-gateway", "--user", f"{os.getuid()}:{os.getgid()}",
           "--shm-size", "2g", "--cpus", str(cfg.get("cpus_per_item", 6)),
           *rb.env_args(env),
           "-v", f"{run_home}:/home/node", "-w", "/home/node",
           "-v", f"{item_root}:/app/outputs/{SCAFFOLD}/{model}",
           "-v", f"{item['video']}:/data/input/source.mp4:ro",
           "-v", f"{REPO / 'e2e' / 'api_proxy.py'}:/opt/api_proxy.py:ro",
           # npm's content-addressed cache is safe to share and turns repeated installs of the same
           # template packages into local copies (the mirror needs minutes per cold install).
           *(["-v", f"{cfg['npm_cache_dir']}:/home/node/.npm"] if cfg.get("npm_cache_dir") else []),
           *(["-v", f"{(REPO / cfg['stop_hook']).parent}:/opt/hooks:ro"] if cfg.get("stop_hook") else []),
           IMAGE, "bash", "-c", container_command(item, model, upstream, cfg.get("skill", "media2threejs"),
                                                  f"--effort {cfg['effort']} " if cfg.get("effort") else "",
                                                  cfg.get("max_resumes", 5), continue_session)]
    started = time.time()
    record = {"id": iid, **{k: v for k, v in item.items() if k != "id"}, "started_at": rb.utc_stamp(started)}
    record["continued"] = continue_session
    if _stop.is_set():
        # Submitted just before a stop signal: don't start a container stop_containers() has already missed.
        # Nothing is logged, so the item is picked up again (and continued in place) on the next start.
        log.info(f"[{iid}] not started: batch is stopping")
        return record
    log.info(f"[{iid}] {'continue' if continue_session else 'start'} ({item.get('title', '')[:40]})")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    stopped_by = "exited"
    # No fixed working-time limit: a heartbeat watchdog (e2e/watchdog.py) follows what the agent is doing and
    # how long its silences usually last. In "shadow" mode it only logs; the item is then stopped only after
    # `hang_minutes` without any progress, or at the `container_timeout` safety limit.
    wd = ItemWatchdog(iid, f"rt_{os.getpid()}_{iid}"[:63], item_root, run_home / "project", REGISTRY,
                      mode=cfg.get("watchdog_mode", "shadow"), floor_s=cfg.get("watchdog_floor_s", 600),
                      cap_s=cfg.get("watchdog_cap_s", 3600), model_floor_s=cfg.get("watchdog_model_floor_s", 7200))
    hang_s = cfg.get("hang_minutes", 240) * 60
    interval = 30
    try:
        while proc.poll() is None:
            time.sleep(interval)
            try:
                verdict = wd.poll(interval)
            except Exception as exc:  # the watchdog must never take the item down
                log.warning(f"[{iid}] watchdog error: {exc!r}")
                verdict = None
            idle = time.time() - wd.silence_start
            if verdict == "hung" or idle > hang_s or time.time() - started > cfg.get("container_timeout", 86400):
                stopped_by = "hung" if verdict == "hung" or idle > hang_s else "timeout"
                log.warning(f"[{iid}] {stopped_by}: no progress for {idle / 60:.0f} min, stopping")
                stop_containers(f"--filter label=rb_item={iid}")
                break
    finally:
        wd.close()
    try:
        _, err = proc.communicate(timeout=rb.DOCKER_STOP_TIMEOUT + 90)
    except subprocess.TimeoutExpired:
        err = "docker client did not exit after stop"
    if _stop.is_set() and stopped_by == "exited":
        stopped_by = "batch_stop"
    elapsed = time.time() - started
    results = item_root / "results" / iid
    exit_file = item_root / "traces" / "log" / f"{iid}.exit"
    record.update({
        "finished_at": rb.utc_stamp(), "elapsed_s": round(elapsed, 1), "exit_code": proc.returncode,
        "stopped_by": stopped_by, "claude_exit": exit_file.read_text().strip() if exit_file.is_file() else None,
        "proxy_not_ready": rb.PROXY_NOT_READY_MARKER in _read(item_root / "traces" / "log" / f"{iid}.proxy.log"),
        "docker_stderr_tail": (err or "")[-600:],
        "deliverables": sorted(p.name for p in results.iterdir()) if results.is_dir() else [],
        "whitebox_s": ffprobe_duration(results / "whitebox.mp4"),
        "compare_s": ffprobe_duration(results / "compare.mp4"),
    })
    outcomes = item_root / "traces" / "log" / f"{iid}.outcomes.jsonl"
    record["attempts"] = [json.loads(l) for l in _read(outcomes).splitlines() if l.strip()]
    decision = (record["claude_exit"] or "").strip()
    if record["whitebox_s"] and decision == "success":
        record["status"] = "success"
    elif stopped_by in ("hung", "timeout", "batch_stop"):
        record["status"] = stopped_by
    elif decision in ("fail_fatal", "fail_stalled", "gave_up"):
        record["status"] = decision
    else:
        record["status"] = "no_video"
    if cfg.get("drop_node_modules", True):
        # Regenerable from package.json/package-lock.json; ~100+ MB per project.
        for nm in run_home.glob("project/**/node_modules"):
            if nm.is_dir() and "node_modules" not in nm.parent.parts:
                shutil.rmtree(nm, ignore_errors=True)
                record.setdefault("dropped", []).append(str(nm.relative_to(run_home)))
    record["workspace_mb"] = round(_du(run_home) / 2**20, 1)
    record["item_mb"] = round(_du(item_root) / 2**20, 1)
    log.info(f"[{iid}] {record['status']} in {elapsed:.0f}s, attempts={len(record['attempts'])}, "
             f"whitebox={record['whitebox_s']}s, ws={record['workspace_mb']}MB")
    if cfg.get("archive_attempts"):
        rb.archive_attempt(model_out, iid)
    with _lock, (model_out / "batch_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _du(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def main() -> int:
    global IMAGE, GOAL_TEMPLATE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--limit", type=int, help="only the first N items")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    model_cfg = rb.load_model_config(cfg["model_config"])
    IMAGE = cfg["image"]
    GOAL_TEMPLATE = (REPO / cfg["goal_template"]).read_text(encoding="utf-8")
    model_out = Path(cfg["output_base"]) / SCAFFOLD / model_cfg["model_name"]
    model_out.mkdir(parents=True, exist_ok=True)
    if cfg.get("npm_cache_dir"):
        Path(cfg["npm_cache_dir"]).mkdir(parents=True, exist_ok=True)
        os.chmod(cfg["npm_cache_dir"], 0o777)
    items = [json.loads(line) for line in Path(cfg["items"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        items = items[:args.limit]
    done = set()
    if cfg.get("skip_done", True) and (model_out / "batch_log.jsonl").is_file():
        done = {json.loads(l)["id"] for l in (model_out / "batch_log.jsonl").read_text().splitlines()
                if l.strip() and json.loads(l).get("status") == "success"}
    items = [i for i in items if i["id"] not in done]
    if not wait_upstream(model_cfg["anthropic_base_url"]):
        log.error(f"model server {model_cfg['anthropic_base_url']} not reachable")
        return 2
    global REGISTRY
    REGISTRY = Registry(model_cfg["anthropic_base_url"].rstrip("/") + "/health")
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGUSR1, on_signal)
    manifest = {"created_at": rb.utc_stamp(), "argv": sys.argv, "host": socket.gethostname(),
                "repo_commit": rb._cmd_out(["git", "-C", str(REPO), "rev-parse", "HEAD"]),
                "repo_dirty": bool(rb._cmd_out(["git", "-C", str(REPO), "status", "--porcelain"])),
                "image": IMAGE, "image_id": rb._cmd_out(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE]),
                "skill_commit": rb._cmd_out(["docker", "image", "inspect", "--format",
                                             "{{index .Config.Labels \"media2threejs.commit\"}}", IMAGE]),
                "config": rb.mask_secrets(cfg), "model_config": rb.mask_secrets(model_cfg),
                "goal_template": GOAL_TEMPLATE, "items": len(items)}
    (model_out / "manifests").mkdir(exist_ok=True)
    (model_out / "manifests" / f"{rb.utc_stamp()}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"{len(items)} items, concurrent={cfg.get('concurrent', 4)}, image={IMAGE}, out={model_out}")
    with ThreadPoolExecutor(cfg.get("concurrent", 4)) as pool:
        pending = {}
        queue = iter(items)
        for item in queue:
            # A stop signal can arrive while the first `concurrent` items are still being launched (2 s apart, so
            # minutes at high concurrency). stop_containers() only stops what exists at that moment, so keep
            # launching here and the new containers run on unstopped (2026-10-27: a 64-item batch, paused 18 s
            # into its launch, kept 51 containers running for 20 min).
            if _stop.is_set():
                break
            pending[pool.submit(run_item, item, cfg, model_cfg, model_out)] = item["id"]
            if len(pending) >= cfg.get("concurrent", 4):
                break
            time.sleep(cfg.get("launch_interval_s", 3))
        if _stop.is_set():
            stop_containers()          # also what was launched between the signal and the check above
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in finished:
                iid = pending.pop(fut)
                try:
                    fut.result()
                except Exception as exc:  # keep the batch going; the item is logged as an error
                    log.exception(f"[{iid}] runner error")
                    with _lock, (model_out / "batch_log.jsonl").open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"id": iid, "status": "runner_error", "error": repr(exc)}) + "\n")
                if not _stop.is_set():
                    nxt = next(queue, None)
                    if nxt is not None:
                        pending[pool.submit(run_item, nxt, cfg, model_cfg, model_out)] = nxt["id"]
    log.info("batch complete")
    return 0


IMAGE = ""
GOAL_TEMPLATE = ""
REGISTRY = Registry(None)

if __name__ == "__main__":
    raise SystemExit(main())
