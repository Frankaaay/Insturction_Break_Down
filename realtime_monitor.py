# -*- coding: utf-8 -*-
"""Realtime visual-monitor service for short camera video windows."""

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
from visual_contracts import build_chain_monitor_prompt, build_monitor_prompt, get_visual_contract


BAILIAN_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MAX_UPLOAD_BYTES = 3 * 1024 * 1024
MONITOR_WINDOW_SECONDS = 7.0
MAX_MONITOR_WINDOW_SECONDS = 15.0
MONITOR_TIMESTAMP_MAX = MONITOR_WINDOW_SECONDS + 0.2
MONITOR_SUCCESS_EVIDENCE_MIN = MONITOR_WINDOW_SECONDS - 1.0
MONITOR_NOW_EVIDENCE_THRESHOLD = MONITOR_WINDOW_SECONDS - 0.05

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


def _window_limits(window_duration_s: float) -> tuple[float, float, float]:
    if not 0 < window_duration_s <= MAX_MONITOR_WINDOW_SECONDS:
        raise ValueError("window_duration_s 必须在 0 到 15 秒之间")
    return (
        window_duration_s + 0.2,
        max(0.0, window_duration_s - 1.0),
        max(0.0, window_duration_s - 0.05),
    )


def output_schema_for_window(window_duration_s: float, *, chain_mode: bool) -> dict[str, Any]:
    """Create a strict schema whose timestamp ceiling matches this video."""
    timestamp_max, _, _ = _window_limits(window_duration_s)
    schema = copy.deepcopy(CHAIN_OUTPUT_SCHEMA if chain_mode else OUTPUT_SCHEMA)

    def replace_timestamp_maximum(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "number" or value.get("type") == ["number", "null"]:
                value["maximum"] = timestamp_max
            for nested in value.values():
                replace_timestamp_maximum(nested)
        elif isinstance(value, list):
            for nested in value:
                replace_timestamp_maximum(nested)

    replace_timestamp_maximum(schema)
    return schema


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


def validate_result(
    value: dict[str, Any], window_duration_s: float = MONITOR_WINDOW_SECONDS,
    terminal_evidence_policy: str = "current_now",
) -> dict[str, Any]:
    if terminal_evidence_policy not in {"current_now", "event_in_window"}:
        raise ValueError("动作终态证据策略非法")
    timestamp_max, success_evidence_min, _ = _window_limits(window_duration_s)
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
        if not isinstance(completion, (int, float)) or not 0 <= completion <= timestamp_max:
            raise ValueError("succeeded 必须给出窗口内完成证据时间")
        if terminal_evidence_policy == "current_now" and completion < success_evidence_min:
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
            or not 0 <= item["timestamp_s"] <= timestamp_max
            or not isinstance(item.get("observation"), str)
            or not item["observation"].strip()
        ):
            raise ValueError("模型 evidence 条目非法")
    return value


def _validate_observation_fields(
    value: dict[str, Any], *, final_step: bool = False,
    terminal_evidence_policy: str = "current_now",
    window_duration_s: float = MONITOR_WINDOW_SECONDS,
) -> None:
    if terminal_evidence_policy not in {"current_now", "event_in_window"}:
        raise ValueError("动作终态证据策略非法")
    timestamp_max, success_evidence_min, _ = _window_limits(window_duration_s)
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
        if not isinstance(completion, (int, float)) or not 0 <= completion <= timestamp_max:
            raise ValueError("succeeded 必须给出窗口内完成证据时间")
        if (
            final_step
            and terminal_evidence_policy == "current_now"
            and completion < success_evidence_min
        ):
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
            or not 0 <= item["timestamp_s"] <= timestamp_max
            or not isinstance(item.get("observation"), str)
            or not item["observation"].strip()
        ):
            raise ValueError("模型 evidence 条目非法")


