# -*- coding: utf-8 -*-
"""Realtime visual-monitor service for short RealSense video windows."""

from __future__ import annotations

import asyncio
import base64
import copy
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
from visual_contracts import build_chain_monitor_prompt, build_monitor_prompt


BAILIAN_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MAX_UPLOAD_BYTES = 3 * 1024 * 1024
MONITOR_WINDOW_SECONDS = 6.0
MONITOR_TIMESTAMP_MAX = MONITOR_WINDOW_SECONDS + 0.2
MONITOR_SUCCESS_EVIDENCE_MIN = MONITOR_WINDOW_SECONDS - 1.0

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status", "description_zh", "failure_reason", "evidence",
        "completion_evidence_timestamp_s",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["in_progress", "succeeded", "failed"]},
        "description_zh": {"type": "string"},
        "failure_reason": {"type": ["string", "null"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["timestamp_s", "observation"],
                "properties": {
                    "timestamp_s": {"type": "number", "minimum": 0, "maximum": MONITOR_TIMESTAMP_MAX},
                    "observation": {"type": "string"},
                },
            },
        },
        "completion_evidence_timestamp_s": {"type": ["number", "null"], "minimum": 0, "maximum": MONITOR_TIMESTAMP_MAX},
    },
}

CHAIN_STEP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "step_id", "status", "description_zh", "failure_reason", "evidence",
        "completion_evidence_timestamp_s",
    ],
    "properties": {
        "step_id": {"type": "string"},
        "status": {"type": "string", "enum": ["in_progress", "succeeded", "failed"]},
        "description_zh": {"type": "string"},
        "failure_reason": {"type": ["string", "null"]},
        "evidence": OUTPUT_SCHEMA["properties"]["evidence"],
        "completion_evidence_timestamp_s": {
            "type": ["number", "null"], "minimum": 0, "maximum": MONITOR_TIMESTAMP_MAX,
        },
    },
}

CHAIN_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "description_zh", "step_updates", "task_completion_evidence_timestamp_s"],
    "properties": {
        "status": {"type": "string", "enum": ["in_progress", "succeeded", "failed"]},
        "description_zh": {"type": "string"},
        "step_updates": {"type": "array", "minItems": 1, "items": CHAIN_STEP_SCHEMA},
        "task_completion_evidence_timestamp_s": {
            "type": ["number", "null"], "minimum": 0, "maximum": MONITOR_TIMESTAMP_MAX,
        },
    },
}


