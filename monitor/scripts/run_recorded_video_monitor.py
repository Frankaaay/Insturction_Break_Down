#!/usr/bin/env python3
"""Benchmark causal visual-monitor checkpoints on ordinary recorded videos.

The runner never controls a robot or advances the website state machine. It
creates evidence available at each checkpoint, calls a configured VLM provider, validates the
strict observation-only response, and writes reproducible latency/accuracy
records under an ignored artifacts directory.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
BAILIAN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_PROVIDER = "bailian"
PROVIDER_API_KEY_NAMES = {
    "openrouter": "OPENROUTER_API_KEY",
    "bailian": "DASHSCOPE_API_KEY",
}
DEFAULT_MODELS_BY_PROVIDER = {
    "bailian": ["qwen3.7-plus"],
    "openrouter": ["qwen/qwen3.8-max"],
}
ALL_CANDIDATE_MODELS_BY_PROVIDER = {
    "bailian": [
        "qwen3.6-flash",
        "qwen3.7-plus",
        "qwen3.6-plus",
        "stepfun/step-3.7-flash",
        "qwen3.5-omni-plus",
    ],
    "openrouter": [
    "qwen/qwen3.8-max",
    "qwen/qwen3.7-flash",
    "qwen/qwen3.6-flash",
    "google/gemini-3.1-flash-lite",
    ],
}
STATUSES = {"in_progress", "succeeded", "failed", "unknown"}
STRATEGIES = {"frame_packet", "native_video"}
DEFAULT_STRATEGIES = ["native_video"]
REQUIRED_OUTPUT_KEYS = {
    "status",
    "description_zh",
    "failure_reason",
    "evidence",
    "completion_evidence_timestamp_s",
}

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {
            "type": "string",
            "enum": ["in_progress", "succeeded", "failed", "unknown"],
        },
        "description_zh": {"type": "string", "minLength": 1},
        "failure_reason": {"type": ["string", "null"]},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "timestamp_s": {"type": "number", "minimum": 0},
                    "observation": {"type": "string", "minLength": 1},
                },
                "required": ["timestamp_s", "observation"],
            },
        },
        "completion_evidence_timestamp_s": {"type": ["number", "null"]},
    },
    "required": sorted(REQUIRED_OUTPUT_KEYS),
}


class ContractError(ValueError):
    """The manifest or model output violates the benchmark contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "item"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_api_key(project_root: Path, provider: str) -> str:
    key_name = PROVIDER_API_KEY_NAMES[provider]
    key = os.environ.get(key_name, "").strip()
    if key:
        return key
    for env_path in (
        project_root / ".env",
        project_root / "monitor" / ".env",
        project_root.parent / ".env",
    ):
        if not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key_name:
                return value.strip().strip('"').strip("'")
    local_config = project_root / "local_config.py"
    if local_config.is_file():
        namespace: dict[str, Any] = {}
        exec(local_config.read_text(encoding="utf-8"), namespace)
        return str(namespace.get(key_name, "")).strip()
    return ""