def validate_chain_result(
    value: dict[str, Any], assignment: dict[str, Any],
    window_duration_s: float = MONITOR_WINDOW_SECONDS,
) -> dict[str, Any]:
    timestamp_max, success_evidence_min, _ = _window_limits(window_duration_s)
    required = {"status", "description_zh", "step_updates", "task_completion_evidence_timestamp_s"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("整链模型 JSON 字段与契约不一致")
    all_steps = assignment.get("steps", [])
    current_index = int(assignment.get("current_step_index", 0))
    final_contract = None
    if all_steps:
        final_step = all_steps[-1]
        final_contract = get_visual_contract(final_step.get("action_id"), final_step.get("logic"))
    final_evidence_policy = (
        final_contract.terminal_evidence_policy if final_contract else "current_now"
    )
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
        _validate_observation_fields(
            item,
            final_step=current_index + index == len(all_steps) - 1,
            terminal_evidence_policy=final_evidence_policy,
            window_duration_s=window_duration_s,
        )
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
        if not isinstance(task_completion, (int, float)) or not 0 <= task_completion <= timestamp_max:
            raise ValueError("整链成功证据必须位于当前窗口内")
        if final_evidence_policy == "current_now" and task_completion < success_evidence_min:
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

    def _history_keyframe_content(
        self, assignment: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int]:
        if assignment.get("monitor_scope") != "chain":
            return [], 0
        steps_by_id = {step["step_id"]: step for step in assignment.get("steps", [])}
        media_prefix = "/api/visual-monitor/media/"
        content: list[dict[str, Any]] = []
        count = 0
        for confirmed in assignment.get("confirmed_steps", []):
            keyframe_url = confirmed.get("success_keyframe_url")
            if not isinstance(keyframe_url, str) or not keyframe_url.startswith(media_prefix):
                raise RuntimeError(f"已确认步骤缺少成功关键帧: {confirmed.get('step_id')}")
            filename = keyframe_url[len(media_prefix):]
            if not filename or Path(filename).name != filename:
                raise RuntimeError("成功关键帧 URL 非法")
            keyframe_path = self.config.storage_root / filename
            if not keyframe_path.is_file() or keyframe_path.stat().st_size == 0:
                raise RuntimeError(f"已确认步骤成功关键帧不存在: {confirmed.get('step_id')}")
            keyframe_data = base64.b64encode(keyframe_path.read_bytes()).decode("ascii")
            step = steps_by_id.get(confirmed.get("step_id"), {})
            evidence_observation = confirmed.get("success_evidence_observation")
            evidence_text = (
                f"冻结证据说明={evidence_observation}。"
                if isinstance(evidence_observation, str) and evidence_observation.strip()
                else ""
            )
            content.extend([
                {"type": "text", "text": (
                    f"HISTORY_STEP_KEYFRAME：step_id={confirmed.get('step_id')}，"
                    f"步骤={step.get('zh') or step.get('action') or '未命名'}，"
                    "后端权威状态=succeeded。该图是此 Step 唯一且冻结的成功关键帧；"
                    f"{evidence_text}不得重新判断、否认或回退该 Step。"
                )},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{keyframe_data}"}},
            ])
            count += 1
        return content, count

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
        window_duration_s: float = MONITOR_WINDOW_SECONDS,
        window_started_at: str | None = None,
        window_ended_at: str | None = None,
    ) -> None:
        _window_limits(window_duration_s)
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
                window_duration_s=window_duration_s,
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
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
            call_result = await self._call_bailian(
                assignment, job["sequence"], job["baseline_path"],
                job["video_path"], job["now_path"], job["window_duration_s"],
            )
            if len(call_result) == 4:
                result, timings, actual_model, raw = call_result
                response_diagnostics = {}
            else:
                result, timings, actual_model, raw, response_diagnostics = call_result
            chain_mode = assignment.get("monitor_scope") == "chain"
            evidence_url = None
            evidence_frame_urls: dict[str, str] = {}

            async def evidence_frame_url(timestamp: float) -> str:
                cache_key = f"{timestamp:.3f}"
                if cache_key not in evidence_frame_urls:
                    evidence_frame_urls[cache_key] = await self._completion_evidence_url(
                        job["video_path"], job["now_path"], timestamp,
                        job["window_duration_s"],
                    )
                return evidence_frame_urls[cache_key]

            async def attach_evidence_frames(evidence: list[dict[str, Any]]) -> None:
                for entry in evidence:
                    try:
                        entry["image_url"] = await evidence_frame_url(float(entry["timestamp_s"]))
                    except (RuntimeError, OSError, subprocess.SubprocessError):
                        # Timeline images are supplemental UI evidence. A single
                        # failed extraction must not discard an otherwise valid
                        # in-progress or failed VLM observation.
                        entry["image_error"] = "证据帧提取失败"

            if chain_mode:
                for item in result["step_updates"]:
                    await attach_evidence_frames(item.get("evidence") or [])
                    completion = item.get("completion_evidence_timestamp_s")
                    if completion is not None:
                        item["completion_evidence_url"] = await evidence_frame_url(float(completion))
            else:
                await attach_evidence_frames(result.get("evidence") or [])
                completion = result.get("completion_evidence_timestamp_s")
                if completion is not None:
                    evidence_url = await evidence_frame_url(float(completion))
            latest = {
                **result,
                "sequence": job["sequence"],
                "model_requested": self.config.model,
                "model_actual": actual_model,
                "video_url": f"/api/visual-monitor/media/{job['video_path'].name}",
                "now_url": f"/api/visual-monitor/media/{job['now_path'].name}",
                "window_duration_s": job["window_duration_s"],
                "window_started_at": job["window_started_at"],
                "window_ended_at": job["window_ended_at"],
                "history_keyframe_count": response_diagnostics.get("history_keyframe_count", 0),
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
                                "window_started_at": job["window_started_at"],
                                "window_ended_at": job["window_ended_at"],
                                "window_duration_s": job["window_duration_s"],
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
                "window_duration_s": job["window_duration_s"],
                "window_started_at": job["window_started_at"],
                "window_ended_at": job["window_ended_at"],
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
                            "window_started_at": job["window_started_at"],
                            "window_ended_at": job["window_ended_at"],
                            "window_duration_s": job["window_duration_s"],
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
        video_path: Path, now_path: Path, window_duration_s: float,
    ) -> tuple[dict[str, Any], dict[str, float], str, str, dict[str, Any]]:
        key = os.getenv("DASHSCOPE_API_KEY", "")
        if not key:
            raise RuntimeError("服务器未配置 DASHSCOPE_API_KEY")
        prep_started = time.perf_counter()
        baseline_data = base64.b64encode(baseline_path.read_bytes()).decode("ascii")
        video_data = base64.b64encode(video_path.read_bytes()).decode("ascii")
        now_data = base64.b64encode(now_path.read_bytes()).decode("ascii")
        historical_content, history_keyframe_count = self._history_keyframe_content(assignment)
        prep_ms = (time.perf_counter() - prep_started) * 1000
        chain_mode = assignment.get("monitor_scope") == "chain"
        output_schema = output_schema_for_window(window_duration_s, chain_mode=chain_mode)
        if chain_mode:
            pending_count = len(assignment.get("steps", [])) - int(assignment.get("current_step_index", 0))
            output_schema["properties"]["step_updates"].update({
                "minItems": 1,
                "maxItems": pending_count,
            })
        prompt = (
            build_chain_monitor_prompt(assignment, sequence, window_duration_s)
            if chain_mode else build_monitor_prompt(assignment, sequence, window_duration_s)
        )
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "text", "text": "CHAIN_BEFORE：本次整条操作链开始前的初始画面。" if chain_mode else "BEFORE：本原子操作开始前的初始画面。"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{baseline_data}"}},
                *historical_content,
                {"type": "text", "text": f"WINDOW：连续时间轴上的本段 {window_duration_s:.3f} 秒视频。"},
                {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{video_data}"}},
                {"type": "text", "text": "NOW：检查点结束时的当前画面；最终状态必须以此画面为准。"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{now_data}"}},
            ]}],
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
            if chain_mode:
                result = validate_chain_result(normalized, assignment, window_duration_s)
            else:
                contract = get_visual_contract(assignment.get("action_id"), assignment.get("logic"))
                if not contract:
                    raise ValueError("当前动作没有 Visual Monitor 动作契约")
                result = validate_result(
                    normalized,
                    window_duration_s,
                    terminal_evidence_policy=contract.terminal_evidence_policy,
                )
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
            "history_keyframe_count": history_keyframe_count,
        }

    async def _completion_evidence_url(
        self, video_path: Path, now_path: Path, timestamp: float,
        window_duration_s: float = MONITOR_WINDOW_SECONDS,
    ) -> str:
        # The final CFR video frame precedes the nominal end timestamp. NOW is
        # authoritative for evidence reported at the dynamic window boundary.
        _, _, now_evidence_threshold = _window_limits(window_duration_s)
        if timestamp >= now_evidence_threshold:
            if not now_path.is_file() or now_path.stat().st_size == 0:
                raise RuntimeError("NOW 完成证据图不存在或为空")
            return f"/api/visual-monitor/media/{now_path.name}"
        evidence_path = self.media_path(".jpg")
        await asyncio.to_thread(self._extract_frame, video_path, evidence_path, timestamp)
        return f"/api/visual-monitor/media/{evidence_path.name}"

    @staticmethod
    def _extract_frame(video_path: Path, output_path: Path, timestamp: float) -> None:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError as exc:
            raise RuntimeError("提取证据图需要 imageio-ffmpeg") from exc
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video_path),
                "-ss", f"{max(0.0, timestamp):.3f}", "-frames:v", "1", "-q:v", "3",
                "-y", str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError(f"无法从视频的 {timestamp:.3f}s 提取完成证据帧")

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
