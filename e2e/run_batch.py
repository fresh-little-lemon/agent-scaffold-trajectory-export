#!/usr/bin/env python3
"""Batch runner for interact-review evaluation.

Each HTML gets its own Docker container with isolated workspace.
Video recording + review. Scores submitted via score_server MCP.

Usage:
    python3 run_batch.py --config evaluation/configs/runs/qwen36_40test_claude_code_review.json
    python3 run_batch.py --model-config configs/qwen3.6-27b.json --scaffolding claude_code --concurrent 10
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from pathlib import Path

# Batch label: used to tag all Docker containers spawned by this process,
import re as _re
import requests as _requests

# so SIGUSR1 only kills containers belonging to THIS batch.
_BATCH_LABEL = f"batch_pid={os.getpid()}"


def _query_vllm_tokens(base_url: str) -> dict | None:
    """Query vLLM /metrics for prompt/generation/cached token totals."""
    if not base_url or ("127.0.0.1" not in base_url and "localhost" not in base_url):
        return None
    metrics_url = base_url.rsplit("/v1", 1)[0] + "/metrics"
    try:
        resp = _requests.get(metrics_url, timeout=5)
        text = resp.text
        def _parse(name):
            m = _re.search(rf'^{_re.escape(name)}\{{[^}}]*\}}\s+([\d.e+]+)', text, _re.MULTILINE)
            return float(m.group(1)) if m else 0.0
        return {"prompt_tokens": _parse("vllm:prompt_tokens_total"),
                "generation_tokens": _parse("vllm:generation_tokens_total"),
                "cached_tokens": _parse("vllm:prefix_cache_hits_total")}
    except Exception:
        return None

# Graceful stop: send SIGUSR1 to stop dispatching and kill THIS batch's containers.
_stop_event = threading.Event()

def _handle_stop_signal(signum, frame):
    log.warning(f"Received signal {signum}, stopping dispatch and killing containers (label={_BATCH_LABEL})...")
    _stop_event.set()
    threading.Thread(target=_kill_own_containers, daemon=True).start()

def _kill_own_containers():
    """Kill only containers belonging to this batch (matched by label)."""
    result = subprocess.run(
        f"docker ps --filter label={_BATCH_LABEL} -q | xargs -r -P10 docker kill",
        shell=True, capture_output=True, text=True,
    )
    killed = result.stdout.strip().splitlines()
    if killed:
        log.info(f"Killed {len(killed)} containers (label={_BATCH_LABEL})")

signal.signal(signal.SIGTERM, _handle_stop_signal)
signal.signal(signal.SIGUSR1, _handle_stop_signal)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BROWSER_AGENT = Path("/home/tsq/workspace/browser_agent")
HTMLS_DIR = BROWSER_AGENT / "resources" / "htmls"
RUBRICS_DIR = BROWSER_AGENT / "rubrics"
CONTEXTS_DIR = BROWSER_AGENT / "resources" / "contexts"
OUTPUT_BASE = BROWSER_AGENT / "evaluation" / "outputs" / "interact-review"
SKILL_FILE = BROWSER_AGENT / "evaluation" / "pipelines" / "interact_review" / "shared" / "eval-html-review.md"
API_PROXY_SCRIPT = BROWSER_AGENT / "evaluation" / "pipelines" / "shared" / "api_proxy.py"
SCORE_SERVER_SCRIPT = BROWSER_AGENT / "evaluation" / "pipelines" / "interact_review" / "review" / "score_server.py"
IN_CONTAINER_PROXY_PORT = 9999  # Per-container proxy port

SUBJECT_MAP = {
    "物理": "physics", "数学": "math", "初中数学": "math",
    "生物": "biology", "化学": "chemistry", "化學": "chemistry",
    "地理": "geography", "高中地理": "geography",
    "计算机": "information", "信息科技": "information",
    "信息技术": "information", "算法": "information",
    "人工智能": "information", "AI": "information",
    "语文": "chinese", "英语": "english", "English": "english",
}


def detect_subject(html_id: str) -> str:
    """Detect subject for an HTML document.

    Primary: read from learn_your_way.db (same source as replay_platform).
    Fallback: keyword matching on context JSON.
    """
    db_path = BROWSER_AGENT / "resources" / "learn_your_way.db"
    if db_path.exists():
        try:
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                row = conn.execute(
                    "SELECT subject FROM documents WHERE id = ?", (int(html_id),)
                ).fetchone()
                if row:
                    subject_str = row[0]
                    if not subject_str or subject_str == "None":
                        return "other_subject"
                    for label, key in SUBJECT_MAP.items():
                        if label in subject_str or subject_str in label:
                            return key
                    return "other_subject"
        except Exception:
            pass

    ctx_file = CONTEXTS_DIR / f"{html_id}.json"
    if not ctx_file.exists():
        return "other_subject"
    ctx = json.loads(ctx_file.read_text())
    s = ctx.get("summary", {})
    combined = f"{s.get('title_summary', '')} {s.get('target_audience', '')}"
    for label, key in SUBJECT_MAP.items():
        if label in combined:
            return key
    return "other_subject"


def prepare_rubrics(html_id: str) -> Path:
    subject = detect_subject(html_id)
    view = Path(f"/tmp/rubric_view_{html_id}_{os.getpid()}")
    view.mkdir(parents=True, exist_ok=True)
    for f in RUBRICS_DIR.glob("*.yaml"):
        (view / f.name).write_bytes(f.read_bytes())
    subj_file = RUBRICS_DIR / "subjects" / f"{subject}.yaml"
    if subj_file.exists():
        (view / subj_file.name).write_bytes(subj_file.read_bytes())
    return view


def _http_port(html_id: str) -> int:
    """Deterministic per-HTML HTTP server port to avoid --network host collisions."""
    return 38000 + (hash(html_id) % 10000)


def build_prompt(html_id: str, scaffolding: str, model: str) -> str:
    template = SKILL_FILE.read_text()
    return (
        template
        .replace("$ARGUMENTS", html_id)
        .replace("$RUBRICS_DIR", "/data/rubrics")
        .replace("$HTMLS_DIR", "/data/resources/htmls")
        .replace("$CONTEXTS_DIR", "/data/resources/contexts")
    )


def ensure_playwright_volume():
    """Create shared Playwright cache volume (once, reused read-only by all containers)."""
    vol_name = "eval-interact-review-pw-cache"
    check = subprocess.run(
        ["docker", "volume", "inspect", vol_name],
        capture_output=True,
    )
    if check.returncode != 0:
        log.info("Creating shared Playwright cache volume...")
        subprocess.run([
            "docker", "run", "--rm", "--user", "root",
            "--label", _BATCH_LABEL,
            "-v", f"{vol_name}:/mnt/pw",
            IMAGE_MAP.get("claude_code", "eval-interact-review-claude-code"),
            "bash", "-c",
            "cp -a /home/node/.cache/ms-playwright/. /mnt/pw/ && chmod -R 777 /mnt/pw",
        ])


def seed_workspace(run_home: Path, scaffolding: str, image: str = "eval-base"):
    """Seed a per-run home directory from the image's home-seed.tar."""
    # Each scaffold's image has its own clean home-seed.tar, just extract it
    if any((run_home / m).exists() for m in [".claude.json", ".codex", ".kimi", ".opencode", ".gemini"]):
        return  # Already seeded
    subprocess.run([
        "docker", "run", "--rm", "--user", "root",
        "--label", _BATCH_LABEL,
        "-v", f"{run_home}:/mnt/run_home",
        image, "bash", "-c",
        "tar xf /tmp/home-seed.tar -C /mnt/run_home && chmod -R 777 /mnt/run_home",
    ], capture_output=True)


