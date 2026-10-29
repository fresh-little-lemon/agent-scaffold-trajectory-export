#!/usr/bin/env python3
"""Audit a whitebox video and build comparison files after an agent session.

The agent renders ``whitebox.mp4``. This script only makes/validates the
comparison artifacts; it never renders the Three.js scene. The task image's
ffmpeg/ffprobe are used by default (``--local`` is useful for a local fixture).
"""
from __future__ import annotations

import argparse
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile


VERSION = 2


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@lru_cache(maxsize=None)
def image_identity(image: str) -> dict:
    r = subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                       text=True, timeout=60, check=True)
    info = json.loads(r.stdout)[0]
    return {"requested": image, "id": info["Id"], "repo_digests": info.get("RepoDigests", [])}


class Runner:
    """Run fixed tools with argv, never through a shell."""

    def __init__(self, image: str | None, local: bool, results: Path, reference: Path, report: dict):
        self.local = local
        self.results = results
        self.reference = reference
        self.report = report
        self.image = None if local else image_identity(str(image))
        report["runtime"] = {"mode": "local" if local else "docker", "image": self.image}

    def _local_argv(self, command: list[str]) -> list[str]:
        return [str(self.reference) if x == "/ref/source.mp4" else
                str(self.results / x[5:]) if x.startswith("/out/") else x for x in command]

    def run(self, stage: str, command: list[str], timeout: int = 3600) -> str:
        if self.local:
            argv = self._local_argv(command)
        else:
            argv = ["docker", "run", "--rm", "--network", "none",
                    "--user", f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "0:0",
                    "-v", f"{self.results}:/out:rw", "-v", f"{self.reference}:/ref/source.mp4:ro",
                    "--entrypoint", command[0], self.image["id"], *command[1:]]
        entry = {"stage": stage, "argv": argv}
        self.report.setdefault("commands", []).append(entry)
        try:
            r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            entry["error"] = str(exc)
            raise RuntimeError(f"{stage}: {exc}") from exc
        entry.update(returncode=r.returncode, stderr_tail=r.stderr[-4000:])
        if r.returncode:
            raise RuntimeError(f"{stage}: {r.stderr[-2000:] or r.stdout[-2000:]}")
        return r.stdout

    def probe(self, label: str, path: str) -> dict:
        raw = json.loads(self.run(f"probe_{label}", [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=codec_name,width,height,sample_aspect_ratio,display_aspect_ratio,"
            "avg_frame_rate,r_frame_rate,duration,nb_read_frames:format=duration,size",
            "-of", "json", path]))
        self.report.setdefault("ffprobe", {})[label] = raw
        if not raw.get("streams"):
            raise ValueError(f"{label}: no video stream")
        stream = raw["streams"][0]
        duration = float(stream.get("duration", raw.get("format", {}).get("duration", 0)))
        rate = stream.get("avg_frame_rate", "0/0")
        if rate in {"0/0", "0/1", "N/A"}:
            rate = stream.get("r_frame_rate", "0/0")
        fps = Fraction(rate)
        sar_text = stream.get("sample_aspect_ratio", "1:1")
        sar = Fraction(sar_text.replace(":", "/")) if sar_text not in {"N/A", "0:1"} else Fraction(1)
        frames = int(stream.get("nb_read_frames", 0))
        width, height = int(stream["width"]), int(stream["height"])
        if min(width, height, frames) <= 0 or not math.isfinite(duration) or duration <= 0 or fps <= 0:
            raise ValueError(f"{label}: invalid dimensions, frame count, duration or frame rate")
        # A non-empty file or a header probe is not a decode test.
        self.run(f"decode_{label}", ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", path,
                                    "-map", "0:v:0", "-an", "-f", "null", "-"])
        return {"width": width, "height": height, "frames": frames, "duration": duration,
                "fps": str(fps), "sar": str(sar), "display_aspect": width * float(sar) / height,
                "decoded": True}