def resolve_ffmpeg(explicit: str) -> str:
    def usable(candidate: str) -> bool:
        try:
            process = subprocess.run(
                [candidate, "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            return process.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    if explicit:
        candidate = Path(explicit).resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"FFmpeg does not exist: {candidate}")
        if not usable(str(candidate)):
            raise RuntimeError(f"FFmpeg cannot start successfully: {candidate}")
        return str(candidate)
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if usable(bundled):
            return bundled
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found and usable(found):
        return found
    raise RuntimeError(
        "No usable FFmpeg was found. Install monitor/requirements.txt "
        "or pass --ffmpeg with a working executable path."
    )


def probe_video_duration(ffmpeg: str, video_path: Path) -> float:
    process = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(video_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = process.stderr + "\n" + process.stdout
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        raise ContractError(f"Cannot read video duration: {video_path}")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0:
        raise ContractError(f"Video duration must be positive: {video_path}")
    return duration


def validate_manifest_case(raw: dict[str, Any], manifest_dir: Path, data_dir: Path | None) -> dict[str, Any]:
    required = {
        "id",
        "video",
        "atomic_action",
        "object",
        "target",
        "initial_state",
        "success_criteria",
        "timeout_s",
    }
    missing = sorted(required - set(raw))
    if missing:
        raise ContractError(f"Manifest case is missing {missing}: {raw.get('id', '<unknown>')}")
    case_id = str(raw["id"]).strip()
    if not case_id:
        raise ContractError("Manifest case id cannot be empty")
    video_value = Path(str(raw["video"]))
    if video_value.is_absolute():
        video_path = video_value.resolve()
    else:
        base = data_dir if data_dir is not None else manifest_dir
        video_path = (base / video_value).resolve()
    if not video_path.is_file():
        raise ContractError(f"Video does not exist for {case_id}: {video_path}")
    criteria = raw["success_criteria"]
    if not isinstance(criteria, list) or not criteria or not all(
        isinstance(item, str) and item.strip() for item in criteria
    ):
        raise ContractError(f"success_criteria must be a non-empty string list: {case_id}")
    timeout_s = float(raw["timeout_s"])
    if timeout_s <= 0:
        raise ContractError(f"timeout_s must be positive: {case_id}")
    case = dict(raw)
    case.update(
        {
            "id": case_id,
            "video_path": video_path,
            "success_criteria": [item.strip() for item in criteria],
            "timeout_s": timeout_s,
        }
    )
    checkpoints = case.get("checkpoints", [])
    if not isinstance(checkpoints, list):
        raise ContractError(f"checkpoints must be a list: {case_id}")
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, dict) or float(checkpoint.get("time_s", 0)) <= 0:
            raise ContractError(f"Every checkpoint needs a positive time_s: {case_id}")
        expected = checkpoint.get("expected_status")
        if expected is not None and expected not in STATUSES:
            raise ContractError(f"Invalid expected_status {expected!r}: {case_id}")
        if expected == "failed":
            reason = checkpoint.get("expected_failure_reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ContractError(f"Failed checkpoint needs expected_failure_reason: {case_id}")
    return case


def load_manifest(path: Path, data_dir: Path | None = None) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ContractError("Manifest schema_version must be 1")
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ContractError("Manifest cases must be a non-empty list")
    cases = [validate_manifest_case(item, path.parent.resolve(), data_dir) for item in raw_cases]
    ids = [item["id"] for item in cases]
    if len(ids) != len(set(ids)):
        raise ContractError("Manifest case ids must be unique")
    return {"schema_version": 1, "cases": cases}


def make_checkpoints(
    duration_s: float,
    interval_s: float,
    annotations: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    if duration_s <= 0 or interval_s <= 0:
        raise ContractError("duration_s and interval_s must be positive")
    by_millis: dict[int, dict[str, Any]] = {}
    tick = interval_s
    while tick < duration_s - 1e-6:
        by_millis[round(tick * 1000)] = {"time_s": round(tick, 3)}
        tick += interval_s
    by_millis[round(duration_s * 1000)] = {"time_s": round(duration_s, 3)}
    for annotation in annotations:
        time_s = float(annotation["time_s"])
        if time_s > duration_s + 0.05:
            raise ContractError(
                f"Checkpoint {time_s:.3f}s exceeds video duration {duration_s:.3f}s"
            )
        key = round(min(time_s, duration_s) * 1000)
        merged = dict(by_millis.get(key, {"time_s": round(min(time_s, duration_s), 3)}))
        merged.update(annotation)
        merged["time_s"] = round(min(time_s, duration_s), 3)
        by_millis[key] = merged
    return [by_millis[key] for key in sorted(by_millis)]


def run_ffmpeg(command: list[str]) -> None:
    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode != 0:
        details = process.stderr.strip()[-4000:]
        raise RuntimeError(f"FFmpeg failed ({process.returncode}): {details}")


def extract_frame(
    ffmpeg: str,
    video_path: Path,
    timestamp_s: float,
    output_path: Path,
    max_edge: int,
    quality: int,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Container duration often points just after the final decodable frame.
    # Try the requested time first, then bounded earlier points without ever
    # looking beyond the causal checkpoint.
    candidates = []
    for candidate in (timestamp_s, timestamp_s - 0.1, timestamp_s - 0.5):
        candidate = max(0.0, candidate)
        if not any(abs(candidate - prior) < 1e-6 for prior in candidates):
            candidates.append(candidate)
    errors = []
    actual_timestamp_s = None
    for candidate in candidates:
        if output_path.exists():
            output_path.unlink()
        try:
            run_ffmpeg(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{candidate:.6f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    (
                        f"scale={max_edge}:{max_edge}:force_original_aspect_ratio=decrease:"
                        "force_divisible_by=2"
                    ),
                    "-q:v",
                    str(quality),
                    str(output_path),
                ]
            )
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        if output_path.is_file() and output_path.stat().st_size > 0:
            actual_timestamp_s = candidate
            break
    if actual_timestamp_s is None:
        raise RuntimeError(
            f"FFmpeg produced no frame at or before {timestamp_s:.3f}s: " + " | ".join(errors)
        )
    return {
        "path": str(output_path),
        "requested_timestamp_s": round(timestamp_s, 3),
        "timestamp_s": round(actual_timestamp_s, 3),
        "bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
    }


def linspace(start: float, end: float, count: int) -> list[float]:
    if count <= 1:
        return [end]
    step = (end - start) / (count - 1)
    return [start + index * step for index in range(count)]


def prepare_frame_packet(
    ffmpeg: str,
    video_path: Path,
    checkpoint_s: float,
    output_dir: Path,
    window_s: float,
    sample_count: int,
    max_edge: int,
    jpeg_quality: int,
) -> dict[str, Any]:
    window_start = max(0.0, checkpoint_s - window_s)
    entries: list[tuple[str, float]] = [("before", 0.0)]
    entries.extend(("window", value) for value in linspace(window_start, checkpoint_s, sample_count))
    entries.append(("now", checkpoint_s))
    deduplicated: list[tuple[str, float]] = []
    seen: set[int] = set()
    for role, value in entries:
        key = round(value * 1000)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append((role, value))

    frames = []
    for index, (role, timestamp_s) in enumerate(deduplicated):
        frame = extract_frame(
            ffmpeg,
            video_path,
            timestamp_s,
            output_dir / f"{index:02d}_{role}_{timestamp_s:.3f}s.jpg",
            max_edge,
            jpeg_quality,
        )
        frame["role"] = role
        frames.append(frame)
    return {
        "strategy": "frame_packet",
        "checkpoint_s": checkpoint_s,
        "visible_ranges_s": [[0.0, 0.0], [window_start, checkpoint_s]],
        "frames": frames,
        "raw_bytes": sum(item["bytes"] for item in frames),
    }


def prepare_native_video(
    ffmpeg: str,
    video_path: Path,
    checkpoint_s: float,
    output_dir: Path,
    window_s: float,
    overlap_s: float,
    fps: float,
    crf: int,
    max_edge: int,
    jpeg_quality: int,
    max_video_mb: float,
) -> dict[str, Any]:
    # Checkpoints are 4s apart. Extending the segment one second backwards
    # makes [3,8], [7,12], ... overlap while retaining a 4s monitoring step.
    window_start = max(0.0, checkpoint_s - window_s - overlap_s)
    output_dir.mkdir(parents=True, exist_ok=True)
    before = extract_frame(
        ffmpeg,
        video_path,
        0.0,
        output_dir / "before_0.000s.jpg",
        max_edge,
        jpeg_quality,
    )
    before["role"] = "before"
    clip_path = output_dir / f"window_{window_start:.3f}_{checkpoint_s:.3f}.mp4"
    run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{window_start:.6f}",
            "-i",
            str(video_path),
            "-t",
            f"{checkpoint_s - window_start:.6f}",
            "-an",
            "-vf",
            (
                f"fps={fps},scale={max_edge}:{max_edge}:"
                "force_original_aspect_ratio=decrease:force_divisible_by=2"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(clip_path),
        ]
    )
    clip_bytes = clip_path.stat().st_size
    if clip_bytes > max_video_mb * 1024 * 1024:
        raise ContractError(
            f"Native clip is {clip_bytes / 1024 / 1024:.3f} MB, above {max_video_mb:.3f} MB"
        )
    return {
        "strategy": "native_video",
        "checkpoint_s": checkpoint_s,
        "visible_ranges_s": [[0.0, 0.0], [window_start, checkpoint_s]],
        "frames": [before],
        "video": {
            "path": str(clip_path),
            "start_s": round(window_start, 3),
            "end_s": round(checkpoint_s, 3),
            "fps": fps,
            "crf": crf,
            "bytes": clip_bytes,
            "sha256": sha256_file(clip_path),
        },
        "raw_bytes": before["bytes"] + clip_bytes,
    }


def data_url(path: Path, mime: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def case_prompt(case: dict[str, Any], checkpoint: dict[str, Any], previous_status: str) -> str:
    criteria = "\n".join(f"- {item}" for item in case["success_criteria"])
    return (
        "请判断这个原子操作在当前检查点的可见状态。\n\n"
        f"原子操作：{case['atomic_action']}\n"
        f"操作对象：{case['object']}\n"
        f"目标状态：{case['target']}\n"
        f"初始可见状态：{case['initial_state']}\n"
        f"成功视觉条件：\n{criteria}\n"
        f"当前检查点：{float(checkpoint['time_s']):.3f} 秒\n"
        f"操作超时：{float(case['timeout_s']):.3f} 秒\n"
        f"上一次状态：{previous_status}\n\n"
        "所有视觉素材都来自同一段视频，并按文字标签给出的时间排列。"
        "只返回系统消息规定的 JSON 对象。"
    )


def build_payload(
    model: str,
    system_prompt: str,
    case: dict[str, Any],
    checkpoint: dict[str, Any],
    evidence_packet: dict[str, Any],
    previous_status: str,
    max_tokens: int,
    reasoning_effort: str,
    structured_output: bool,
    provider: str = "openrouter",
) -> tuple[dict[str, Any], int]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": case_prompt(case, checkpoint, previous_status)}
    ]
    if evidence_packet["strategy"] == "frame_packet":
        for frame in evidence_packet["frames"]:
            content.append(
                {
                    "type": "text",
                    "text": f"图像：{frame['role']}，原视频 t={frame['timestamp_s']:.3f}s",
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url(Path(frame["path"]), "image/jpeg")},
                }
            )
    else:
        before = evidence_packet["frames"][0]
        video = evidence_packet["video"]
        content.extend(
            [
                {"type": "text", "text": "初始锚点图像：原视频 t=0.000s"},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url(Path(before["path"]), "image/jpeg")},
                },
                {
                    "type": "text",
                    "text": (
                        "连续视频窗口：原视频 "
                        f"t={video['start_s']:.3f}s 到 t={video['end_s']:.3f}s；"
                        "模型报告的时间戳必须使用原视频绝对时间。"
                    ),
                },
                {
                    "type": "video_url",
                    "video_url": {"url": data_url(Path(video["path"]), "video/mp4")},
                },
            ]
        )
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if provider == "openrouter":
        if reasoning_effort != "none":
            payload["reasoning"] = {"effort": reasoning_effort, "exclude": True}
        else:
            # Omitting this object lets several OpenRouter models turn reasoning on
            # by default. Explicitly disable it for latency-oriented Flash runs.
            payload["reasoning"] = {"enabled": False}
    elif provider == "bailian":
        # Qwen3.7/3.6 structured output is supported with visual input in
        # non-thinking mode. Keep the latency benchmark explicit and reproducible.
        payload["enable_thinking"] = reasoning_effort != "none"
    else:
        raise ContractError(f"Unsupported provider: {provider}")
    if structured_output:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "recorded_video_monitor",
                "strict": True,
                "schema": OUTPUT_SCHEMA,
            },
        }
    body_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return payload, body_bytes


