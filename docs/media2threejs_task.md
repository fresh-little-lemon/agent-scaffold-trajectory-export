# media2threejs task: chemistry videos → white-model Three.js reconstructions

Claude Code 2.1.310 runs in `/goal` mode with the
[media2threejs](https://github.com/promptwhisper/media2threejs) skill (commit `1c14ada`) on clips from
`/slow_share2/chenmf/lwm/datasets/chem_video_v1/`, served by DeepSeek-V4.1-Flash on the A800. Each item delivers a
white-model (clay) render of the clip, a side-by-side comparison with the reference, the Three.js source, and, from
the run, an API trace and training data. Started 2026-10-26.

## Pieces

| Piece | File |
|---|---|
| image (lean, 2.65 GB): Claude Code binary, node 20, python 3.11 + playwright 1.60 (chromium-headless-shell-1223), numpy, opencv, pillow, ffmpeg, the skill at `/opt/skills/media2threejs` | `docker/media2threejs.Dockerfile` → `agent-scaffold-media2threejs:lean` |
| item selection: experiment videos from the dataset index, a 10 s clip window at 35% of the duration | `e2e/tasks/select_chem_videos.py` → `e2e/tasks/chem_test10.jsonl`, `chem_scale.jsonl` |
| task text, rendered per item into the `/goal` condition (< 4000 chars) | `e2e/tasks/media2threejs_goal.txt` |
| runner: one container per item, capture proxy, per-item mounts, attempts kept, Stop hook | `e2e/run_tasks.py --config e2e/tasks/media2threejs_*.json` |
| deterministic completion check | `e2e/tasks/hooks/require_deliverables.py` |
| post-processing: calls, training rows, audit, metrics, errors, index | `e2e/tasks/postprocess_run.py` |
| review page | `e2e/tasks/build_review_site.py` + `e2e/tasks/review_site/index.html` |

The old evaluation image (7.6 GB: review/score/playwright MCP servers, an npm copy of Claude Code, full Chromium,
browsers stored twice) is not used for this task.

## Where an item's artifacts are (all keyed by the source video id)

Under `e2e/3d-code-gen/claude_code/deepseek-v4.1-flash/`:

| Artifact | Path |
|---|---|
| source video, title, URL, clip window | `items/<id>/item.json`, `dataset/index.jsonl` |
| the clip the agent worked from | `workspace/<id>/project/input/<id>.mp4` |
| white-model video, side-by-side video + still, agent README | `items/<id>/results/<id>/{whitebox.mp4, compare.mp4, compare.jpg, README.md}` |
| Three.js source | `workspace/<id>/project/scene/` (`node_modules` removed after the run; `package.json` kept) |
| Claude Code transcripts (main + subagents) | `workspace/<id>/.claude/projects/-home-node/` |
| API trace (every exchange, byte-exact) | `items/<id>/traces/<id>.jsonl` + `.journal.jsonl` + `.wire/` |
| every call rebuilt from the transcript, training rows, audit | `items/<id>/train/{calls.jsonl, train.jsonl, audit.json}` |
| metrics | `items/<id>/metrics.json`; run averages `dataset/stats.json`; errors `dataset/errors.jsonl` |
| all training rows with item and call ids | `dataset/train.jsonl` + `dataset/train.jsonl.meta.jsonl` |
| previous attempts of an item | `attempts/<id>/<UTC>/` |

## What went wrong and what changed (2026-10-26)

| Symptom | Cause | Fix |
|---|---|---|
| an item ended after 22 min with "all conditions met" and an empty results dir | auto-compaction at 120k × 60% ≈ 72k tokens (the model config's window); after compacting, the agent claimed success, and the `/goal` evaluator, which only reads the conversation, accepted it | compaction at 200k × 80% (vLLM serves 262k); a Stop hook that blocks ending while `whitebox.mp4`, `compare.mp4`, `compare.jpg`, `README.md` are missing (at most 12 times) |
| 10-item test: all 10 ran into the 90 min limit, 3 rendered a video | agents spent the time on the skill's precision gates (camera landmark solving, spatial facts, layout validation) before any render | goal text asks for all deliverables at blockout fidelity first (skipping optional precision gates), then refinement; `--effort high` instead of the default `xhigh` |
| every request 400: `reasoning_effort must be low, high, xhigh, max` | DeepSeek V4.1 has no `medium` effort | use `high` |
| 400 `DeepSeek V4.1 supports text and image content only; got 'tool_reference'` | behind `ANTHROPIC_BASE_URL`, Claude Code ≥ 2.1.304 enables MCP tool search; vLLM rejects `tool_reference` blocks; Claude Code then retries without tool search | `ENABLE_TOOL_SEARCH=false` in later configs (also keeps the tool list stable) |
| `docker run` env from `~/.docker/config.json` points at a dead proxy (`172.17.0.1:20171`) | A800 user docker config | the runner blanks all six proxy variables; npm/pip use mirrors |

## Data checks on the 10-item test (old settings)

Every call rebuilt from the transcript matched its wire request exactly (1,457 calls: messages incl. cache_control,
system, tools, params). Responses differed only where Claude Code strips a leading `cd /home/node/project && ` from
Bash commands before storing them (10-39 per item, because the agent's working directory is `project`); later
requests send the stripped form back, so the training rows are consistent with what the model saw afterwards. Each
item had 4-6 compaction calls, which only the trace has.

## Run 2026-10-26/27 (106 items, 32 concurrent, 90 min limit per attempt)

Final numbers from `dataset/stats.json` after `e2e/tasks/finish_run.sh` stopped the runner at 00:10 UTC (before its
retry rounds) and ran the final post-processing.

| | value |
|---|---|
| items with a white-model video | 79 of 116 (34 timeouts, 3 exited without a video) |
| wall time per successful item | 85.7 min (agents keep refining until the stop time stated in the goal) |
| model time per successful item | 80.1 min (sum of `/v1/messages` durations) |
| model calls per successful item | 63 (93 training rows in total) |
| input tokens per successful item | 3.73 M (each call's full prompt summed; vLLM prefix-cache hit rate about 90%) |
| output tokens per successful item | 66 k |
| transcript vs trace | every rebuilt call matched its wire request exactly (messages, system, tools, params) |
| vLLM throughput at 32 agents | 30-37 running requests, 450-1100 generated tokens/s, KV cache 5-21% |
| storage | about 150 MB per item (mostly the agents' rendered frame PNGs), 21 GB for the run |

Yield by setting: 3/10 with the first goal text; 25/36 in the first scale wave after the Stop hook and the
deliverables-first order; 21/32 then 23/32 in later waves with the clock times in the goal. Failures are mostly
timeouts with a scene but no final render.

Review page: `e2e/3d-code-gen/review_site/` on the A800 (served with `python3 -m http.server 8791` there; from the
local machine through `ssh -L 8791:127.0.0.1:8791 maic_a800_80_2`).

## Change 2026-10-27: whole videos, attempt loop, finer outcomes

- **Input is the whole video** (`select_chem_videos.py --from-videos`): triage passed, one downloaded file covering
  the full duration (partial multi-file downloads skipped), shortest first. No clip cap. The goal text tells the
  agent the video can contain several shots and title cards. The earlier 10 s clips (fixed window, then middle of
  a shot) are kept as attempts but are not what we want.
- **No working-time limit.** The container runs `claude -p "/goal ..."`, then after every attempt
  `hooks/attempt_outcome.py` decides: `success` (deliverables + "Refinement" section present), `fail_fatal`
  (auth error, 400/401/403/404/413, "unrecoverable error"), `fail_stalled` (two attempts in a row with < 3 tool
  calls), or `resume` (anything else: `claude --continue` the same session with the goal set again, backoff 60 s
  doubling to 15 min, at most `max_resumes` resumes, then `gave_up`). Each attempt is a line in
  `traces/log/<id>.outcomes.jsonl`.
- The host stops an item only when no model call happened for `hang_minutes` (status `hung`) or at the 24 h safety
  limit (`timeout`).
- Claude Code's stream watchdog gave up on slow streams under load (one item failed with "The response stopped
  arriving" after three 440-480 s streams): `CLAUDE_STREAM_IDLE_TIMEOUT_MS=1800000`, `API_TIMEOUT_MS=3600000`.

## TODO

- Semantic segmentation into 15 s / 10 s / 5 s pieces **after** the white model of the whole video exists: re-enter
  the same image and `claude --resume` the item's session to add the step, so the segmentation becomes part of the
  same trajectory. Not started.

## Hang detection (2026-10-27, `e2e/watchdog.py`)

Measured on the earlier runs (`tests/serving_perf/silence_gaps.py`): model calls take p50 22 s, p95 7.7 min,
max 46 min under load; agent work between calls p99 83 s, max 9 min; response headers arrive in < 3 s (vLLM answers
before prefill, so time to first byte says nothing). A per-session "mean + 4 sd" timeout on wall time would flag 4%
of normal gaps, up to 63x over. So the watchdog judges progress, not time:

- **State** comes from Claude Code's own stream-json events: model call in flight (proxy journal), foreground
  tool (`tool_use` without `tool_result`), background task (`task_started` / `task_updated` is_backgrounded /
  `task_notification`), or idle.
- **Progress** = a stream byte (the proxy flushes every chunk), a new or finished model call, a tool result, a
  finished task, or a new file in the project (directory mtimes only, the workspace is on NFS). CPU is not
  progress: a busy loop burns CPU forever; per-process CPU of Claude's child processes (from /proc, attributed via
  `docker top`) only doubles the patience for a busy tool or background task. Other users' load does not leak in.
- **Threshold** per item and state: mean + 4 sd of this item's past silences that ended on their own, clamped to
  [10 min, 60 min].
- **Escalation** with jittered waits U(1,2) x threshold x 2^level: log, then kill Claude's child process trees
  (stuck tool or background task, e.g. a preview server Claude waits on), then end Claude (the attempt loop resumes
  the session), then report hung. Any progress resets it. If more than half of the items are suspicious at once, or
  the model server's /health fails, escalation freezes (server problem, not the items').
- Runs in **shadow** mode first (`watchdog_mode`: log to `traces/log/<id>.watchdog.jsonl`, no action; items are
  stopped only after `hang_minutes` = 240 without progress). Thresholds get set from that log before `act` mode.