def fix_permissions(run_home: Path, image: str = "eval-base"):
    """Make all files in run_home world-readable so host root can access."""
    subprocess.run([
        "docker", "run", "--rm", "--user", "root",
        "--label", _BATCH_LABEL,
        "-v", f"{run_home}:/mnt/h",
        image, "bash", "-c", "chmod -R 777 /mnt/h 2>/dev/null; true",
    ], capture_output=True)


def _archive_failed(html_id: str, tried_cnt: int, workspace_dir: Path,
                    scores_dir: Path, model_out_dir: Path):
    """Move stale workspace/traces/progress to {model_out_dir}/failed/{html_id}-tried-{cnt}/."""
    failed_dir = model_out_dir / "failed" / f"{html_id}-tried-{tried_cnt}"
    failed_dir.mkdir(parents=True, exist_ok=True)
    ws = workspace_dir / html_id
    if ws.exists():
        shutil.move(str(ws), str(failed_dir / "workspace"))
    traces_dir = model_out_dir / "traces"
    trace = traces_dir / f"{html_id}.jsonl"
    if trace.exists():
        (failed_dir / "traces").mkdir(exist_ok=True)
        shutil.move(str(trace), str(failed_dir / "traces" / f"{html_id}.jsonl"))
    progress = scores_dir / ".progress" / f"{html_id}.json"
    if progress.exists():
        (failed_dir / "progress").mkdir(exist_ok=True)
        shutil.move(str(progress), str(failed_dir / "progress" / f"{html_id}.json"))


IMAGE_MAP = {
    "claude_code": "agent-scaffold-claude-code:2.1.310",
    "kimi_code_cli": "eval-interact-review-kimi-code-cli",
    "opencode": "eval-interact-review-opencode",
    "codex": "eval-interact-review-codex",
    "qwen_code": "eval-interact-review-qwen-code",
    "mistral_vibe": "eval-interact-review-mistral-vibe",
}


