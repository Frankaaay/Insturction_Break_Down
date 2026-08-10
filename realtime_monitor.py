# -*- coding: utf-8 -*-
"""Realtime visual-monitor service for short RealSense video windows."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import httpx

from execution import ExecutionConflictError
from visual_contracts import build_monitor_prompt


BAILIAN_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MAX_UPLOAD_BYTES = 3 * 1024 * 1024

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status", "description_zh", "failure_reason", "evidence",
        "completion_evidence_timestamp_s",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["in_progress", "succeeded", "failed", "unknown"]},
        "description_zh": {"type": "string"},
        "failure_reason": {"type": ["string", "null"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["timestamp_s", "observation"],
                "properties": {
                    "timestamp_s": {"type": "number", "minimum": 0, "maximum": 7.2},
                    "observation": {"type": "string"},
                },
            },
        },
        "completion_evidence_timestamp_s": {"type": ["number", "null"], "minimum": 0, "maximum": 7.2},
    },
}


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class VisualMonitorConfig:
    storage_root: Path
    model: str = "qwen3.7-plus"
    retention_hours: int = 24
    max_concurrency: int = 2
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "VisualMonitorConfig":
        return cls(
            storage_root=Path(os.getenv("VISUAL_MONITOR_STORAGE_ROOT", "monitor/runtime_data")),
            model=os.getenv("VISUAL_MONITOR_MODEL", "qwen3.7-plus"),
            retention_hours=int(os.getenv("VISUAL_MONITOR_RETENTION_HOURS", "24")),
            max_concurrency=int(os.getenv("VISUAL_MONITOR_MAX_CONCURRENCY", "2")),
            timeout_seconds=float(os.getenv("VISUAL_MONITOR_API_TIMEOUT_SECONDS", "30")),
        )


def validate_result(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "status", "description_zh", "failure_reason", "evidence",
        "completion_evidence_timestamp_s",
    }
    if set(value) != required:
        raise ValueError("模型 JSON 字段与契约不一致")
    status = value.get("status")
    if status not in {"in_progress", "succeeded", "failed", "unknown"}:
        raise ValueError("模型 status 非法")
    reason = value.get("failure_reason")
    if status == "failed":
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("failed 必须给出 failure_reason")
    elif reason is not None:
        raise ValueError("非 failed 状态的 failure_reason 必须为 null")
    completion = value.get("completion_evidence_timestamp_s")
    if status == "succeeded":
        if not isinstance(completion, (int, float)) or not 0 <= completion <= 7.2:
            raise ValueError("succeeded 必须给出窗口内完成证据时间")
    elif completion is not None:
        raise ValueError("非 succeeded 状态的完成证据时间必须为 null")
    if not value.get("description_zh", "").strip() or not isinstance(value.get("evidence"), list) or not value["evidence"]:
        raise ValueError("模型描述或 evidence 非法")
    for item in value["evidence"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"timestamp_s", "observation"}
            or not isinstance(item.get("timestamp_s"), (int, float))
            or not 0 <= item["timestamp_s"] <= 7.2
            or not isinstance(item.get("observation"), str)
            or not item["observation"].strip()
        ):
            raise ValueError("模型 evidence 条目非法")
    return value


class VisualMonitorService:
    def __init__(
        self,
        update_callback: Callable[..., Awaitable[dict[str, Any]]],
        config: VisualMonitorConfig | None = None,
    ) -> None:
        self.config = config or VisualMonitorConfig.from_env()
        self.update_callback = update_callback
        self.config.storage_root.mkdir(parents=True, exist_ok=True)
        self._tasks: set[asyncio.Task] = set()
        self._client = httpx.AsyncClient(timeout=self.config.timeout_seconds)
        self._last_cleanup = 0.0

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def media_path(self, suffix: str) -> Path:
        return self.config.storage_root / f"{uuid4().hex}{suffix}"

    async def save_upload(self, upload: Any, suffix: str) -> tuple[Path, int, float]:
        started = time.perf_counter()
        data = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("上传文件超过 3 MB 服务端上限")
        if not data:
            raise ValueError("上传文件为空")
        path = self.media_path(suffix)
        path.write_bytes(data)
        return path, len(data), (time.perf_counter() - started) * 1000

    async def submit(
        self,
        *,
        assignment: dict[str, Any],
        camera_id: str,
        sequence: int,
        baseline_path: Path,
        video_path: Path,
        client_timings: dict[str, float],
    ) -> None:
        if len(self._tasks) >= self.config.max_concurrency:
            raise RuntimeError("Visual Monitor 推理并发已满，请等待下一个检查点")
        task = asyncio.create_task(
            self._run(
                assignment=assignment,
                camera_id=camera_id,
                sequence=sequence,
                baseline_path=baseline_path,
                video_path=video_path,
                client_timings=client_timings,
            ),
            name=f"visual-monitor:{assignment['execution_id']}:{sequence}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, **job: Any) -> None:
        started = time.perf_counter()
        assignment = job["assignment"]
        common = {
            "execution_id": assignment["execution_id"],
            "attempt_id": assignment["attempt_id"],
            "camera_id": job["camera_id"],
        }
        try:
            result, timings, actual_model, raw = await self._call_bailian(
                assignment, job["sequence"], job["baseline_path"], job["video_path"]
            )
            evidence_url = None
            completion = result.get("completion_evidence_timestamp_s")
            if completion is not None:
                evidence_path = self.media_path(".jpg")
                await asyncio.to_thread(self._extract_frame, job["video_path"], evidence_path, float(completion))
                evidence_url = f"/api/visual-monitor/media/{evidence_path.name}"
            latest = {
                **result,
                "sequence": job["sequence"],
                "model_requested": self.config.model,
                "model_actual": actual_model,
                "video_url": f"/api/visual-monitor/media/{job['video_path'].name}",
                "completion_evidence_url": evidence_url,
                "timings_ms": {
                    **job["client_timings"],
                    **timings,
                    "server_job_total": round((time.perf_counter() - started) * 1000, 1),
                },
                "observed_at": utc_iso(),
            }
            log_path = job["video_path"].with_suffix(".json")
            log_path.write_text(json.dumps({"latest": latest, "raw": raw}, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                next_state = (
                    "awaiting_confirmation"
                    if result["status"] in {"succeeded", "failed"}
                    else "observing"
                )
                await self.update_callback(
                    **common,
                    patch={"state": next_state, "latest": latest, "active_sequence": None},
                    event_type="visual_monitor.observation",
                )
            except ExecutionConflictError:
                # The operator may have confirmed the step while inference was in flight.
                # The execution manager rejects that stale attempt; never let it overwrite
                # the newly active step or turn the completed job into a second error update.
                return
        except Exception as exc:
            try:
                await self.update_callback(
                    **common,
                    patch={"state": "error", "active_sequence": None, "latest": {"status": "unknown", "description_zh": "视觉模型请求失败", "failure_reason": None, "error": str(exc), "sequence": job["sequence"], "observed_at": utc_iso()}},
                    event_type="visual_monitor.error",
                )
            except ExecutionConflictError:
                # A stale attempt is expected during a fast human-confirmed transition.
                return

    async def _call_bailian(
        self, assignment: dict[str, Any], sequence: int, baseline_path: Path, video_path: Path
    ) -> tuple[dict[str, Any], dict[str, float], str, str]:
        key = os.getenv("DASHSCOPE_API_KEY", "")
        if not key:
            raise RuntimeError("服务器未配置 DASHSCOPE_API_KEY")
        prep_started = time.perf_counter()
        image_data = base64.b64encode(baseline_path.read_bytes()).decode("ascii")
        video_data = base64.b64encode(video_path.read_bytes()).decode("ascii")
        prep_ms = (time.perf_counter() - prep_started) * 1000
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": build_monitor_prompt(assignment, sequence)},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{video_data}"}},
            ]}],
            "enable_thinking": False,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "visual_monitor_result", "strict": True, "schema": OUTPUT_SCHEMA},
            },
        }
        request_started = time.perf_counter()
        first_byte = None
        async with self._client.stream(
                "POST", BAILIAN_ENDPOINT,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            ) as response:
            chunks = []
            async for chunk in response.aiter_bytes():
                if first_byte is None:
                    first_byte = time.perf_counter()
                chunks.append(chunk)
            response.raise_for_status()
        finished = time.perf_counter()
        raw_body = b"".join(chunks).decode("utf-8")
        body = json.loads(raw_body)
        content = body["choices"][0]["message"]["content"]
        result = validate_result(json.loads(content))
        return result, {
            "base64_prepare": round(prep_ms, 1),
            "bailian_first_byte": round(((first_byte or finished) - request_started) * 1000, 1),
            "bailian_download": round((finished - (first_byte or finished)) * 1000, 1),
            "bailian_total": round((finished - request_started) * 1000, 1),
        }, str(body.get("model") or self.config.model), raw_body

    @staticmethod
    def _extract_frame(video_path: Path, output_path: Path, timestamp: float) -> None:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError as exc:
            raise RuntimeError("提取证据图需要 imageio-ffmpeg") from exc
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, timestamp - 0.05):.3f}", "-i", str(video_path), "-frames:v", "1", "-q:v", "3", "-y", str(output_path)],
            check=True,
            capture_output=True,
        )

    def cleanup_expired(self, *, force: bool = False) -> int:
        now = time.monotonic()
        if not force and now - self._last_cleanup < 300:
            return 0
        self._last_cleanup = now
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.config.retention_hours)
        removed = 0
        for path in self.config.storage_root.iterdir():
            if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                path.unlink()
                removed += 1
        return removed

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._client.aclose()
