#!/usr/bin/env python3
"""Heartbeat-based hang watchdog for one agent container (host side).

It does not guess from wall time. Every poll it reads what the agent is doing from its own records:
  * Claude Code's stream-json log: pending foreground tool calls (tool_use without tool_result) and background
    tasks (task_started without a terminal task_notification);
  * the capture proxy: model calls in flight (journal) and the bytes they stream (sidecars are flushed per chunk);
  * per-process CPU time from /proc for the container's process tree, attributed to Claude's child processes;
  * the newest file change in the agent's project directory (outputs of renders, builds, ...).

State per poll: model (waiting on the model server) | tool (a foreground tool runs) | background (only background
tasks run) | idle (none of these but Claude is alive) | exited.
Progress = any stream byte, new model call, new tool result, finished task, or project file change. CPU alone
is not progress (a busy loop burns CPU forever); it only marks a background task as busy rather than idle.

A silence is a stretch without progress. Its threshold is adaptive per item: S = clamp(mean + 4*sd of this item's
past silences that ended on their own, floor, cap), tracked per state. When a silence exceeds S the item becomes
suspicious and is escalated in levels with jittered waits U(1,2) * S * 2^level (jitter keeps many items that went
quiet together, e.g. during a server hiccup, from acting at the same moment). Any progress resets the level.
If more than half of the running items are suspicious at once, or the model server's /health fails, the cause is
global: levels freeze and nothing is escalated.

In "shadow" mode the watchdog only writes events to <item>/traces/log/<id>.watchdog.jsonl; in "act" mode it also
acts: level 1 kills a stuck background task's process tree (Claude gets the task's end and continues), level 2
ends Claude (the attempt loop resumes the session), level 3 reports the item hung.
"""
from __future__ import annotations

import json
import os
import random
import statistics
import subprocess
import threading
import time
from pathlib import Path

CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
TERMINAL_TASK = ("completed", "failed", "killed", "stopped", "cancelled", "error")


class Registry:
    """Shared view across items: how many are suspicious right now (to tell an item hang from a server stall)."""

    def __init__(self, health_url: str | None):
        self.lock = threading.Lock()
        self.suspicious: dict[str, bool] = {}
        self.health_url = health_url
        self._health = (0.0, True)

    def set(self, iid: str, value: bool | None) -> None:
        with self.lock:
            if value is None:
                self.suspicious.pop(iid, None)
            else:
                self.suspicious[iid] = value

    def global_stall(self) -> bool:
        with self.lock:
            n = len(self.suspicious)
            bad = sum(self.suspicious.values())
        return (n >= 4 and bad > n / 2) or not self.server_healthy()

    def server_healthy(self) -> bool:
        if not self.health_url:
            return True
        at, ok = self._health
        if time.time() - at > 60:
            try:
                import urllib.request
                urllib.request.urlopen(self.health_url, timeout=10).read()
                ok = True
            except Exception:
                ok = False
            self._health = (time.time(), ok)
        return ok


