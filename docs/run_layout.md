# Run layout, attempts and versioning (`e2e/run_batch.py`)

## Layout

Under `<output_base>/<scaffolding>/<model>/`:

| Path | Content |
|---|---|
| `workspace/<id>/` | the agent's `/home/node`, including `.claude/` (transcripts, subagents, tool-results, shapes, blobs) |
| `items/<id>/` | mounted as `/app/outputs/<scaffolding>/<model>` in that item's container only |
| `items/<id>/traces/<id>.jsonl` (+ `.journal.jsonl`, `.wire/<proxy_run_id>/`) | API capture, see `docs/api_capture.md` |
| `items/<id>/traces/log/` | Claude Code stream-json, proxy log |
| `items/<id>/results/scores/` | task outputs written by MCP servers |
| `results/scores/<id>.json` | copy of each item's final score, for summary tooling |
| `attempts/<id>/<UTC>/{workspace,item}/` + `attempt_info.json` | previous attempts, moved here on re-run; never deleted |
| `archives/<id>/<UTC>.tar.zst` | optional packed attempt (`archive_attempts: true`) |
| `manifests/<UTC>.json` | one per batch: repo commit + dirty flag, image id, Claude Code version, model/run config, container env (secrets masked), prompt sha256, argv, host |
| `batch_log.jsonl` | per item: status, `exit_code`, `stopped_by` (`exited`/`timeout`/`batch_stop`/`error`), `capture`, `item_root` |

Isolation: before 2026-10-26 every container mounted the whole `output_base` read-write, so an agent (root, Bash)
could read or overwrite other items' and other models' workspaces, traces and scores. Each container now sees only
its own item root, under the same in-container path.

## Run config keys added 2026-10-26

| Key | Default | Effect |
|---|---|---|
| `capture` | `true` | start the capture proxy for every scaffold; the run aborts (exit 97, `PROXY_NOT_READY` in the proxy log) if it does not listen within 15 s, so no agent runs uncaptured |
| `claude_env` | `{}` | merged over the Claude Code defaults `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`, `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_ATTRIBUTION_HEADER=0`; `null` removes one |
| `claude_debug` | `false` | `claude --debug` + verbose log in `.claude/debug/` (troubleshooting only; now honoured with concurrency > 1) |
| `enable_tasks` | `false` | `CLAUDE_CODE_ENABLE_TASKS=1` (changes behaviour, so it is no longer tied to `claude_debug`) |
| `archive_attempts` | `false` | pack each finished item into `archives/` with `zstd -19 --long=27` |

Containers run with `--init -e TINI_KILL_PROCESS_GROUP=1` and are stopped with `docker stop -t 30`, so SIGTERM
reaches Claude Code and the proxy (see `docs/api_capture.md` for why `docker kill` and plain `docker stop` are
worse). Verified 2026-10-26: SIGUSR1 to the batch stopped item 401 in 6 s, `exit_code: 143`,
`stopped_by: batch_stop`, and the proxy journaled the 4 calls in flight.

## Versioning: what to version, at what granularity

| What | How | Why |
|---|---|---|
| pipeline code | this git repo (local + bare repo on the A800, `git push a800 main`, then `git pull` in the A800 work tree); each batch manifest records the commit and whether the tree was dirty | the repo had no version control before 2026-10-26 |
| per-request system prompt and tools | nothing to add: the transcript's content-addressed `api-request-shape` records give each call its exact system/tools | verified 88/88 on item 13 |
| per-step workspace state | nothing to add for training: what the model saw is in the transcript (tool calls and results) | snapshotting the filesystem at every step costs a lot and is not model input |
| each run of each item | one attempt directory, never overwritten; optional `tar.zst` per attempt | reruns must not destroy evidence |

Storage measured on item 13 (2026-10-26 run): the raw trace (`.wire/` + trace) is 69 MB and packs to 0.79 MB with
`zstd -19 --long=27` in 3 s (each request repeats the whole history, which long-range matching removes); the
workspace's `.claude/` is 4.8 MB → 0.74 MB. About 1.5 MB per item per attempt. Derived files
(`*.reconstructed.jsonl` 33 MB, `*.raw.jsonl` 132 MB) are regenerable and excluded from archives. If attempts
pile up across thousands of items, a chunk-deduplicating backup (restic/borg) on top of the attempt dirs removes
the repetition between attempts; not needed yet.