def run_single(
    html_id: str,
    scaffolding: str,
    model_cfg: dict,
    scores_dir: Path,
    workspace_dir: Path,
    output_base: Path = OUTPUT_BASE,
    container_timeout: int = 7200,
    debug: bool = False,
    claude_debug: bool = False,
) -> dict:
    model = model_cfg["model_name"]
    score_file = scores_dir / f"{html_id}.json"
    if score_file.exists():
        return {"html_id": html_id, "status": "skipped"}

    rubric_view = prepare_rubrics(html_id)
    prompt = build_prompt(html_id, scaffolding, model)
    prompt += (
        "\n\n附加执行要求：这是一个需要多角度证据核验的复杂评估。请明确使用一个 Workflow，"
        "至少安排三个独立子任务，分别检查 HTML/交互实现、评分标准与学科背景、"
        "以及对前两项结论做反证核验；汇总后由主会话亲自完成浏览器操作、视频回看和打分。"
        "子任务不要同时操作共享浏览器。说明每个子任务的结论依据，再完成原有全部评估步骤。"
    )

    # Per-run isolated home directory
    run_home = workspace_dir / html_id

    # Scenario 2 (retry): workspace exists from a previously killed container
    # but no score file → stale workspace, clean and re-seed
    if run_home.exists() and not score_file.exists():
        has_session = (
            (any((run_home / ".claude" / "projects").rglob("*.jsonl")) if (run_home / ".claude" / "projects").exists() else False)
            or (any((run_home / ".codex" / "sessions").rglob("*.jsonl")) if (run_home / ".codex" / "sessions").exists() else False)
            or (any((run_home / ".kimi" / "sessions").rglob("*")) if (run_home / ".kimi" / "sessions").exists() else False)
        )
        if has_session:
            log.info(f"[{html_id}] Stale workspace (no score), cleaning for retry")
            # Use docker to remove root-owned files (delete contents, not mount point)
            subprocess.run([
                "docker", "run", "--rm",
                "-v", f"{run_home}:/target",
                "alpine", "sh", "-c", "rm -rf /target/* /target/.[!.]* /target/..?*",
            ], capture_output=True)
            # Also try regular rmtree as fallback
            shutil.rmtree(run_home, ignore_errors=True)

    run_home.mkdir(parents=True, exist_ok=True)
    base_image = IMAGE_MAP.get(scaffolding, "eval-interact")
    seed_workspace(run_home, scaffolding, base_image)

    # Write prompt to temp file
    prompt_file = Path(f"/tmp/eval_prompt_{html_id}_{os.getpid()}.md")
    prompt_file.write_text(prompt, encoding="utf-8")

    # Generate per-run MCP configs:
    # 1. review_mcp.py wraps Playwright + auto recording + review tools
    # 2. score_server.py handles scoring (separate, independent)
    review_mcp_config = {
        "mcpServers": {
            "review": {
                "command": "python3",
                "args": ["/opt/review/review_mcp.py"],
            },
        },
    }
    review_mcp_file = Path(f"/tmp/review_mcp_{html_id}_{os.getpid()}.json")
    review_mcp_file.write_text(json.dumps(review_mcp_config))

    score_mcp_config = {
        "mcpServers": {
            "score": {
                "command": "python3",
                "args": [
                    "/opt/review/score_server.py",
                    "--html-id", html_id,
                    "--output-dir", f"/app/outputs/{scaffolding}/{model}",
                    "--rubrics-dir", "/data/rubrics",
                ],
            },
        },
    }
    score_mcp_file = Path(f"/tmp/score_mcp_{html_id}_{os.getpid()}.json")
    score_mcp_file.write_text(json.dumps(score_mcp_config))

    log.info(f"[{html_id}] Starting {scaffolding}/{model}")
    start = time.time()

    # Pre-create traces/log dir on host (avoids permission issues inside container)
    (output_base / scaffolding / model / "traces" / "log").mkdir(parents=True, exist_ok=True)

    host_uid_gid = f"{os.getuid()}:{os.getgid()}"

    docker_cmd = [
        "docker", "run", "--rm",
        "--add-host", "host.docker.internal:host-gateway",
        "--label", _BATCH_LABEL,
        "--user", host_uid_gid,
        "-e", "http_proxy=", "-e", "https_proxy=",
        "-e", "HTTP_PROXY=", "-e", "HTTPS_PROXY=",
        "-e", "all_proxy=", "-e", "ALL_PROXY=",
        "-e", "HOME=/home/node",
        "-e", "PLAYWRIGHT_BROWSERS_PATH=/home/node/.cache/ms-playwright",
    ]
    # Set context window for auto-compact if specified in model config
    ctx_window = model_cfg.get("context_window")
    if ctx_window:
        docker_cmd += [
            "-e", f"CLAUDE_CODE_AUTO_COMPACT_WINDOW={ctx_window}",
            "-e", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=60",
        ]
    # Claude Code verbose debug logging (writes to .claude/debug/)
    if claude_debug:
        docker_cmd += [
            "-e", "CLAUDE_CODE_DEBUG_LOG_LEVEL=verbose",
            "-e", "CLAUDE_CODE_ENABLE_TASKS=1",
        ]
    docker_cmd += [
        "-v", f"{run_home}:/home/node",
        "-w", "/home/node",
        "-v", "eval-interact-review-pw-cache:/home/node/.cache/ms-playwright",
        "-v", f"{HTMLS_DIR}/{html_id}.html:/data/resources/htmls/{html_id}.html:ro",
        "-v", f"{CONTEXTS_DIR}/{html_id}.json:/data/resources/contexts/{html_id}.json:ro",
        "-v", f"{rubric_view}:/data/rubrics:ro",
        "-v", f"{output_base}:/app/outputs",
        "-v", f"{prompt_file}:/tmp/prompt.md:ro",
        "-v", f"{review_mcp_file}:/tmp/review_mcp.json:ro",
        "-v", f"{score_mcp_file}:/tmp/score_mcp.json:ro",
        "-v", f"{Path(__file__).resolve().parent / 'api_proxy.py'}:/opt/api_proxy.py:ro",
        "-v", f"{SCORE_SERVER_SCRIPT}:/opt/review/score_server.py:ro",
    ]

    # Remap localhost URLs to host.docker.internal (containers no longer use --network host)
    _raw_api_base = model_cfg.get("anthropic_base_url") or model_cfg.get("openai_base_url", "http://127.0.0.1:8001")
    api_base = _raw_api_base.replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")

    no_score_prompt = (
        "检测到你尚未使用 score MCP 工具进行评分。如果你认为浏览器交互还不够充分，可以继续操作网页收集更多信息。否则请按以下流程完成评估：\\n"
        "1. 调用 list_operations() 查看你之前的浏览器操作记录\\n"
        "2. 对关键操作调用 review_around() 回看视频帧，并用 Read 工具查看全分辨率帧\\n"
        "3. 调用 get_progress() 查看需要评分的维度列表\\n"
        "4. 对每个维度调用 submit_score(dimension_key, score, reasoning)，reasoning 必须引用你观察到的具体帧内容\\n"
        "5. 调用 set_interaction_summary() 记录交互总结\\n"
        "6. 调用 finalize_scores() 完成最终提交"
    )
    resume_prompt = (
        "检测到你已开始评分但尚未完成。请继续完成剩余维度的评分：\\n"
        "1. 调用 get_progress() 查看已评和未评的维度\\n"
        "2. 对每个未评维度调用 submit_score()，基于你之前的浏览器交互和视频回看给出评分和reasoning\\n"
        "3. 如果还没设置，调用 set_interaction_summary() 记录交互总结\\n"
        "4. 调用 finalize_scores() 完成评分"
    )
    finalize_only_prompt = (
        "你之前的评分已经全部提交完成。请立即调用 finalize_scores() 完成最终提交。"
    )

    if scaffolding == "claude_code":
        score_path = f"/app/outputs/{scaffolding}/{model}/results/scores/{html_id}.json"
        claude_debug_flag = " --debug" if claude_debug else ""
        claude_base = (
            f"claude --print --dangerously-skip-permissions{claude_debug_flag} "
            "--effort ultracode --output-format stream-json --verbose --include-partial-messages "
            "--include-hook-events --forward-subagent-text --system-prompt-snapshot on "
            "--mcp-config /tmp/review_mcp.json --mcp-config /tmp/score_mcp.json "
            "--allowedTools 'Agent,Workflow,Bash(*),Read(*),Write(*),Edit(*),Glob(*),Grep(*),mcp__review__*,mcp__score__*'"
        )
        progress_path = f"/app/outputs/{scaffolding}/{model}/results/scores/.progress/{html_id}.json"
        trace_file = f"/app/outputs/{scaffolding}/{model}/traces/{html_id}.jsonl"
        # In debug mode, start per-container proxy and route through it
        if debug:
            proxy_start = (
                f"mkdir -p /app/outputs/{scaffolding}/{model}/traces/log && "
                f"UPSTREAM={api_base} TRACE_FILE={trace_file} "
                f"python3 /opt/api_proxy.py {IN_CONTAINER_PROXY_PORT} > /tmp/proxy.log 2>/app/outputs/{scaffolding}/{model}/traces/log/{html_id}.proxy.log &"
                f"sleep 1 && "
            )
            actual_api_base = f"http://127.0.0.1:{IN_CONTAINER_PROXY_PORT}"
        else:
            proxy_start = ""
            actual_api_base = api_base

        bash_cmd = (
            f"umask 0000 && "
            f"mkdir -p $(dirname {trace_file}) && "
            f"{proxy_start}"
            f"cd /data/resources/htmls && python3 -m http.server 8765 > /tmp/http.log 2>&1 & cd /home/node && "
            f"cp /tmp/prompt.md /home/node/.claude/commands/eval-html-review.md 2>/dev/null ; "
            f"sleep 1 && "
            f"{claude_base} -p \"$(cat /tmp/prompt.md)\" > /app/outputs/{scaffolding}/{model}/traces/log/{html_id}.claude.stream.jsonl 2>&1 ; "
            f"for _retry in 1 2 3 4 5; do "
            f"if [ -f \"{score_path}\" ]; then break; fi ; "
            f"if [ -f \"{progress_path}\" ]; then "
            f"SCORED=$(python3 -c \"import json; d=json.load(open('{progress_path}')); print(len(d.get('scores',{{}})))\" 2>/dev/null || echo 0) ; "
            f"HAS_SUMMARY=$(python3 -c \"import json; d=json.load(open('{progress_path}')); print('1' if d.get('interaction_summary') else '0')\" 2>/dev/null || echo 0) ; "
            f"if [ \"$SCORED\" -ge 10 ] && [ \"$HAS_SUMMARY\" = \"1\" ]; then "
            f"echo \"[RESUME $_retry] finalize-only (scored=$SCORED)\" >&2 ; "
            f"{claude_base} --continue -p \"{finalize_only_prompt}\" ; "
            f"else "
            f"echo \"[RESUME $_retry] incomplete (scored=$SCORED summary=$HAS_SUMMARY)\" >&2 ; "
            f"{claude_base} --continue -p \"{resume_prompt}\" ; "
            f"fi ; "
            f"else "
            f"echo \"[RESUME $_retry] no score activity\" >&2 ; "
            f"{claude_base} --continue -p \"{no_score_prompt}\" ; "
            f"fi ; "
            f"done"
        )
        # Map all Claude Code model aliases to the actual model name
        docker_cmd += [
            "-e", f"ANTHROPIC_API_KEY={model_cfg['api_key']}",
            "-e", f"ANTHROPIC_BASE_URL={actual_api_base}",
            "-e", f"ANTHROPIC_MODEL={model}",
            "-e", f"ANTHROPIC_SMALL_FAST_MODEL={model}",
            "-e", f"ANTHROPIC_DEFAULT_SONNET_MODEL={model}",
            "-e", f"ANTHROPIC_DEFAULT_OPUS_MODEL={model}",
            "-e", f"ANTHROPIC_DEFAULT_HAIKU_MODEL={model}",
            "-e", "API_TIMEOUT_MS=600000",
            base_image,
            "bash", "-c",
            bash_cmd,
        ]
    elif scaffolding == "codex":
        score_path = f"/app/outputs/{scaffolding}/{model}/results/scores/{html_id}.json"
        trace_file = f"/app/outputs/{scaffolding}/{model}/traces/{html_id}.jsonl"
        vllm_url = model_cfg.get("openai_base_url", "http://127.0.0.1:8001/v1")
        vllm_url = vllm_url.replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")
        # In debug mode, route through per-container proxy
        if debug:
            proxy_base_url = f"http://127.0.0.1:{IN_CONTAINER_PROXY_PORT}/v1"
            upstream_for_proxy = vllm_url.rsplit("/v1", 1)[0]
            proxy_start = (
                f"mkdir -p /app/outputs/{scaffolding}/{model}/traces/log && "
                f"UPSTREAM={upstream_for_proxy} TRACE_FILE={trace_file} "
                f"python3 /opt/api_proxy.py {IN_CONTAINER_PROXY_PORT} > /tmp/proxy.log 2>/app/outputs/{scaffolding}/{model}/traces/log/{html_id}.proxy.log &"
                f"sleep 1 && "
            )
        else:
            proxy_base_url = vllm_url
            proxy_start = ""
        # Generate codex config.toml with model provider + score server MCP
        ctx_window = model_cfg.get("model_context_window", 262144)
        compact_limit = model_cfg.get("model_auto_compact_token_limit", 200000)
        ctx_lines = f'model_context_window = {ctx_window}\n'
        ctx_lines += f'model_auto_compact_token_limit = {compact_limit}\n'
        codex_model_toml = (
            f'model_provider = "vLLM"\n'
            f'model = "{model}"\n'
            f'review_model = "{model}"\n'
            f'model_reasoning_effort = "xhigh"\n'
            f'{ctx_lines}\n'
            f'[model_providers.vLLM]\n'
            f'name = "vLLM"\n'
            f'base_url = "{proxy_base_url}"\n'
            f'wire_api = "responses"\n'
            f'requires_openai_auth = false\n\n'
            f'[mcp_servers.review]\n'
            f'command = "python3"\n'
            f'args = ["/opt/review/review_mcp.py"]\n'
            f'tool_timeout_sec = 600\n\n'
            f'[mcp_servers.score]\n'
            f'command = "python3"\n'
            f'args = ["/opt/review/score_server.py", "--html-id", "{html_id}", '
            f'"--output-dir", "/app/outputs/{scaffolding}/{model}", '
            f'"--rubrics-dir", "/data/rubrics"]\n'
            f'tool_timeout_sec = 600\n\n'
        )
        codex_config_file = Path(f"/tmp/codex_config_{html_id}_{os.getpid()}.toml")
        codex_config_file.write_text(codex_model_toml)

        bash_cmd = (
            f"umask 0000 && "
            f"mkdir -p $(dirname {trace_file}) && "
            f"{proxy_start}"
            f"cd /data/resources/htmls && python3 -m http.server 8765 > /tmp/http.log 2>&1 & cd /home/node && "
            f"git init -q 2>/dev/null ; "
            f"sleep 1 && "
            f"cat /tmp/codex_extra.toml /home/node/.codex/config.toml > /tmp/merged.toml && "
            f"cp /tmp/merged.toml /home/node/.codex/config.toml && "
            f"codex exec --yolo --skip-git-repo-check \"$(cat /tmp/prompt.md)\" ; "
            f"SESS_ID=$(basename $(find /home/node/.codex/sessions -name 'rollout-*.jsonl' 2>/dev/null | sort | head -1) | grep -oP '[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}') ; "
            f"for _retry in 1 2 3 4 5; do "
            f"if [ -f \"{score_path}\" ] || [ -z \"$SESS_ID\" ]; then break; fi ; "
            f"if [ -f \"/app/outputs/{scaffolding}/{model}/results/scores/.progress/{html_id}.json\" ]; then "
            f"echo \"[RESUME $_retry] Score in progress ($SESS_ID), resuming to finalize...\" >&2 ; "
            f"codex exec resume --yolo --skip-git-repo-check \"$SESS_ID\" \"{resume_prompt}\" ; "
            f"else "
            f"echo \"[RESUME $_retry] No score activity ($SESS_ID)\" >&2 ; "
            f"codex exec resume --yolo --skip-git-repo-check \"$SESS_ID\" \"{no_score_prompt}\" ; "
            f"fi ; "
            f"done"
        )

        image = IMAGE_MAP["codex"]

        docker_cmd += [
            "-e", f"OPENAI_API_KEY={model_cfg['api_key']}",
            "-e", f"OPENAI_BASE_URL={proxy_base_url}",
            "-v", f"{codex_config_file}:/tmp/codex_extra.toml:ro",
            image,
            "bash", "-c",
            bash_cmd,
        ]
    elif scaffolding == "kimi_code_cli":
        score_path = f"/app/outputs/{scaffolding}/{model}/results/scores/{html_id}.json"
        trace_file = f"/app/outputs/{scaffolding}/{model}/traces/{html_id}.jsonl"

        # In debug mode, route through per-container proxy
        if debug:
            kimi_api_base = f"http://127.0.0.1:{IN_CONTAINER_PROXY_PORT}"
            proxy_start = (
                f"mkdir -p /app/outputs/{scaffolding}/{model}/traces/log && "
                f"UPSTREAM={api_base} TRACE_FILE={trace_file} "
                f"python3 /opt/api_proxy.py {IN_CONTAINER_PROXY_PORT} > /tmp/proxy.log 2>/app/outputs/{scaffolding}/{model}/traces/log/{html_id}.proxy.log &"
                f"sleep 1 && "
            )
        else:
            kimi_api_base = api_base
            proxy_start = ""

        # Generate kimi config.toml
        kimi_config = (
            f'default_model = "eval-model"\n'
            f'default_yolo = true\n\n'
            f'[providers.vllm]\n'
            f'type = "anthropic"\n'
            f'base_url = "{kimi_api_base}"\n'
            f'api_key = "{model_cfg.get("api_key", "EMPTY")}"\n\n'
            f'[models.eval-model]\n'
            f'provider = "vllm"\n'
            f'model = "{model}"\n'
            f'max_context_size = 262144\n'
            f'capabilities = ["image_in", "thinking"]\n'
        )
        kimi_config_file = Path(f"/tmp/kimi_config_{html_id}_{os.getpid()}.toml")
        kimi_config_file.write_text(kimi_config)

        # MCP config: review_mcp.py (wraps Playwright + review tools) + score server
        kimi_mcp = json.dumps({
            "mcpServers": {
                "review": {
                    "command": "python3",
                    "args": ["/opt/review/review_mcp.py"],
                },
                "score": {
                    "command": "python3",
                    "args": ["/opt/review/score_server.py", "--html-id", html_id,
                             "--output-dir", f"/app/outputs/{scaffolding}/{model}",
                             "--rubrics-dir", "/data/rubrics"],
                },
            }
        })
        kimi_mcp_file = Path(f"/tmp/kimi_mcp_{html_id}_{os.getpid()}.json")
        kimi_mcp_file.write_text(kimi_mcp)

        bash_cmd = (
            f"umask 0000 && "
            f"mkdir -p $(dirname {trace_file}) && "
            f"{proxy_start}"
            f"cd /data/resources/htmls && python3 -m http.server 8765 > /tmp/http.log 2>&1 & cd /home/node && "
            f"sleep 1 && "
            f"cp /tmp/kimi_config.toml /home/node/.kimi/config.toml && "
            f"cp /tmp/kimi_mcp.json /home/node/.kimi/mcp.json && "
            f"cat /tmp/prompt.md | /usr/local/bin/kimi --print ; "
            f"KIMI_SID=$(ls /home/node/.kimi/sessions/ 2>/dev/null | sort | tail -1 | sed 's/.jsonl//') ; "
            f"for _retry in 1 2 3 4 5; do "
            f"if [ -f \"{score_path}\" ] || [ -z \"$KIMI_SID\" ]; then break; fi ; "
            f"if [ -f \"/app/outputs/{scaffolding}/{model}/results/scores/.progress/{html_id}.json\" ]; then "
            f"echo \"[RESUME $_retry] Score in progress ($KIMI_SID)\" >&2 ; "
            f"echo \"{resume_prompt}\" | /usr/local/bin/kimi --print -r \"$KIMI_SID\" ; "
            f"else "
            f"echo \"[RESUME $_retry] No score activity ($KIMI_SID)\" >&2 ; "
            f"echo \"{no_score_prompt}\" | /usr/local/bin/kimi --print -r \"$KIMI_SID\" ; "
            f"fi ; "
            f"done"
        )

        docker_cmd += [
            "-e", f"KIMI_API_KEY={model_cfg.get('api_key', 'EMPTY')}",
            "-e", "KIMI_MODEL_CAPABILITIES=image_in,thinking",
            "-v", f"{kimi_config_file}:/tmp/kimi_config.toml:ro",
            "-v", f"{kimi_mcp_file}:/tmp/kimi_mcp.json:ro",
            IMAGE_MAP["kimi_code_cli"],
            "bash", "-c",
            bash_cmd,
        ]
    elif scaffolding == "opencode":
        score_path = f"/app/outputs/{scaffolding}/{model}/results/scores/{html_id}.json"
        trace_file = f"/app/outputs/{scaffolding}/{model}/traces/{html_id}.jsonl"

        # In debug mode, route through per-container proxy
        if debug:
            opencode_api_base = f"http://127.0.0.1:{IN_CONTAINER_PROXY_PORT}/v1"
            proxy_start = (
                f"mkdir -p /app/outputs/{scaffolding}/{model}/traces/log && "
                f"UPSTREAM={api_base} TRACE_FILE={trace_file} "
                f"python3 /opt/api_proxy.py {IN_CONTAINER_PROXY_PORT} > /tmp/proxy.log 2>/app/outputs/{scaffolding}/{model}/traces/log/{html_id}.proxy.log &"
                f"sleep 1 && "
            )
        else:
            opencode_api_base = model_cfg.get("openai_base_url", "http://127.0.0.1:8001/v1")
            opencode_api_base = opencode_api_base.replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")
            proxy_start = ""

        # OpenCode config with review_mcp (wraps Playwright) + score server
        opencode_config = json.dumps({
            "provider": {
                "vllm": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "vLLM",
                    "options": {"baseURL": opencode_api_base},
                    "models": {
                        model: {
                            "name": model,
                            "contextWindow": model_cfg.get("context_window", 262144),
                            "modalities": {"input": ["text", "image"], "output": ["text"]},
                        },
                    },
                },
            },
            "mcp": {
                "review": {
                    "type": "local",
                    "enabled": True,
                    "command": ["python3", "/opt/review/review_mcp.py"],
                },
                "score": {
                    "type": "local",
                    "enabled": True,
                    "command": ["python3", "/opt/review/score_server.py",
                                "--html-id", html_id,
                                "--output-dir", f"/app/outputs/{scaffolding}/{model}",
                                "--rubrics-dir", "/data/rubrics"],
                },
            },
        })
        opencode_config_file = Path(f"/tmp/opencode_config_{html_id}_{os.getpid()}.json")
        opencode_config_file.write_text(opencode_config)

        bash_cmd = (
            f"umask 0000 && "
            f"mkdir -p $(dirname {trace_file}) && "
            f"apt-get update -qq > /dev/null 2>&1 && apt-get install -y -qq ffmpeg > /dev/null 2>&1 ; "
            f"{proxy_start}"
            f"cd /data/resources/htmls && python3 -m http.server 8765 > /tmp/http.log 2>&1 & cd /home/node && "
            f"git init -q 2>/dev/null ; "
            f"sleep 1 && "
            f"cp /tmp/opencode_config.json /home/node/opencode.json && "
            f"opencode run --dangerously-skip-permissions --model vllm/{model} \"$(cat /tmp/prompt.md)\" ; "
            f"for _retry in 1 2 3 4 5; do "
            f"if [ -f \"{score_path}\" ]; then break; fi ; "
            f"echo \"[RESUME $_retry] no score activity\" >&2 ; "
            f"opencode run --dangerously-skip-permissions --continue --model vllm/{model} \"{no_score_prompt}\" ; "
            f"done ; "
            f"SESS_ID=$(opencode session list 2>/dev/null | head -1 | awk '{{print $1}}') ; "
            f"SESS_ID=$(opencode session list 2>/dev/null | grep -oP 'ses_\\S+' | head -1) ; "
            f"[ -n \"$SESS_ID\" ] && opencode export \"$SESS_ID\" > /home/node/session.json 2>/dev/null"
        )

        docker_cmd += [
            "-e", f"OPENAI_API_KEY={model_cfg.get('api_key', 'EMPTY')}",
            "-v", f"{opencode_config_file}:/tmp/opencode_config.json:ro",
            "-v", "eval-opencode-modules:/home/node/.config/opencode/node_modules:ro",
            IMAGE_MAP["opencode"],
            "bash", "-c",
            bash_cmd,
        ]
    elif scaffolding == "qwen_code":
        score_path = f"/app/outputs/{scaffolding}/{model}/results/scores/{html_id}.json"
        progress_path = f"/app/outputs/{scaffolding}/{model}/results/scores/.progress/{html_id}.json"
        trace_file = f"/app/outputs/{scaffolding}/{model}/traces/{html_id}.jsonl"
        if debug:
            proxy_start = (
                f"mkdir -p /app/outputs/{scaffolding}/{model}/traces/log && "
                f"UPSTREAM={api_base} TRACE_FILE={trace_file} "
                f"python3 /opt/api_proxy.py {IN_CONTAINER_PROXY_PORT} > /tmp/proxy.log 2>/app/outputs/{scaffolding}/{model}/traces/log/{html_id}.proxy.log &"
                f"sleep 1 && "
            )
            actual_api_base = f"http://127.0.0.1:{IN_CONTAINER_PROXY_PORT}"
        else:
            proxy_start = ""
            actual_api_base = api_base

        qwen_base = (
            f"qwen --yolo --auth-type openai --openai-api-key {model_cfg.get('api_key', 'EMPTY')} "
            f"--openai-base-url {actual_api_base}/v1 "
            f"--model {model} --mcp-config /tmp/qwen_mcp_merged.json"
        )
        bash_cmd = (
            f"umask 0000 && "
            f"python3 -c \"import json; a=json.load(open('/tmp/review_mcp.json')); b=json.load(open('/tmp/score_mcp.json')); a['mcpServers'].update(b['mcpServers']); json.dump(a, open('/tmp/qwen_mcp_merged.json','w'))\" && "
            f"mkdir -p $(dirname {trace_file}) && "
            f"{proxy_start}"
            f"cd /data/resources/htmls && python3 -m http.server 8765 > /tmp/http.log 2>&1 & cd /home/node && "
            f"sleep 1 && "
            f"{qwen_base} -p \"$(cat /tmp/prompt.md)\" ; "
            f"for _retry in 1 2 3 4 5; do "
            f"if [ -f \"{score_path}\" ]; then break; fi ; "
            f"if [ -f \"{progress_path}\" ]; then "
            f"SCORED=$(python3 -c \"import json; d=json.load(open('{progress_path}')); print(len(d.get('scores',{{}})))\" 2>/dev/null || echo 0) ; "
            f"HAS_SUMMARY=$(python3 -c \"import json; d=json.load(open('{progress_path}')); print('1' if d.get('interaction_summary') else '0')\" 2>/dev/null || echo 0) ; "
            f"if [ \"$SCORED\" -ge 10 ] && [ \"$HAS_SUMMARY\" = \"1\" ]; then "
            f"echo \"[RESUME $_retry] finalize-only (scored=$SCORED)\" >&2 ; "
            f"{qwen_base} -c -p \"{finalize_only_prompt}\" ; "
            f"else "
            f"echo \"[RESUME $_retry] incomplete (scored=$SCORED summary=$HAS_SUMMARY)\" >&2 ; "
            f"{qwen_base} -c -p \"{resume_prompt}\" ; "
            f"fi ; "
            f"else "
            f"echo \"[RESUME $_retry] no score activity\" >&2 ; "
            f"{qwen_base} -c -p \"{no_score_prompt}\" ; "
            f"fi ; "
            f"done"
        )
        # qwen CLI calls os.userInfo() which needs /etc/passwd entry;
        # generate a fake passwd file with host uid mapped to node user
        passwd_file = Path(f"/tmp/qwen_passwd_{html_id}_{os.getpid()}")
        passwd_file.write_text(f"root:x:0:0:root:/root:/bin/bash\nnode:x:{os.getuid()}:{os.getgid()}::/home/node:/bin/bash\n")
        docker_cmd += ["-v", f"{passwd_file}:/etc/passwd:ro"]
        docker_cmd += [
            "-e", f"OPENAI_API_KEY={model_cfg.get('api_key', 'EMPTY')}",
            "-e", f"OPENAI_BASE_URL={actual_api_base}",
            "-e", f"QWEN_MODEL={model}",
            "-e", "API_TIMEOUT_MS=600000",
            IMAGE_MAP["qwen_code"],
            "bash", "-c",
            bash_cmd,
        ]
    elif scaffolding == "mistral_vibe":
        score_path = f"/app/outputs/{scaffolding}/{model}/results/scores/{html_id}.json"
        progress_path = f"/app/outputs/{scaffolding}/{model}/results/scores/.progress/{html_id}.json"
        trace_file = f"/app/outputs/{scaffolding}/{model}/traces/{html_id}.jsonl"
        if debug:
            proxy_start = (
                f"mkdir -p /app/outputs/{scaffolding}/{model}/traces/log && "
                f"UPSTREAM={api_base} TRACE_FILE={trace_file} "
                f"python3 /opt/api_proxy.py {IN_CONTAINER_PROXY_PORT} > /tmp/proxy.log 2>/app/outputs/{scaffolding}/{model}/traces/log/{html_id}.proxy.log &"
                f"sleep 1 && "
            )
            actual_api_base = f"http://127.0.0.1:{IN_CONTAINER_PROXY_PORT}"
        else:
            proxy_start = ""
            actual_api_base = api_base

        # Generate Vibe config.toml at runtime with review_mcp + score server
        vibe_config = (
            f"[providers.vllm]\\n"
            f"api_base = \\\"{actual_api_base}\\\"\\n"
            f"api_key_env_var = \\\"\\\"\\n"
            f"api_style = \\\"openai\\\"\\n"
            f"backend = \\\"generic\\\"\\n\\n"
            f"[mcp_servers.review]\\n"
            f"transport = \\\"stdio\\\"\\n"
            f"command = \\\"python3\\\"\\n"
            f"args = [\\\"/opt/review/review_mcp.py\\\"]\\n\\n"
            f"[mcp_servers.score]\\n"
            f"transport = \\\"stdio\\\"\\n"
            f"command = \\\"python3\\\"\\n"
            f"args = [\\\"/opt/review/score_server.py\\\", \\\"--html-id\\\", \\\"{html_id}\\\", \\\"--output-dir\\\", \\\"/app/outputs/{scaffolding}/{model}\\\", \\\"--rubrics-dir\\\", \\\"/data/rubrics\\\"]\\n"
        )
        bash_cmd = (
            f"umask 0000 && "
            f"mkdir -p $(dirname {trace_file}) && "
            f"{proxy_start}"
            f"cd /data/resources/htmls && python3 -m http.server 8765 > /tmp/http.log 2>&1 & cd /home/node && "
            f"mkdir -p /home/node/.config/mistral-vibe && "
            f"printf '{vibe_config}' > /home/node/.config/mistral-vibe/config.toml && "
            f"sleep 1 && "
            f"vibe --non-interactive --auto-approve "
            f"--model vllm/{model} "
            f"--instructions /home/node/INSTRUCTIONS.md "
            f"--prompt \"$(cat /tmp/prompt.md)\" ; "
            f"for _retry in 1 2 3 4 5; do "
            f"if [ -f \"{score_path}\" ]; then break; fi ; "
            f"if [ -f \"{progress_path}\" ]; then "
            f"SCORED=$(python3 -c \"import json; d=json.load(open('{progress_path}')); print(len(d.get('scores',{{}})))\" 2>/dev/null || echo 0) ; "
            f"HAS_SUMMARY=$(python3 -c \"import json; d=json.load(open('{progress_path}')); print('1' if d.get('interaction_summary') else '0')\" 2>/dev/null || echo 0) ; "
            f"if [ \"$SCORED\" -ge 10 ] && [ \"$HAS_SUMMARY\" = \"1\" ]; then "
            f"echo \"[RESUME $_retry] finalize-only (scored=$SCORED)\" >&2 ; "
            f"vibe --non-interactive --auto-approve --continue --prompt \"{finalize_only_prompt}\" ; "
            f"else "
            f"echo \"[RESUME $_retry] incomplete (scored=$SCORED summary=$HAS_SUMMARY)\" >&2 ; "
            f"vibe --non-interactive --auto-approve --continue --prompt \"{resume_prompt}\" ; "
            f"fi ; "
            f"else "
            f"echo \"[RESUME $_retry] no score activity\" >&2 ; "
            f"vibe --non-interactive --auto-approve --continue --prompt \"{no_score_prompt}\" ; "
            f"fi ; "
            f"done"
        )
        docker_cmd += [
            "-e", f"OPENAI_API_KEY={model_cfg.get('api_key', 'EMPTY')}",
            "-e", "API_TIMEOUT_MS=600000",
            IMAGE_MAP["mistral_vibe"],
            "bash", "-c",
            bash_cmd,
        ]
    else:
        return {"html_id": html_id, "status": "error", "error": f"Unknown scaffolding: {scaffolding}"}

    try:
        proc = subprocess.Popen(docker_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = proc.communicate(timeout=container_timeout)
        except subprocess.TimeoutExpired:
            # Don't kill the container — leave it running, let end-of-batch handle it
            elapsed = time.time() - start
            log.warning(f"[{html_id}] TIMEOUT after {elapsed:.0f}s (limit={container_timeout}s), container left running")
            # Check if score was written despite timeout (container may have finished scoring)
            fix_permissions(run_home, base_image)
            if score_file.exists():
                log.info(f"[{html_id}] TIMEOUT but score file exists — counting as success")
                status = "success"
            else:
                status = "timeout"
        else:
            elapsed = time.time() - start
            if proc.returncode != 0 and stderr:
                log.warning(f"[{html_id}] docker exit={proc.returncode}: {stderr.decode(errors='replace')[:300]}")
            fix_permissions(run_home, base_image)
            if score_file.exists():
                log.info(f"[{html_id}] DONE in {elapsed:.0f}s")
                status = "success"
            else:
                log.warning(f"[{html_id}] Finished in {elapsed:.0f}s but no score file")
                status = "no_score"

    except Exception as e:
        elapsed = time.time() - start
        log.error(f"[{html_id}] ERROR after {elapsed:.0f}s: {e}")
        status = "error"
    finally:
        shutil.rmtree(rubric_view, ignore_errors=True)
        prompt_file.unlink(missing_ok=True)
        review_mcp_file.unlink(missing_ok=True)
        score_mcp_file.unlink(missing_ok=True)
        if scaffolding == "codex":
            codex_config_file.unlink(missing_ok=True)
        elif scaffolding == "kimi_code_cli":
            kimi_config_file.unlink(missing_ok=True)
            kimi_mcp_file.unlink(missing_ok=True)
        elif scaffolding == "opencode":
            opencode_config_file.unlink(missing_ok=True)
        elif scaffolding == "qwen_code":
            passwd_file.unlink(missing_ok=True)

    return {
        "html_id": html_id,
        "status": status,
        "elapsed": round(elapsed, 1),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start + elapsed)),
    }