class ItemWatchdog:
    def __init__(self, iid: str, container: str, item_root: Path, project_dir: Path, registry: Registry,
                 mode: str = "shadow", floor_s: float = 600, cap_s: float = 3600, max_level: int = 3,
                 cpu_busy_cores: float = 0.2, model_floor_s: float = 7200):
        self.iid, self.container, self.mode = iid, container, mode
        self.item_root, self.project_dir, self.registry = item_root, project_dir, registry
        self.stream = item_root / "traces" / "log" / f"{iid}.claude.stream.jsonl"
        self.journal = item_root / "traces" / f"{iid}.jsonl.journal.jsonl"
        self.wire = item_root / "traces" / f"{iid}.jsonl.wire"
        self.events_path = item_root / "traces" / "log" / f"{iid}.watchdog.jsonl"
        self.floor_s, self.cap_s, self.max_level, self.cpu_busy = floor_s, cap_s, max_level, cpu_busy_cores
        # A model call can stream nothing for a long time: vLLM's DeepSeek tool parser sends a long tool argument
        # (a whole file) only once it is complete. Silence while a call is in flight gets this floor.
        self.model_floor_s = model_floor_s
        self.stream_pos = 0
        self.pending_tools: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.signature = None
        self.cpu_prev: dict[int, int] = {}
        self.history: dict[str, list[float]] = {}
        self.silence_start = time.time()
        self.silence_state = "start"
        self.level = 0
        self.next_check = None
        self.hung = False

    # ---- observation -------------------------------------------------------------------------------------
    def _read_stream(self) -> bool:
        """Consume new stream-json events; return True if any of them is progress."""
        progress = False
        try:
            with self.stream.open("rb") as f:
                f.seek(self.stream_pos)
                data = f.read()
        except OSError:
            return False
        end = data.rfind(b"\n") + 1
        self.stream_pos += end
        for line in data[:end].splitlines():
            if not line.startswith(b"{"):
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ, sub = e.get("type"), e.get("subtype")
            if typ == "assistant":
                for b in (e.get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_use":
                        cmd = (b.get("input") or {}).get("command") or (b.get("input") or {}).get("file_path") or ""
                        self.pending_tools[b.get("id")] = {"name": b.get("name"), "cmd": str(cmd)[:200],
                                                           "since": time.time()}
            elif typ == "user":
                content = (e.get("message") or {}).get("content")
                for b in content if isinstance(content, list) else []:
                    if b.get("type") == "tool_result" and self.pending_tools.pop(b.get("tool_use_id"), None):
                        progress = True
            elif typ == "system" and sub == "task_started":
                # Every Bash command is a task; long ones get moved to the background (task_updated).
                self.tasks[str(e.get("task_id") or e.get("uuid"))] = {
                    "desc": str(e.get("description") or "")[:200], "since": time.time(),
                    "background": str(e.get("is_backgrounded")).lower() == "true"}
            elif typ == "system" and sub == "task_updated":
                task = self.tasks.get(str(e.get("task_id")))
                if task is not None and "is_backgrounded" in str(e.get("patch")) and "true" in str(e.get("patch")).lower():
                    task["background"] = True
            elif typ == "system" and sub == "task_notification":
                status = str(e.get("status") or "").lower()
                if any(t in status for t in TERMINAL_TASK) or not status:
                    if self.tasks.pop(str(e.get("task_id") or e.get("uuid")), None) is not None:
                        progress = True
            elif typ == "result":
                progress = True
        return progress

    def _model_signature(self) -> tuple:
        """Changes whenever a model call starts/finishes or streams bytes (sidecars are flushed per chunk)."""
        size = self.journal.stat().st_size if self.journal.is_file() else 0
        wire = 0
        if self.wire.is_dir():
            for run in self.wire.iterdir():
                if run.is_dir():
                    for f in os.scandir(run):
                        if f.name.endswith(".response.bin"):
                            wire += f.stat().st_size
        return size, wire

    def _in_flight(self) -> int:
        """Model calls started by the current proxy process and not finished yet. Earlier proxy runs (a batch
        restart continued this item) can leave calls that never finish; they belong to a dead process."""
        run, starts = None, set()
        try:
            for line in self.journal.open(encoding="utf-8", errors="replace"):
                if '"proxy_start"' in line:
                    run, starts = json.loads(line).get("proxy_run_id"), set()
                elif '"exchange_start"' in line and run is not None:
                    e = json.loads(line)
                    if e.get("proxy_run_id") == run:
                        starts.add(e.get("n"))
        except (OSError, json.JSONDecodeError):
            return 0
        finished = set()
        try:
            for line in (self.item_root / "traces" / f"{self.iid}.jsonl").open(encoding="utf-8", errors="replace"):
                if run and run in line:
                    finished.add(json.loads(line).get("n"))
        except (OSError, json.JSONDecodeError):
            pass
        return len(starts - finished)

    def _project_mtime(self) -> float:
        """Newest directory mtime under the project: creating or removing a file updates its directory, so this
        sees new render frames, builds and outputs without stat-ing every file (the workspace is on NFS and a
        whole-video render writes thousands of frames)."""
        newest = 0.0
        for root, dirs, _files in os.walk(self.project_dir):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
            try:
                newest = max(newest, os.stat(root).st_mtime)
            except OSError:
                pass
        return newest

    def _proc_tree_cpu(self) -> tuple[float, list[int]]:
        """CPU cores used since the last poll by Claude's child processes (tools, background tasks)."""
        try:
            out = subprocess.run(["docker", "top", self.container, "-eo", "pid,ppid,args"], capture_output=True,
                                 text=True, timeout=30).stdout.splitlines()[1:]
        except (subprocess.TimeoutExpired, OSError):
            return 0.0, []
        procs = []
        for line in out:
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit():
                procs.append((int(parts[0]), int(parts[1]), parts[2]))
        claude = {p for p, _, a in procs if a.startswith("claude ") or "/claude " in a}
        children, frontier = set(), set(claude)
        while frontier:
            nxt = {p for p, pp, _ in procs if pp in frontier and p not in children and p not in claude}
            children |= nxt
            frontier = nxt
        total, now = 0, {}
        for pid in children:
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                ticks = int(fields[11]) + int(fields[12])
            except (OSError, IndexError, ValueError):
                continue
            now[pid] = ticks
            total += ticks - self.cpu_prev.get(pid, ticks)
        self.cpu_prev = now
        return total / CLK, sorted(children)

    # ---- decision -----------------------------------------------------------------------------------------
    def _threshold(self, state: str) -> float:
        floor = self.model_floor_s if state == "model" else self.floor_s
        h = self.history.get(state, [])
        if len(h) < 3:
            return floor
        return min(max(self.cap_s, floor), max(floor, statistics.mean(h) + 4 * statistics.pstdev(h)))

    def _log(self, kind: str, **fields) -> None:
        rec = {"at": round(time.time(), 1), "event": kind, "mode": self.mode, **fields}
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def poll(self, interval_s: float) -> str | None:
        """One observation step. Returns "hung" when the item should be reported hung (act mode only)."""
        now = time.time()
        progress = self._read_stream()
        sig = self._model_signature()
        if sig != self.signature:
            progress = progress or self.signature is not None
            self.signature = sig
        mtime = self._project_mtime()
        if mtime > getattr(self, "_mtime", 0):
            progress = progress or hasattr(self, "_mtime")
            self._mtime = mtime
        cpu_s, _children = self._proc_tree_cpu()
        cores = cpu_s / max(interval_s, 1)
        in_flight = self._in_flight()
        state = ("model" if in_flight else "tool" if self.pending_tools else
                 "background" if any(t.get("background") for t in self.tasks.values()) else "idle")
        if progress:
            silent = now - self.silence_start
            if silent >= interval_s * 2:
                self.history.setdefault(self.silence_state, []).append(silent)
                self._log("silence_end", state=self.silence_state, seconds=round(silent), level=self.level)
            self.silence_start, self.silence_state, self.level, self.next_check = now, state, 0, None
            self.registry.set(self.iid, False)
            return None
        if state != self.silence_state and self.level == 0:
            self.silence_state = state
        silent = now - self.silence_start
        busy = cores >= self.cpu_busy
        threshold = self._threshold(state) * (2 if busy and state in ("tool", "background") else 1)
        if self.next_check is None:
            if silent < threshold:
                self.registry.set(self.iid, False)
                return None
            self.next_check = now
        if now < self.next_check:
            return None
        self.registry.set(self.iid, True)
        if self.registry.global_stall():
            self._log("global_stall", state=state, seconds=round(silent))
            self.next_check = now + threshold
            return None
        action = self._act(self.level, state) if self.mode == "act" else "none"
        self._log("suspect", state=state, seconds=round(silent), level=self.level, threshold=round(threshold),
                  cpu_cores=round(cores, 3), pending_tools=list(self.pending_tools.values())[:3],
                  tasks=list(self.tasks.values())[:3], action=action)
        if self.level >= self.max_level:
            self.hung = True
            return "hung" if self.mode == "act" else None
        self.level += 1
        self.next_check = now + random.uniform(1, 2) * threshold * (2 ** (self.level - 1))
        return None

    def _act(self, level: int, state: str) -> str:
        if level == 1 and state in ("tool", "background"):
            # Kill Claude's child process trees (stuck tools / background tasks); Claude itself keeps going.
            cmd = "for p in $(pgrep -f 'claude --dangerously'); do pkill -TERM -P $p; done"
        elif level == 2:
            cmd = "pkill -TERM -f 'claude --dangerously'"  # the attempt loop resumes the session
        else:
            return "none"
        subprocess.run(["docker", "exec", self.container, "sh", "-c", cmd], capture_output=True, timeout=60)
        return cmd

    def close(self) -> None:
        self.registry.set(self.iid, None)