class ModelResponseValidationError(ValueError):
    def __init__(
        self, message: str, *, raw_body: str, actual_model: str,
        timings_ms: dict[str, float], model_content: Any = None,
        normalized_json: dict[str, Any] | None = None,
        normalization_applied: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_body = raw_body
        self.actual_model = actual_model
        self.timings_ms = timings_ms
        self.model_content = model_content
        self.normalized_json = normalized_json
        self.normalization_applied = normalization_applied or []


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


def depth_input_instructions(depth_min_m: float, depth_max_m: float) -> str:
    return f"""视觉输入说明：
- 6 秒 WINDOW 的每一帧左右拼接且严格同步：左侧是 RGB，右侧是已对齐到 RGB 坐标的深度伪彩图。
- 深度图使用整段固定范围 {depth_min_m:.2f}～{depth_max_m:.2f} 米：红色表示靠近相机，蓝色表示远离相机，黑色表示无有效深度。
- 物体身份、颜色、类别必须以 RGB 为准；深度只辅助判断真实空间移动、离开或接触支撑面、前后关系和遮挡下的几何变化。
- 不得把深度空洞、黑色无效区域、物体边缘噪声或快速运动拖影解释为物体消失、拿起、掉落或放置成功。
- NOW RGB 是当前最终状态的主要依据；NOW DEPTH 与其同步，只作为当前几何状态的辅助证据。"""


def build_multimodal_content(
    *,
    prompt: str,
    baseline_data: str,
    video_data: str,
    now_data: str,
    chain_mode: bool,
    visual_input_format: str,
    depth_min_m: float,
    depth_max_m: float,
    now_depth_data: str | None,
) -> list[dict[str, Any]]:
    rgbd = visual_input_format == "rgb_depth_side_by_side"
    if rgbd:
        prompt = f"{prompt}\n\n{depth_input_instructions(depth_min_m, depth_max_m)}"
    content = [
        {"type": "text", "text": prompt},
        {"type": "text", "text": "BEFORE RGB：本次整条操作链开始前的初始彩色画面。" if chain_mode else "BEFORE RGB：本原子操作开始前的初始彩色画面。"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{baseline_data}"}},
        {"type": "text", "text": (
            "WINDOW：从 BEFORE 之后到当前检查点的 6 秒同步视频；每帧左侧 RGB、右侧对齐深度。"
            if rgbd else "WINDOW：从 BEFORE 之后到当前检查点的 6 秒 RGB 视频。"
        )},
        {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{video_data}"}},
        {"type": "text", "text": "NOW RGB：检查点结束时的当前彩色画面；最终状态必须以此画面为准。"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{now_data}"}},
    ]
    if rgbd and now_depth_data is not None:
        content.extend([
            {"type": "text", "text": "NOW DEPTH：与 NOW RGB 同步并对齐的深度伪彩图，仅用于辅助当前几何关系。"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{now_depth_data}"}},
        ])
    return content


def normalize_model_json(value: Any) -> tuple[dict[str, Any], list[str]]:
    """Normalize only known provider deviations before strict validation."""
    applied: list[str] = []
    if isinstance(value, list):
        if len(value) != 1 or not isinstance(value[0], dict):
            raise ValueError("模型顶层数组必须只包含一个 JSON 对象")
        value = value[0]
        applied.append("unwrapped_singleton_array")
    if not isinstance(value, dict):
        raise ValueError("模型 JSON 顶层必须是对象")
    normalized = copy.deepcopy(value)
    if normalized.get("status") == "unknown":
        normalized["status"] = "in_progress"
        applied.append("top_status_unknown_to_in_progress")
    updates = normalized.get("step_updates")
    if isinstance(updates, list):
        for index, item in enumerate(updates):
            if isinstance(item, dict) and item.get("status") == "unknown":
                item["status"] = "in_progress"
                applied.append(f"step_{index}_status_unknown_to_in_progress")
    return normalized, applied


def validate_result(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "status", "description_zh", "failure_reason", "evidence",
        "completion_evidence_timestamp_s",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("模型 JSON 字段与契约不一致")
    status = value.get("status")
    if status not in {"in_progress", "succeeded", "failed"}:
        raise ValueError("模型 status 非法")
    reason = value.get("failure_reason")
    if status == "failed":
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("failed 必须给出 failure_reason")
    elif reason is not None:
        raise ValueError("非 failed 状态的 failure_reason 必须为 null")
    completion = value.get("completion_evidence_timestamp_s")
    if status == "succeeded":
        if not isinstance(completion, (int, float)) or not 0 <= completion <= MONITOR_TIMESTAMP_MAX:
            raise ValueError("succeeded 必须给出窗口内完成证据时间")
        if completion < MONITOR_SUCCESS_EVIDENCE_MIN:
            raise ValueError("succeeded 的完成证据必须位于窗口最后 1 秒")
    elif completion is not None:
        raise ValueError("非 succeeded 状态的完成证据时间必须为 null")
    if (
        not isinstance(value.get("description_zh"), str)
        or not value["description_zh"].strip()
        or not isinstance(value.get("evidence"), list)
        or not value["evidence"]
    ):
        raise ValueError("模型描述或 evidence 非法")
    for item in value["evidence"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"timestamp_s", "observation"}
            or not isinstance(item.get("timestamp_s"), (int, float))
            or not 0 <= item["timestamp_s"] <= MONITOR_TIMESTAMP_MAX
            or not isinstance(item.get("observation"), str)
            or not item["observation"].strip()
        ):
            raise ValueError("模型 evidence 条目非法")
    return value


def _validate_observation_fields(value: dict[str, Any], *, final_step: bool = False) -> None:
    status = value.get("status")
    if status not in {"in_progress", "succeeded", "failed"}:
        raise ValueError("模型 status 非法")
    reason = value.get("failure_reason")
    if status == "failed":
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("failed 必须给出 failure_reason")
    elif reason is not None:
        raise ValueError("非 failed 状态的 failure_reason 必须为 null")
    completion = value.get("completion_evidence_timestamp_s")
    if status == "succeeded":
        if not isinstance(completion, (int, float)) or not 0 <= completion <= MONITOR_TIMESTAMP_MAX:
            raise ValueError("succeeded 必须给出窗口内完成证据时间")
        if final_step and completion < MONITOR_SUCCESS_EVIDENCE_MIN:
            raise ValueError("最终步骤成功证据必须位于窗口最后 1 秒")
    elif completion is not None:
        raise ValueError("非 succeeded 状态的完成证据时间必须为 null")
    if not isinstance(value.get("description_zh"), str) or not value["description_zh"].strip():
        raise ValueError("模型 description_zh 非法")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("模型 evidence 非法")
    for item in evidence:
        if (
            not isinstance(item, dict)
            or set(item) != {"timestamp_s", "observation"}
            or not isinstance(item.get("timestamp_s"), (int, float))
            or not 0 <= item["timestamp_s"] <= MONITOR_TIMESTAMP_MAX
            or not isinstance(item.get("observation"), str)
            or not item["observation"].strip()
        ):
            raise ValueError("模型 evidence 条目非法")


def validate_chain_result(value: dict[str, Any], assignment: dict[str, Any]) -> dict[str, Any]:
    required = {"status", "description_zh", "step_updates", "task_completion_evidence_timestamp_s"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("整链模型 JSON 字段与契约不一致")
    all_steps = assignment.get("steps", [])
    current_index = int(assignment.get("current_step_index", 0))
    expected_ids = [step["step_id"] for step in all_steps[current_index:]]
    updates = value.get("step_updates")
    if not isinstance(updates, list) or not updates or len(updates) > len(expected_ids):
        raise ValueError("step_updates 必须是从当前步骤开始的连续前缀")
    if any(not isinstance(item, dict) for item in updates):
        raise ValueError("step_update 必须是 JSON 对象")
    if [item.get("step_id") for item in updates] != expected_ids[:len(updates)]:
        raise ValueError("step_updates 必须是从当前步骤开始的连续前缀")
    step_required = {
        "step_id", "status", "description_zh", "failure_reason", "evidence",
        "completion_evidence_timestamp_s",
    }
    for index, item in enumerate(updates):
        if not isinstance(item, dict) or set(item) != step_required:
            raise ValueError("step_update 字段与契约不一致")
        _validate_observation_fields(item, final_step=current_index + index == len(all_steps) - 1)
    if any(item["status"] != "succeeded" for item in updates[:-1]):
        raise ValueError("第一个未成功步骤之后不得继续输出 step_updates")
    last_status = updates[-1]["status"]
    if len(updates) < len(expected_ids) and last_status == "succeeded":
        raise ValueError("成功前缀不能遗漏紧随其后的未完成步骤")
    all_completed = len(updates) == len(expected_ids) and last_status == "succeeded"
    derived = "succeeded" if all_completed else last_status
    if value.get("status") != derived:
        raise ValueError("顶层 status 与逐步状态不一致")
    task_completion = value.get("task_completion_evidence_timestamp_s")
    if derived == "succeeded":
        if not isinstance(task_completion, (int, float)) or not MONITOR_SUCCESS_EVIDENCE_MIN <= task_completion <= MONITOR_TIMESTAMP_MAX:
            raise ValueError("整链成功证据必须位于窗口最后 1 秒")
    elif task_completion is not None:
        raise ValueError("非整链成功不得填写任务完成证据时间")
    if not isinstance(value.get("description_zh"), str) or not value["description_zh"].strip():
        raise ValueError("整链 description_zh 非法")
    return value


class VisualMonitorService:
    def __init__(
        self,
        update_callback: Callable[..., Awaitable[dict[str, Any]]],
        config: VisualMonitorConfig | None = None,
        success_callback: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        chain_result_callback: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.config = config or VisualMonitorConfig.from_env()
        self.update_callback = update_callback
        self.success_callback = success_callback
        self.chain_result_callback = chain_result_callback
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
        now_path: Path,
        client_timings: dict[str, float],
        now_depth_path: Path | None = None,
        visual_input_format: str = "rgb",
        depth_min_m: float = 0.25,
        depth_max_m: float = 2.0,
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
                now_path=now_path,
                client_timings=client_timings,
                now_depth_path=now_depth_path,
                visual_input_format=visual_input_format,
                depth_min_m=depth_min_m,
                depth_max_m=depth_max_m,
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
            call_result = await self._call_bailian(
                assignment,
                job["sequence"],
                job["baseline_path"],
                job["video_path"],
                job["now_path"],
                job.get("now_depth_path"),
                job.get("visual_input_format", "rgb"),
                float(job.get("depth_min_m", 0.25)),
                float(job.get("depth_max_m", 2.0)),
            )
            if len(call_result) == 4:
                result, timings, actual_model, raw = call_result
                response_diagnostics = {}
            else:
                result, timings, actual_model, raw, response_diagnostics = call_result
            chain_mode = assignment.get("monitor_scope") == "chain"
            evidence_url = None
            if chain_mode:
                for item in result["step_updates"]:
                    completion = item.get("completion_evidence_timestamp_s")
                    if completion is not None:
                        evidence_path = self.media_path(".jpg")
                        await asyncio.to_thread(self._extract_frame, job["video_path"], evidence_path, float(completion))
                        item["completion_evidence_url"] = f"/api/visual-monitor/media/{evidence_path.name}"
            else:
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
                "now_url": f"/api/visual-monitor/media/{job['now_path'].name}",
                "now_depth_url": (
                    f"/api/visual-monitor/media/{job['now_depth_path'].name}"
                    if job.get("now_depth_path") else None
                ),
                "visual_input_format": job.get("visual_input_format", "rgb"),
                "depth_range_m": (
                    [job.get("depth_min_m"), job.get("depth_max_m")]
                    if job.get("visual_input_format") == "rgb_depth_side_by_side" else None
                ),
                "completion_evidence_url": evidence_url,
                "timings_ms": {
                    **job["client_timings"],
                    **timings,
                    "server_job_total": round((time.perf_counter() - started) * 1000, 1),
                },
                "observed_at": utc_iso(),
            }
            log_path = job["video_path"].with_suffix(".json")
            log_path.write_text(json.dumps({
                "latest": latest,
                "raw": raw,
                **response_diagnostics,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                if chain_mode:
                    if self.chain_result_callback is None:
                        raise RuntimeError("Visual Monitor 未配置整链结果回调")
                    await self.chain_result_callback(**common, latest=latest)
                elif result["status"] == "succeeded":
                    if self.success_callback is None:
                        raise RuntimeError("Visual Monitor 未配置成功自动推进回调")
                    await self.success_callback(**common, latest=latest)
                else:
                    next_state = "awaiting_confirmation" if result["status"] == "failed" else "observing"
                    await self.update_callback(
                        **common,
                        patch={
                            "state": next_state, "latest": latest, "active_sequence": None,
                            "pipeline": {
                                "phase": "completed", "sequence": job["sequence"],
                                "phase_started_at": latest["observed_at"],
                                "timings_ms": latest["timings_ms"],
                            },
                        },
                        event_type="visual_monitor.observation",
                    )
            except ExecutionConflictError:
                # The operator may have confirmed the step while inference was in flight.
                # The execution manager rejects that stale attempt; never let it overwrite
                # the newly active step or turn the completed job into a second error update.
                return
        except Exception as exc:
            error_timings = getattr(exc, "timings_ms", {})
            error_latest = {
                "status": "in_progress",
                "description_zh": "视觉模型请求失败",
                "failure_reason": None,
                "error": str(exc),
                "sequence": job["sequence"],
                "model_requested": self.config.model,
                "model_actual": getattr(exc, "actual_model", None),
                "video_url": f"/api/visual-monitor/media/{job['video_path'].name}",
                "now_url": f"/api/visual-monitor/media/{job['now_path'].name}",
                "now_depth_url": (
                    f"/api/visual-monitor/media/{job['now_depth_path'].name}"
                    if job.get("now_depth_path") else None
                ),
                "visual_input_format": job.get("visual_input_format", "rgb"),
                "timings_ms": {
                    **job["client_timings"],
                    **error_timings,
                    "server_job_total": round((time.perf_counter() - started) * 1000, 1),
                },
                "observed_at": utc_iso(),
            }
            job["video_path"].with_suffix(".json").write_text(
                json.dumps({
                    "latest": error_latest,
                    "raw": getattr(exc, "raw_body", None),
                    "model_content": getattr(exc, "model_content", None),
                    "normalized_json": getattr(exc, "normalized_json", None),
                    "normalization_applied": getattr(exc, "normalization_applied", []),
                    "validation_error": str(exc),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                await self.update_callback(
                    **common,
                    patch={
                        "state": "error", "active_sequence": None,
                        "latest": error_latest,
                        "pipeline": {
                            "phase": "error", "sequence": job["sequence"],
                            "phase_started_at": error_latest["observed_at"],
                            "error": str(exc),
                            "timings_ms": error_latest["timings_ms"],
                        },
                    },
                    event_type="visual_monitor.error",
                )
            except ExecutionConflictError:
                # A stale attempt is expected during a fast human-confirmed transition.
                return

    async def _call_bailian(
        self, assignment: dict[str, Any], sequence: int, baseline_path: Path,
        video_path: Path, now_path: Path, now_depth_path: Path | None = None,
        visual_input_format: str = "rgb", depth_min_m: float = 0.25,
        depth_max_m: float = 2.0,
    ) -> tuple[dict[str, Any], dict[str, float], str, str, dict[str, Any]]:
        key = os.getenv("DASHSCOPE_API_KEY", "")
        if not key:
            raise RuntimeError("服务器未配置 DASHSCOPE_API_KEY")
        prep_started = time.perf_counter()
        baseline_data = base64.b64encode(baseline_path.read_bytes()).decode("ascii")
        video_data = base64.b64encode(video_path.read_bytes()).decode("ascii")
        now_data = base64.b64encode(now_path.read_bytes()).decode("ascii")
        now_depth_data = (
            base64.b64encode(now_depth_path.read_bytes()).decode("ascii")
            if now_depth_path is not None else None
        )
        prep_ms = (time.perf_counter() - prep_started) * 1000
        chain_mode = assignment.get("monitor_scope") == "chain"
        output_schema = copy.deepcopy(CHAIN_OUTPUT_SCHEMA if chain_mode else OUTPUT_SCHEMA)
        if chain_mode:
            pending_count = len(assignment.get("steps", [])) - int(assignment.get("current_step_index", 0))
            output_schema["properties"]["step_updates"].update({
                "minItems": 1,
                "maxItems": pending_count,
            })
        prompt = build_chain_monitor_prompt(assignment, sequence) if chain_mode else build_monitor_prompt(assignment, sequence)
        content = build_multimodal_content(
            prompt=prompt,
            baseline_data=baseline_data,
            video_data=video_data,
            now_data=now_data,
            chain_mode=chain_mode,
            visual_input_format=visual_input_format,
            depth_min_m=depth_min_m,
            depth_max_m=depth_max_m,
            now_depth_data=now_depth_data,
        )
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": content}],
            "enable_thinking": False,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "chain_visual_monitor_result" if chain_mode else "visual_monitor_result", "strict": True, "schema": output_schema},
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
        timings = {
            "base64_prepare": round(prep_ms, 1),
            "bailian_first_byte": round(((first_byte or finished) - request_started) * 1000, 1),
            "bailian_download": round((finished - (first_byte or finished)) * 1000, 1),
            "bailian_total": round((finished - request_started) * 1000, 1),
        }
        actual_model = self.config.model
        content: Any = None
        normalized: dict[str, Any] | None = None
        normalization_applied: list[str] = []
        try:
            body = json.loads(raw_body)
            if not isinstance(body, dict):
                raise ValueError("百炼响应顶层必须是 JSON 对象")
            actual_model = str(body.get("model") or self.config.model)
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError("百炼响应 choices 非法")
            message = choices[0].get("message")
            if not isinstance(message, dict) or "content" not in message:
                raise ValueError("百炼响应 message.content 非法")
            content = message["content"]
            if isinstance(content, str):
                parsed = json.loads(content)
            elif isinstance(content, (dict, list)):
                parsed = content
            else:
                raise ValueError("模型 message.content 必须是 JSON 文本、对象或数组")
            normalized, normalization_applied = normalize_model_json(parsed)
            result = validate_chain_result(normalized, assignment) if chain_mode else validate_result(normalized)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError, IndexError, AttributeError) as exc:
            raise ModelResponseValidationError(
                str(exc), raw_body=raw_body, actual_model=actual_model, timings_ms=timings,
                model_content=content, normalized_json=normalized,
                normalization_applied=normalization_applied,
            ) from exc
        return result, timings, actual_model, raw_body, {
            "model_content": content,
            "normalized_json": normalized,
            "normalization_applied": normalization_applied,
        }

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