def load_model_config(config_path: str) -> dict:
    """Load model config JSON."""
    cfg = json.loads(Path(config_path).read_text())
    return {
        "model_name": cfg["model_name"],
        "api_key": cfg.get("api_key", "EMPTY"),
        "openai_base_url": cfg.get("openai_base_url"),
        "anthropic_base_url": cfg.get("anthropic_base_url"),
        "context_window": cfg.get("context_window"),
    }


def main():
    parser = argparse.ArgumentParser(description="Batch agent evaluation")
    parser.add_argument("--config", default=None, help="Path to run config JSON (overridden by CLI args)")
    parser.add_argument("--model-config", default=None, help="Path to model config JSON")
    parser.add_argument("--scaffolding", default=None, choices=["claude_code", "codex", "kimi_code_cli", "opencode", "qwen_code", "mistral_vibe"])
    parser.add_argument("--html-id", default=None, help="Single HTML ID (deprecated, use --html-ids)")
    parser.add_argument("--html-ids", default=None, help="Comma-separated IDs, 'first:N', or 'all' (default: all)")
    parser.add_argument("--concurrent", type=int, default=None)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--output-base", default=None, help="Override OUTPUT_BASE")
    parser.add_argument("--container-timeout", type=int, default=None)
    parser.add_argument("--debug", action="store_true", help="Record raw API traces")
    parser.add_argument("--claude-debug", action="store_true", help="Enable claude --debug verbose logging")
    args = parser.parse_args()

    # Load run config JSON if provided, then overlay CLI args
    run_cfg = {}
    if args.config:
        with open(args.config) as f:
            run_cfg = json.load(f)

    # Merge: CLI args > config file > defaults
    def _get(cli_val, cfg_key, default):
        if cli_val is not None:
            return cli_val
        return run_cfg.get(cfg_key, default)

    args.model_config = _get(args.model_config, "model_config", None)
    args.scaffolding = _get(args.scaffolding, "scaffolding", None)
    args.concurrent = _get(args.concurrent, "concurrent", 1)
    args.container_timeout = _get(args.container_timeout, "container_timeout", 7200)
    args.output_base = _get(args.output_base, "output_base", None)
    if not args.debug:
        args.debug = run_cfg.get("debug", False)
    if not args.claude_debug:
        args.claude_debug = run_cfg.get("claude_debug", False)

    if not args.model_config or not args.scaffolding:
        parser.error("--model-config and --scaffolding are required (via CLI or --config)")

    # Resolve html_ids: CLI --html-id (single, compat) > --html-ids > config > all
    raw_ids = args.html_ids or args.html_id or run_cfg.get("html_ids", None)

    # Resolve model_config to absolute path (config JSON may use relative)
    model_config_path = Path(args.model_config)
    if not model_config_path.is_absolute():
        model_config_path = (BROWSER_AGENT / model_config_path).resolve()
    model_cfg = load_model_config(str(model_config_path))
    model_name = model_cfg["model_name"]

    if args.output_base:
        ob = Path(args.output_base)
        output_base = ob if ob.is_absolute() else (BROWSER_AGENT / ob).resolve()
    else:
        output_base = OUTPUT_BASE
    out_dir = output_base / args.scaffolding / model_name
    scores_dir = out_dir / "results" / "scores"
    workspace_dir = out_dir / "workspace"
    analysis_dir = out_dir / "analysis"
    scores_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    analysis_dir.mkdir(parents=True, exist_ok=True)

    # Write PID file for easy signal management
    pid_file = out_dir / "batch.pid"
    pid_file.write_text(str(os.getpid()))
    log.info(f"PID {os.getpid()} written to {pid_file}")

    ensure_playwright_volume()

    if args.debug:
        (out_dir / "traces").mkdir(parents=True, exist_ok=True)
        log.info("Debug mode: per-container API proxy will log traces")

    # Resolve html_ids from raw_ids
    # Supports: None/"all", list of IDs, Python slice "[:40]" "[10:20]" "[::2]",
    #           Python list "[100,101,102]", comma-separated "100,101,102", single id
    all_html_ids = sorted(f.stem for f in HTMLS_DIR.glob("*.html"))

    def _resolve_ids(raw):
        if raw is None or raw == "all":
            return all_html_ids
        if isinstance(raw, list):
            return [str(x) for x in raw]
        raw = str(raw).strip()
        # Slice syntax: [:40], [10:20], [::2], [10:50:2]
        if raw.startswith("[") and ":" in raw and not any(c.isalpha() for c in raw):
            slice_expr = raw[1:-1]  # strip []
            parts = slice_expr.split(":")
            s = slice(*[int(p) if p else None for p in parts])
            return all_html_ids[s]
        # List syntax: [100, 101, 102]
        if raw.startswith("["):
            import ast
            return [str(x) for x in ast.literal_eval(raw)]
        # Comma-separated: 100,101,102
        if "," in raw:
            return [x.strip() for x in raw.split(",")]
        # Single id
        return [raw]

    html_ids = _resolve_ids(raw_ids)
    log.info(f"Resolved {len(html_ids)} HTML IDs")

    # Resume: skip already scored
    if not args.retry_failed:
        existing = {f.stem for f in scores_dir.glob("*.json")}
        if existing:
            log.info(f"Resuming: {len(existing)} already scored, skipping")
            html_ids = [h for h in html_ids if h not in existing]

    if not html_ids:
        log.info("Nothing to do")
        return

    log.info(f"=== {args.scaffolding}/{model_name} ===")
    log.info(f"Total: {len(html_ids)} HTMLs, concurrent={args.concurrent}")

    # Query vLLM tokens at batch start
    track_tokens = run_cfg.get("track_tokens", False)
    vllm_base_url = model_cfg.get("openai_base_url", "")
    vllm_tokens_start = _query_vllm_tokens(vllm_base_url) if track_tokens else None
    if vllm_tokens_start:
        log.info(f"vLLM tokens at start: prompt={vllm_tokens_start['prompt_tokens']:.0f}, "
                 f"generation={vllm_tokens_start['generation_tokens']:.0f}, "
                 f"cached={vllm_tokens_start['cached_tokens']:.0f}")

    # batch_log.jsonl: append each result as it completes
    batch_log_file = out_dir / "batch_log.jsonl"
    batch_log_lock = threading.Lock()
    # Write header
    with open(batch_log_file, "a") as bf:
        bf.write(json.dumps({"event": "batch_start", "model": model_name, "scaffolding": args.scaffolding,
                             "concurrent": args.concurrent, "total_htmls": len(html_ids),
                             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")

    def _record_result(r):
        results.append(r)
        with batch_log_lock:
            with open(batch_log_file, "a") as bf:
                bf.write(json.dumps(r, ensure_ascii=False) + "\n")

    results = []
    if args.concurrent <= 1:
        for html_id in html_ids:
            if _stop_event.is_set():
                log.info("Stop requested, no more tasks will be dispatched")
                break
            r = run_single(html_id, args.scaffolding, model_cfg, scores_dir, workspace_dir, output_base, args.container_timeout, args.debug, args.claude_debug)
            _record_result(r)
    else:
        # Warmup: stagger launches 5s apart until reaching target concurrency,
        # then let the completion-driven loop maintain the pool.
        with ThreadPoolExecutor(max_workers=args.concurrent) as executor:
            pending = {}
            it = iter(html_ids)

            log.info(f"Warmup: launching tasks with 5s interval until {args.concurrent} concurrent")
            for html_id in itertools.islice(it, args.concurrent):
                if _stop_event.is_set():
                    break
                f = executor.submit(run_single, html_id, args.scaffolding, model_cfg, scores_dir, workspace_dir, output_base, args.container_timeout, args.debug, args.claude_debug)
                pending[f] = html_id
                time.sleep(5)
                if _stop_event.is_set():
                    break

            log.info(f"Warmup complete: {len(pending)} tasks running")

            # Completion-driven loop: when one finishes, submit next
            while pending:
                done, _ = wait(pending.keys(), return_when=FIRST_COMPLETED)
                for f in done:
                    _record_result(f.result())
                    del pending[f]

                    if _stop_event.is_set():
                        continue

                    html_id = next(it, None)
                    if html_id:
                        nf = executor.submit(run_single, html_id, args.scaffolding, model_cfg, scores_dir, workspace_dir, output_base, args.container_timeout, args.debug, args.claude_debug)
                        pending[nf] = html_id

                if _stop_event.is_set() and not pending:
                    log.info("All running tasks completed after stop signal")

    # Wait for timed-out containers to finish on their own.
    # Python's communicate() timed out, but containers are still running.
    # Poll every 60s for up to 30 minutes, checking if they produce score files.
    timeout_ids = [r["html_id"] for r in results if r["status"] == "timeout"]
    if timeout_ids:
        log.info(f"Waiting for {len(timeout_ids)} timed-out containers to finish...")
        wait_deadline = time.time() + 1800  # 30 min grace period
        while time.time() < wait_deadline:
            # Check which ones now have scores
            still_waiting = []
            for html_id in timeout_ids:
                sf = scores_dir / f"{html_id}.json"
                if sf.exists():
                    log.info(f"[{html_id}] Container finished scoring → success")
                else:
                    still_waiting.append(html_id)
            timeout_ids = still_waiting
            if not timeout_ids:
                log.info("All timed-out containers finished scoring")
                break
            # Check if any containers are still running
            check = subprocess.run(
                f"docker ps --filter label={_BATCH_LABEL} -q | wc -l",
                shell=True, capture_output=True, text=True,
            )
            running = int(check.stdout.strip() or "0")
            if running == 0:
                log.info("No containers running, done waiting")
                break
            log.info(f"  {len(timeout_ids)} still waiting, {running} containers alive, "
                     f"{int(wait_deadline - time.time())}s remaining")
            time.sleep(60)

    # Reclassify based on final state
    for r in results:
        if r["status"] == "timeout":
            score_file = scores_dir / f"{r['html_id']}.json"
            if score_file.exists():
                r["status"] = "success"

    # Summary
    by_status = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r["html_id"])

    log.info("=== Summary ===")
    for status, ids in sorted(by_status.items()):
        log.info(f"  {status}: {len(ids)}")
    log.info(f"  Total scored: {len(list(scores_dir.glob('*.json')))}")

    # Kill any remaining timed-out containers
    timed_out = by_status.get("timeout", [])
    if timed_out:
        log.info(f"Killing {len(timed_out)} remaining timed-out containers (label={_BATCH_LABEL})...")
        _kill_own_containers()

    # --- Retry queue: re-run no_score and timeout items (max 3 rounds) ---
    MAX_RETRY_ROUNDS = int(run_cfg.get("max_retry_rounds", 3))
    for retry_round in range(1, MAX_RETRY_ROUNDS + 1):
        if _stop_event.is_set():
            break
        failed_ids = [r["html_id"] for r in results
                      if r["status"] in ("no_score", "timeout")]
        if not failed_ids:
            break
        log.info(f"=== Retry round {retry_round}/{MAX_RETRY_ROUNDS}: "
                 f"{len(failed_ids)} failed items ===")
        for html_id in failed_ids:
            _archive_failed(html_id, retry_round, workspace_dir, scores_dir, out_dir)
        for html_id in failed_ids:
            if _stop_event.is_set():
                break
            r = run_single(html_id, args.scaffolding, model_cfg, scores_dir,
                           workspace_dir, output_base, args.container_timeout,
                           args.debug, args.claude_debug)
            _record_result(r)
            for old_r in results:
                if old_r["html_id"] == html_id and old_r["status"] in ("no_score", "timeout"):
                    old_r["status"] = f"retry{retry_round}_{r['status']}"
                    break
        by_status = {}
        for r in results:
            by_status.setdefault(r["status"], []).append(r["html_id"])
        log.info(f"After retry {retry_round}:")
        for status, ids in sorted(by_status.items()):
            log.info(f"  {status}: {len(ids)}")

    # Final summary
    log.info("=== Final Summary ===")
    final_scored = len(list(scores_dir.glob("*.json")))
    for status, ids in sorted(by_status.items()):
        log.info(f"  {status}: {len(ids)}")
    log.info(f"  Total scored: {final_scored}")

    # Query vLLM tokens at batch end
    vllm_tokens_consumed = None
    if vllm_tokens_start:
        vllm_tokens_end = _query_vllm_tokens(vllm_base_url)
        if vllm_tokens_end:
            vllm_tokens_consumed = {
                "prompt_tokens": vllm_tokens_end["prompt_tokens"] - vllm_tokens_start["prompt_tokens"],
                "generation_tokens": vllm_tokens_end["generation_tokens"] - vllm_tokens_start["generation_tokens"],
                "cached_tokens": vllm_tokens_end["cached_tokens"] - vllm_tokens_start["cached_tokens"],
            }
            log.info(f"vLLM tokens at end: prompt={vllm_tokens_end['prompt_tokens']:.0f}, "
                     f"generation={vllm_tokens_end['generation_tokens']:.0f}, "
                     f"cached={vllm_tokens_end['cached_tokens']:.0f}")
            log.info(f"vLLM tokens consumed: prompt={vllm_tokens_consumed['prompt_tokens']:.0f}, "
                     f"generation={vllm_tokens_consumed['generation_tokens']:.0f}, "
                     f"cached={vllm_tokens_consumed['cached_tokens']:.0f}")

    # Append summary line to batch_log.jsonl
    batch_end_event = {
        "event": "batch_end",
        "summary": {s: len(ids) for s, ids in by_status.items()},
        "total_scored": final_scored,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if vllm_tokens_consumed:
        batch_end_event["vllm_tokens_consumed"] = vllm_tokens_consumed
    with open(batch_log_file, "a") as bf:
        bf.write(json.dumps(batch_end_event, ensure_ascii=False) + "\n")

    # Clean up
    pid_file.unlink(missing_ok=True)
    log.info("Batch complete")


if __name__ == "__main__":
    main()