def response_text(response: dict[str, Any]) -> str:
    content = response["choices"][0]["message"]["content"]
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content)


def parse_json_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```")
        stripped = stripped.removesuffix("```").strip()
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ContractError("Model output must be one JSON object")
    return value


def timestamp_visible(timestamp_s: float, packet: dict[str, Any]) -> bool:
    if packet["strategy"] == "frame_packet":
        return any(
            abs(timestamp_s - float(item["timestamp_s"])) <= 0.075
            or (
                item.get("requested_timestamp_s") is not None
                and abs(timestamp_s - float(item["requested_timestamp_s"])) <= 0.075
            )
            for item in packet["frames"]
        )
    return any(
        float(start) - 0.075 <= timestamp_s <= float(end) + 0.075
        for start, end in packet["visible_ranges_s"]
    )


def validate_model_output(result: dict[str, Any], packet: dict[str, Any]) -> None:
    keys = set(result)
    if keys != REQUIRED_OUTPUT_KEYS:
        raise ContractError(
            f"Output keys must be exactly {sorted(REQUIRED_OUTPUT_KEYS)}; got {sorted(keys)}"
        )
    status = result["status"]
    if status not in STATUSES:
        raise ContractError(f"Invalid status: {status!r}")
    if not isinstance(result["description_zh"], str) or not result["description_zh"].strip():
        raise ContractError("description_zh must be a non-empty string")
    failure_reason = result["failure_reason"]
    if status == "failed":
        if not isinstance(failure_reason, str) or not failure_reason.strip():
            raise ContractError("failed requires a non-empty failure_reason")
    elif failure_reason is not None:
        raise ContractError("failure_reason must be null unless status is failed")
    evidence = result["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise ContractError("evidence must be a non-empty list")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"timestamp_s", "observation"}:
            raise ContractError("Every evidence item needs exactly timestamp_s and observation")
        timestamp_s = item["timestamp_s"]
        if isinstance(timestamp_s, bool) or not isinstance(timestamp_s, (int, float)):
            raise ContractError("Evidence timestamp_s must be a number")
        if not timestamp_visible(float(timestamp_s), packet):
            raise ContractError(f"Evidence timestamp is outside sent visual evidence: {timestamp_s}")
        if not isinstance(item["observation"], str) or not item["observation"].strip():
            raise ContractError("Evidence observation must be a non-empty string")
    completion = result["completion_evidence_timestamp_s"]
    if status == "succeeded":
        if isinstance(completion, bool) or not isinstance(completion, (int, float)):
            raise ContractError("succeeded requires completion_evidence_timestamp_s")
        if not timestamp_visible(float(completion), packet):
            raise ContractError("Completion timestamp is outside sent visual evidence")
    elif completion is not None:
        raise ContractError("completion_evidence_timestamp_s must be null unless succeeded")


def provider_once(
    payload: dict[str, Any],
    api_key: str,
    timeout_s: float,
    provider: str,
    endpoint_url: str,
) -> tuple[dict[str, Any], dict[str, float]]:
    encoding_started = time.perf_counter()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    encoding_done = time.perf_counter()
    request = urllib.request.Request(
        endpoint_url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    if provider == "openrouter":
        request.add_header("HTTP-Referer", "https://github.com/local/planner-monitor")
        request.add_header("X-Title", "Planner Monitor Recorded Video Benchmark")
    request_started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            headers_received = time.perf_counter()
            first_byte = response.read(1)
            first_byte_received = time.perf_counter()
            raw = first_byte + response.read()
            response_done = time.perf_counter()
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{provider} HTTP {exc.code}: {details[:4000]}") from exc
    return json.loads(raw.decode("utf-8")), {
        "payload_encoding_seconds": encoding_done - encoding_started,
        "request_to_headers_seconds": headers_received - request_started,
        "request_to_first_byte_seconds": first_byte_received - request_started,
        "response_read_seconds": response_done - first_byte_received,
        "request_total_seconds": response_done - request_started,
    }


def request_with_retries(
    payload: dict[str, Any],
    api_key: str,
    timeout_s: float,
    retries: int,
    retry_delay_s: float,
    provider: str = "openrouter",
    endpoint_url: str = OPENROUTER_URL,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    attempts = []
    for attempt_no in range(1, retries + 2):
        started = time.perf_counter()
        try:
            response, timing = provider_once(payload, api_key, timeout_s, provider, endpoint_url)
            attempts.append({"attempt_no": attempt_no, "status": "ok", **timing})
            return response, attempts
        except Exception as exc:
            attempts.append(
                {
                    "attempt_no": attempt_no,
                    "status": "error",
                    "elapsed_seconds": time.perf_counter() - started,
                    "error": str(exc),
                }
            )
            if attempt_no > retries:
                return None, attempts
            time.sleep(retry_delay_s * attempt_no)
    return None, attempts


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def safe_ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize_group(records: list[dict[str, Any]], sla_seconds: float, min_requests: int) -> dict[str, Any]:
    responded = [item for item in records if item.get("raw_response_path")]
    valid = [item for item in responded if item.get("contract_valid")]
    latencies = [
        float(item["evidence_to_result_seconds"])
        for item in records
        if item.get("evidence_to_result_seconds") is not None
        and item.get("request_status") != "dry_run"
    ]
    total_latencies = [
        float(item["wall_total_seconds"])
        for item in records
        if item.get("wall_total_seconds") is not None
        and item.get("request_status") != "dry_run"
    ]
    truth = [item for item in valid if item.get("expected_status") in STATUSES]
    expected_success = [item for item in truth if item["expected_status"] == "succeeded"]
    predicted_success = [item for item in truth if item["model_output"]["status"] == "succeeded"]
    true_success = [item for item in predicted_success if item["expected_status"] == "succeeded"]
    expected_failed = [item for item in truth if item["expected_status"] == "failed"]
    detected_failed = [item for item in expected_failed if item["model_output"]["status"] == "failed"]
    recoverable = [item for item in truth if item.get("recoverable_event")]
    recoverable_false_failed = [
        item for item in recoverable if item["model_output"]["status"] == "failed"
    ]
    succeeded_records = [item for item in valid if item["model_output"]["status"] == "succeeded"]
    evidence_ok = [item for item in succeeded_records if item.get("completion_evidence_path")]
    exact = [item for item in truth if item["model_output"]["status"] == item["expected_status"]]
    p95 = percentile(latencies, 0.95)
    json_rate = safe_ratio(len(valid), len(responded))
    request_success_rate = safe_ratio(len(valid), len(records))
    sla_eligible_rate = safe_ratio(
        sum(bool(item.get("sla_eligible")) for item in records), len(records)
    )
    success_precision = safe_ratio(len(true_success), len(predicted_success))
    failure_recall = safe_ratio(len(detected_failed), len(expected_failed))
    recoverable_false_failure_rate = safe_ratio(len(recoverable_false_failed), len(recoverable))
    completion_evidence_rate = safe_ratio(len(evidence_ok), len(succeeded_records))
    gate_details = {
        "enough_requests": len(records) >= min_requests,
        "p95_within_sla": p95 is not None and p95 <= sla_seconds,
        "json_valid_rate_100pct": json_rate == 1.0,
        "all_requests_returned_valid_output": request_success_rate == 1.0,
        "success_precision_at_least_95pct": success_precision is not None and success_precision >= 0.95,
        "failure_recall_100pct": failure_recall is not None and failure_recall == 1.0,
        "no_recoverable_false_failure": recoverable_false_failure_rate in {None, 0.0},
        "completion_evidence_rate_100pct": completion_evidence_rate in {None, 1.0},
    }
    gate_status = "pass" if all(gate_details.values()) else "fail"
    if not gate_details["enough_requests"]:
        gate_status = "insufficient_data"
    return {
        "request_count": len(records),
        "response_received_count": len(responded),
        "valid_contract_count": len(valid),
        "truth_labeled_count": len(truth),
        "latency_seconds": {
            "sla_metric": "evidence_to_result_seconds",
            "p50": percentile(latencies, 0.50),
            "p95": p95,
            "max": max(latencies) if latencies else None,
            "wall_total_p95_including_preparation": percentile(total_latencies, 0.95),
        },
        "json_valid_rate": json_rate,
        "request_success_rate": request_success_rate,
        "sla_eligible_rate": sla_eligible_rate,
        "status_accuracy": safe_ratio(len(exact), len(truth)),
        "success_precision": success_precision,
        "failure_recall": failure_recall,
        "recoverable_false_failure_rate": recoverable_false_failure_rate,
        "completion_evidence_rate": completion_evidence_rate,
        "failure_reason_semantics": "human_review_required",
        "gate_status": gate_status,
        "gate_details": gate_details,
    }


def build_summary(records: list[dict[str, Any]], sla_seconds: float, min_requests: int) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[
            (
                record.get("provider_requested", "openrouter"),
                record["model_requested"],
                record["strategy"],
            )
        ].append(record)
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "sla_seconds": sla_seconds,
        "minimum_requests_per_combination": min_requests,
        "groups": [
            {
                "provider": provider,
                "model": model,
                "strategy": strategy,
                **summarize_group(items, sla_seconds, min_requests),
            }
            for (provider, model, strategy), items in sorted(groups.items())
        ],
    }


def prepare_evidence(
    args: argparse.Namespace,
    ffmpeg: str,
    case: dict[str, Any],
    checkpoint: dict[str, Any],
    strategy: str,
    output_dir: Path,
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    if strategy == "frame_packet":
        packet = prepare_frame_packet(
            ffmpeg,
            case["video_path"],
            float(checkpoint["time_s"]),
            output_dir,
            args.window_seconds,
            args.frame_count,
            args.max_edge,
            args.jpeg_quality,
        )
    else:
        packet = prepare_native_video(
            ffmpeg,
            case["video_path"],
            float(checkpoint["time_s"]),
            output_dir,
            args.window_seconds,
            args.video_overlap_seconds,
            args.video_fps,
            args.video_crf,
            args.max_edge,
            args.jpeg_quality,
            args.max_video_mb,
        )
    return packet, time.perf_counter() - started


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/recorded_samples/manifest.json"),
        help="Manifest path, relative to monitor/ unless absolute.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Optional base directory for relative video paths; defaults to the manifest directory.",
    )
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--prompt", type=Path, default=Path("prompts/recorded_video_monitor_v2_zh.md"))
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDER_API_KEY_NAMES),
        default=DEFAULT_PROVIDER,
        help="VLM API provider. The recorded-video benchmark defaults to Bailian Beijing.",
    )
    parser.add_argument(
        "--endpoint-url",
        default="",
        help="Optional provider endpoint override; defaults to the provider's Beijing/global endpoint.",
    )
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--all-models", action="store_true")
    parser.add_argument("--strategy", action="append", choices=sorted(STRATEGIES), default=[])
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--interval-seconds", type=float, default=4.0)
    parser.add_argument("--window-seconds", type=float, default=4.0)
    parser.add_argument("--video-overlap-seconds", type=float, default=1.0)
    parser.add_argument("--frame-count", type=int, default=8)
    parser.add_argument("--max-edge", type=int, default=640)
    parser.add_argument("--jpeg-quality", type=int, default=4, help="FFmpeg q:v; lower is higher quality.")
    parser.add_argument("--video-fps", type=float, default=6.0)
    parser.add_argument("--video-crf", type=int, default=28)
    parser.add_argument("--max-video-mb", type=float, default=1.5)
    parser.add_argument("--max-request-mb", type=float, default=8.0)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1000,
        help="Includes hidden reasoning tokens; Qwen3.8-Max truncated JSON at 300 in the kettle pilot.",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high"],
        default="none",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--sla-seconds", type=float, default=5.0)
    parser.add_argument("--minimum-requests", type=int, default=30)
    parser.add_argument("--max-checkpoints", type=int, default=0)
    parser.add_argument("--ffmpeg", default="")
    parser.add_argument("--no-json-schema", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def resolve_under_monitor(monitor_root: Path, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (monitor_root / value).resolve()


def main() -> None:
    args = parse_args()
    monitor_root = Path(__file__).resolve().parents[1]
    project_root = monitor_root.parent
    manifest_path = resolve_under_monitor(monitor_root, args.manifest)
    prompt_path = resolve_under_monitor(monitor_root, args.prompt)
    data_dir = resolve_under_monitor(monitor_root, args.data_dir) if args.data_dir else None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = (
        resolve_under_monitor(monitor_root, args.output_root)
        if args.output_root
        else monitor_root / "artifacts" / "recorded_monitor" / timestamp
    )
    if args.interval_seconds <= 0 or args.window_seconds <= 0:
        raise SystemExit("Interval and window seconds must be positive")
    if args.video_overlap_seconds < 0 or args.video_fps <= 0:
        raise SystemExit("Video overlap cannot be negative and FPS must be positive")
    if args.frame_count < 2:
        raise SystemExit("--frame-count must be at least 2")
    if args.max_edge < 64 or args.max_video_mb <= 0 or args.max_request_mb <= 0:
        raise SystemExit("Image and request size limits must be positive and usable")
    if args.retries < 0:
        raise SystemExit("--retries cannot be negative")

    manifest = load_manifest(manifest_path, data_dir)
    cases = manifest["cases"]
    if args.case:
        wanted = set(args.case)
        cases = [item for item in cases if item["id"] in wanted]
        missing = wanted - {item["id"] for item in cases}
        if missing:
            raise SystemExit(f"Unknown --case values: {sorted(missing)}")
    candidate_models = ALL_CANDIDATE_MODELS_BY_PROVIDER[args.provider]
    default_models = DEFAULT_MODELS_BY_PROVIDER[args.provider]
    models = list(dict.fromkeys(candidate_models if args.all_models else (args.model or default_models)))
    # The current experiment contract uses native video only. Frame packets
    # remain available explicitly to reproduce earlier benchmark artifacts.
    strategies = list(dict.fromkeys(args.strategy or DEFAULT_STRATEGIES))
    system_prompt = prompt_path.read_text(encoding="utf-8")
    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    endpoint_url = args.endpoint_url or (
        BAILIAN_URL if args.provider == "bailian" else OPENROUTER_URL
    )
    api_key = "" if args.dry_run else get_api_key(project_root, args.provider)
    if not args.dry_run and not api_key:
        raise SystemExit(f"{PROVIDER_API_KEY_NAMES[args.provider]} is missing")

    output_root.mkdir(parents=True, exist_ok=True)
    run_manifest = {
        "schema_version": 1,
        "started_at": utc_now(),
        "source_manifest": str(manifest_path),
        "source_manifest_sha256": sha256_file(manifest_path),
        "prompt": str(prompt_path),
        "prompt_sha256": sha256_bytes(system_prompt.encode("utf-8")),
        "models": models,
        "provider": args.provider,
        "endpoint_url": endpoint_url,
        "strategies": strategies,
        "arguments": {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(args).items()
        },
        "ffmpeg": ffmpeg,
        "dry_run": args.dry_run,
    }
    json_dump(output_root / "run_manifest.json", run_manifest)

    records: list[dict[str, Any]] = []
    task_count = 0
    for case in cases:
        duration_s = probe_video_duration(ffmpeg, case["video_path"])
        checkpoints = make_checkpoints(duration_s, args.interval_seconds, case.get("checkpoints", []))
        previous_by_model_strategy: dict[tuple[str, str], str] = defaultdict(lambda: "none")
        for checkpoint in checkpoints:
            for strategy in strategies:
                task_count += 1
                if args.max_checkpoints and task_count > args.max_checkpoints:
                    break
                checkpoint_label = f"t{float(checkpoint['time_s']):09.3f}"
                evidence_dir = output_root / "evidence" / slug(case["id"]) / checkpoint_label / strategy
                try:
                    packet, preparation_seconds = prepare_evidence(
                        args, ffmpeg, case, checkpoint, strategy, evidence_dir
                    )
                except Exception as exc:
                    for model in models:
                        records.append(
                            {
                                "case_id": case["id"],
                                "video": str(case["video_path"]),
                                "video_duration_s": duration_s,
                                "checkpoint_s": checkpoint["time_s"],
                                "expected_status": checkpoint.get("expected_status"),
                                "recoverable_event": bool(checkpoint.get("recoverable_event", False)),
                                "model_requested": model,
                                "provider_requested": args.provider,
                                "strategy": strategy,
                                "request_status": "preparation_error",
                                "error": str(exc),
                            }
                        )
                    continue

                for model in models:
                    key = (model, strategy)
                    evidence_ready_started = time.perf_counter()
                    payload, request_bytes = build_payload(
                        model,
                        system_prompt,
                        case,
                        checkpoint,
                        packet,
                        previous_by_model_strategy[key],
                        args.max_tokens,
                        args.reasoning_effort,
                        not args.no_json_schema,
                        args.provider,
                    )
                    record: dict[str, Any] = {
                        "case_id": case["id"],
                        "video": str(case["video_path"]),
                        "video_sha256": sha256_file(case["video_path"]),
                        "video_duration_s": duration_s,
                        "checkpoint_s": checkpoint["time_s"],
                        "expected_status": checkpoint.get("expected_status"),
                        "expected_failure_reason": checkpoint.get("expected_failure_reason"),
                        "recoverable_event": bool(checkpoint.get("recoverable_event", False)),
                        "model_requested": model,
                        "provider_requested": args.provider,
                        "endpoint_url": endpoint_url,
                        "strategy": strategy,
                        "preparation_seconds": preparation_seconds,
                        "evidence": packet,
                        "request_bytes": request_bytes,
                        "estimated_request_mb": request_bytes / 1024 / 1024,
                        "previous_status_supplied": previous_by_model_strategy[key],
                        "started_at": utc_now(),
                    }
                    if record["estimated_request_mb"] > args.max_request_mb:
                        record.update(
                            {
                                "request_status": "size_rejected",
                                "error": (
                                    f"Request is {record['estimated_request_mb']:.3f} MB, "
                                    f"above {args.max_request_mb:.3f} MB"
                                ),
                            }
                        )
                        records.append(record)
                        continue
                    if args.dry_run:
                        record.update(
                            {
                                "request_status": "dry_run",
                                "evidence_to_result_seconds": time.perf_counter() - evidence_ready_started,
                                "wall_total_seconds": preparation_seconds
                                + (time.perf_counter() - evidence_ready_started),
                            }
                        )
                        records.append(record)
                        print(
                            f"DRY {case['id']} t={checkpoint['time_s']:.3f}s {strategy} "
                            f"{model} request={record['estimated_request_mb']:.3f}MB",
                            flush=True,
                        )
                        continue

                    response, attempts = request_with_retries(
                        payload,
                        api_key,
                        args.timeout,
                        args.retries,
                        args.retry_delay,
                        args.provider,
                        endpoint_url,
                    )
                    record["attempts"] = attempts
                    record["retry_used"] = len(attempts) > 1
                    if response is None:
                        record.update(
                            {
                                "request_status": "error",
                                "contract_valid": False,
                                "error": attempts[-1].get("error", "unknown request error"),
                            }
                        )
                    else:
                        raw_dir = output_root / "raw" / slug(model) / slug(case["id"]) / strategy
                        raw_path = raw_dir / f"{checkpoint_label}.json"
                        json_dump(raw_path, response)
                        record["raw_response_path"] = str(raw_path)
                        record["model_returned"] = response.get("model")
                        record["usage"] = response.get("usage", {})
                        choices = response.get("choices") or [{}]
                        record["finish_reason"] = choices[0].get("finish_reason")
                        try:
                            result = parse_json_text(response_text(response))
                            validate_model_output(result, packet)
                            record["model_output"] = result
                            record["contract_valid"] = True
                            record["request_status"] = "ok"
                            previous_by_model_strategy[key] = result["status"]
                        except Exception as exc:
                            record["request_status"] = "invalid_output"
                            record["contract_valid"] = False
                            record["contract_error"] = str(exc)
                            record["model_text"] = response_text(response)
                        if record.get("contract_valid") and result["status"] == "succeeded":
                            try:
                                completion_dir = (
                                    output_root
                                    / "completion_evidence"
                                    / slug(model)
                                    / slug(case["id"])
                                    / strategy
                                )
                                completion_path = completion_dir / f"{checkpoint_label}.jpg"
                                extracted = extract_frame(
                                    ffmpeg,
                                    case["video_path"],
                                    float(result["completion_evidence_timestamp_s"]),
                                    completion_path,
                                    args.max_edge,
                                    args.jpeg_quality,
                                )
                                record["completion_evidence_path"] = extracted["path"]
                                record["completion_evidence"] = extracted
                            except Exception as exc:
                                record["request_status"] = "completion_evidence_error"
                                record["completion_evidence_error"] = str(exc)
                    evidence_to_result = time.perf_counter() - evidence_ready_started
                    record["evidence_to_result_seconds"] = evidence_to_result
                    record["wall_total_seconds"] = preparation_seconds + evidence_to_result
                    record["sla_eligible"] = (
                        record["request_status"] == "ok"
                        and not record.get("retry_used")
                        and evidence_to_result <= args.sla_seconds
                    )
                    records.append(record)
                    print(
                        f"{record['request_status'].upper()} {case['id']} "
                        f"t={checkpoint['time_s']:.3f}s {strategy} {model} "
                        f"latency={evidence_to_result:.3f}s",
                        flush=True,
                    )
            if args.max_checkpoints and task_count >= args.max_checkpoints:
                break
        if args.max_checkpoints and task_count >= args.max_checkpoints:
            break

    json_dump(output_root / "records.json", {"schema_version": 1, "records": records})
    summary = build_summary(records, args.sla_seconds, args.minimum_requests)
    summary["dry_run"] = args.dry_run
    json_dump(output_root / "summary.json", summary)
    print(f"Records: {output_root / 'records.json'}")
    print(f"Summary: {output_root / 'summary.json'}")


if __name__ == "__main__":
    try:
        main()
    except (ContractError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