def even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def one(item: dict, image: str | None, force: bool = False, local: bool = False) -> dict:
    iid = str(item["id"])
    if not iid or iid in {".", ".."} or "/" in iid or "\\" in iid:
        raise ValueError("id must be a single path component")
    root = Path(item["item_root"]).resolve()
    results = (root / "results" / iid).resolve()
    if not results.is_relative_to(root):
        raise ValueError("results directory escapes item_root")
    results.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": VERSION, "id": iid, "status": "failed", "commands": []}
    report_path = results / "postprocess_report.json"
    try:
        previous = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    except (OSError, ValueError):
        previous = {}
    try:
        # This is the clip mounted for the same run, so it already includes
        # clip_start/clip_seconds and cannot accidentally use the full source.
        clip = root.parent.parent / "workspace" / iid / "project" / "input" / f"{iid}.mp4"
        reference = clip if clip.is_file() else Path(item.get("video") or "")
        whitebox = results / "whitebox.mp4"
        report["reference"] = {"path": str(reference),
                               "kind": "workspace_input" if clip.is_file() else "source_window",
                               "clip_start": item.get("clip_start", 0),
                               "clip_seconds": item.get("clip_seconds")}
        if not reference.is_file() or not whitebox.is_file():
            raise FileNotFoundError("reference or whitebox.mp4 is missing")
        report["reference"]["sha256"] = sha256(reference)
        report["whitebox_sha256"] = sha256(whitebox)
        run = Runner(image, local, results, reference.resolve(), report)
        report["runtime"]["versions"] = {
            name: run.run(f"version_{name}", [name, "-version"], 60).splitlines()[0]
            for name in ("ffmpeg", "ffprobe")}
        fingerprint = {"version": VERSION, "reference": dict(report["reference"]),
                       "whitebox_sha256": report["whitebox_sha256"], "runtime": report["runtime"]}
        report["input_fingerprint"] = fingerprint
        reusable = (not force and previous.get("status") == "ok" and
                    previous.get("input_fingerprint") == fingerprint and
                    (results / "compare.mp4").is_file())
        with tempfile.TemporaryDirectory(prefix=".postprocess-", dir=results) as tmp:
            tmp_dir = Path(tmp)
            temp = "/out/" + tmp_dir.name
            ref_path = "/ref/source.mp4"
            if not clip.is_file():
                start, seconds = float(item.get("clip_start", 0)), float(item["clip_seconds"])
                if not math.isfinite(start) or not math.isfinite(seconds) or start < 0 or seconds <= 0:
                    raise ValueError("source fallback needs finite clip_start >= 0 and clip_seconds > 0")
                ref_path = temp + "/reference.mp4"
                run.run("extract_reference_window", ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(start),
                    "-t", str(seconds), "-i", "/ref/source.mp4", "-an", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", ref_path])
                report["reference"]["effective_clip_sha256"] = sha256(tmp_dir / "reference.mp4")
            ref = run.probe("reference_clip", ref_path)
            wb = run.probe("whitebox", "/out/whitebox.mp4")
            duration = min(ref["duration"], wb["duration"])
            fps = Fraction(wb["fps"])
            width, height = even(wb["width"] * float(Fraction(wb["sar"]))), even(wb["height"])
            # Preserve display aspect and square pixels by letterboxing the reference.
            rw = even(min(width, height * ref["display_aspect"]))
            rh = even(min(height, width / ref["display_aspect"]))
            report["alignment"] = {
                "reference": ref, "whitebox": wb, "comparison_duration_seconds": duration,
                "comparison_fps": str(fps), "panel_size": [width, height], "reference_fit": [rw, rh],
                "policy": "zero-based PTS; whitebox fps; square pixels; reference letterboxed; shortest duration",
                "duration_delta_seconds": wb["duration"] - ref["duration"],
                "display_aspect_relative_delta": wb["display_aspect"] / ref["display_aspect"] - 1,
                "reference_tail_excluded_seconds": ref["duration"] - duration,
                "whitebox_tail_excluded_seconds": wb["duration"] - duration}
            checks = report["checks"] = {
                "duration_matches_within_one_second": abs(wb["duration"] - ref["duration"]) <= 1,
                "display_aspect_matches_within_one_percent": abs(wb["display_aspect"] / ref["display_aspect"] - 1) <= .01}
            compare_path = "/out/compare.mp4"
            reused = False
            if reusable:
                try:
                    compared = run.probe("compare", compare_path)
                    reused = sha256(results / "compare.mp4") == previous.get("artifacts", {}).get("compare.mp4", {}).get("sha256")
                except (RuntimeError, ValueError):
                    reused = False
            if not reused:
                compare_path = temp + "/compare.mp4"
                filters = (f"[0:v]setpts=PTS-STARTPTS,fps={fps}:start_time=0,scale={rw}:{rh},setsar=1,"
                           f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black[ref];"
                           f"[1:v]setpts=PTS-STARTPTS,fps={fps}:start_time=0,scale={width}:{height},setsar=1[wb];"
                           "[ref][wb]hstack=inputs=2:shortest=1[v]")
                run.run("build_compare", ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", ref_path,
                    "-i", "/out/whitebox.mp4", "-filter_complex", filters, "-map", "[v]", "-an",
                    "-t", str(duration), "-fps_mode", "passthrough", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", compare_path])
                compared = run.probe("compare", compare_path)
            if (compared["width"], compared["height"], compared["sar"]) != (2 * width, height, "1"):
                raise ValueError("comparison has unexpected dimensions or pixel aspect ratio")
            if abs(compared["duration"] - duration) > 1 / float(fps) + .001:
                raise ValueError("comparison duration differs by more than one output frame")
            # Select the middle decoded frame. End-relative seeking (-sseof -1)
            # selected the last frame in the old implementation.
            middle = compared["frames"] // 2
            report["middle_frame"] = {"index_zero_based": middle,
                                      "time_seconds": middle / float(fps),
                                      "comparison_frame_count": compared["frames"]}
            run.run("build_middle_jpg", ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", compare_path,
                "-vf", f"select=eq(n\\,{middle})", "-frames:v", "1", "-q:v", "2", temp + "/compare.jpg"], 300)
            # A still image needs dimension/decode checks, not a positive video duration.
            jpg_raw = json.loads(run.run("probe_compare_jpg", ["ffprobe", "-v", "error",
                "-show_entries", "stream=width,height,nb_frames", "-of", "json", temp + "/compare.jpg"]))
            report["ffprobe"]["compare_jpg"] = jpg_raw
            jpg_stream = jpg_raw.get("streams", [{}])[0]
            if (int(jpg_stream.get("width", 0)), int(jpg_stream.get("height", 0))) != (2 * width, height):
                raise ValueError("middle JPEG has unexpected dimensions")
            run.run("decode_compare_jpg", ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i",
                temp + "/compare.jpg", "-f", "null", "-"], 300)
            if not reused:
                (tmp_dir / "compare.mp4").replace(results / "compare.mp4")
            (tmp_dir / "compare.jpg").replace(results / "compare.jpg")
            report["compare_reused"] = reused
            report["status"] = "ok" if all(checks.values()) else "needs_review"
            if not (results / "README.md").exists():
                (results / "README.md").write_text(
                    f"# media2threejs result: {iid}\n\n"
                    "Claude Code produced whitebox.mp4. This postprocessor generated the comparison and "
                    "measured report; it did not render or visually assess the scene.\n\n"
                    f"Reference: `{reference}` ({report['reference']['kind']})\n\n"
                    f"Reference / whitebox duration: {ref['duration']:.6f} / {wb['duration']:.6f} s.\n\n"
                    f"Comparison: {2 * width}x{height}, {fps} fps, {compared['frames']} frames; "
                    f"middle JPEG: frame {middle}. Audit status: {report['status']}.\n\n"
                    "See postprocess_report.json for hashes, ffprobe data, decode checks, alignment and commands.\n",
                    encoding="utf-8")
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError, ZeroDivisionError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["artifacts"] = {name: {"bytes": (results / name).stat().st_size, "sha256": sha256(results / name)}
                           for name in ("whitebox.mp4", "compare.mp4", "compare.jpg", "README.md")
                           if (results / name).is_file()}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch-log", required=True, type=Path)
    backend = ap.add_mutually_exclusive_group(required=True)
    backend.add_argument("--image", help="Task image; resolved to an immutable Docker image ID")
    backend.add_argument("--local", action="store_true", help="Use ffmpeg and ffprobe from PATH")
    ap.add_argument("--force", action="store_true", help="Rebuild comparisons even when audited inputs match")
    args = ap.parse_args()
    rows = {}
    for line in args.batch_log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["id"]] = row
    out = []
    for row in rows.values():
        row.setdefault("item_root", str(args.batch_log.parent / "items" / row["id"]))
        out.append(one(row, args.image, args.force, local=args.local))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if all(r["status"] == "ok" for r in out) else 1


if __name__ == "__main__":
    raise SystemExit(main())
