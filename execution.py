# -*- coding: utf-8 -*-
"""后端驱动的原子操作执行状态机。

执行数据当前仅保存在单个 Python 进程内。网页和未来的机器人 monitor
都只能通过 report() 上报结果；步骤推进、重试和超时均由本模块决定。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from visual_contracts import supports_visual_contract, visual_contract_metadata


class ExecutionNotFoundError(KeyError):
    """执行会话不存在。"""


class ExecutionConflictError(RuntimeError):
    """请求与执行会话的当前状态冲突。"""


WORKFLOW_NODE_TYPES = {"command", "service", "motion"}
EXECUTION_MODES = {"robot_agent", "visual_monitor", "chain_visual_monitor"}


class SQLiteExecutionStore:
    """Small JSON snapshot store; WAL lets API reads coexist with event writes."""

    def __init__(self, path: str):
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(db_path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS executions "
            "(execution_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS idempotency "
            "(scope TEXT NOT NULL, item_id TEXT NOT NULL, PRIMARY KEY(scope, item_id))"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS robot_workflows ("
            "robot_id TEXT NOT NULL, capability_id TEXT NOT NULL, payload TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, PRIMARY KEY(robot_id, capability_id))"
        )
        self.connection.commit()

    def load(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM executions ORDER BY updated_at"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, execution: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO executions(execution_id, payload, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(execution_id) DO UPDATE SET "
            "payload=excluded.payload, updated_at=excluded.updated_at",
            (
                execution["execution_id"],
                json.dumps(execution, ensure_ascii=False, separators=(",", ":")),
                execution["updated_at"],
            ),
        )
        self.connection.commit()

    def seen(self, scope: str, item_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM idempotency WHERE scope=? AND item_id=?",
            (scope, item_id),
        ).fetchone()
        return row is not None

    def remember(self, scope: str, item_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO idempotency(scope, item_id) VALUES(?, ?)",
            (scope, item_id),
        )
        self.connection.commit()

    def load_workflows(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM robot_workflows ORDER BY updated_at"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_workflow(self, workflow: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO robot_workflows(robot_id, capability_id, payload, updated_at) "
            "VALUES(?, ?, ?, ?) ON CONFLICT(robot_id, capability_id) DO UPDATE SET "
            "payload=excluded.payload, updated_at=excluded.updated_at",
            (
                workflow["robot_id"],
                workflow["capability_id"],
                json.dumps(workflow, ensure_ascii=False, separators=(",", ":")),
                workflow["registered_at"],
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    value = value or _now()
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timeout_from_env() -> float:
    """读取 monitor 超时；非法配置在启动时直接报错。"""
    raw = os.getenv("MONITOR_TIMEOUT_SECONDS", "20")
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError("MONITOR_TIMEOUT_SECONDS 必须是数字") from exc
    if value <= 0:
        raise RuntimeError("MONITOR_TIMEOUT_SECONDS 必须大于 0")
    return value


class ExecutionManager:
    """管理执行会话、GraspArm 子流程、服务端计时器和 SSE 订阅。"""

    def __init__(
        self,
        timeout_seconds: float | None = None,
        max_auto_attempts: int = 3,
        db_path: str | None = None,
        pick_claim_timeout_seconds: float = 30,
        agent_stale_seconds: float = 20,
    ):
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else timeout_from_env()
        self.max_auto_attempts = max_auto_attempts
        self.pick_claim_timeout_seconds = pick_claim_timeout_seconds
        self.agent_stale_seconds = agent_stale_seconds
        self._executions: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._heartbeat_timers: dict[str, asyncio.Task] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._report_ids: dict[str, set[str]] = {}
        self._workflow_registry: dict[tuple[str, str], dict[str, Any]] = {}
        configured_path = db_path if db_path is not None else os.getenv("EXECUTION_DB_PATH", "")
        self._store = SQLiteExecutionStore(configured_path) if configured_path else None
        if self._store:
            for execution in self._store.load():
                execution_id = execution["execution_id"]
                self._executions[execution_id] = execution
                self._locks[execution_id] = asyncio.Lock()
                self._subscribers[execution_id] = set()
                self._report_ids[execution_id] = set()
            for workflow in self._store.load_workflows():
                self._workflow_registry[
                    (workflow["robot_id"], workflow["capability_id"])
                ] = workflow

    async def create(
        self,
        instruction: str,
        provider: str,
        model: str | None,
        planner_steps: list[dict[str, Any]],
        execution_mode: str = "robot_agent",
    ) -> dict[str, Any]:
        if execution_mode not in EXECUTION_MODES:
            raise ExecutionConflictError("不支持的执行模式")
        execution_id = str(uuid4())
        created_at = _iso()
        steps = []
        for index, planner_step in enumerate(planner_steps):
            step = copy.deepcopy(planner_step)
            step.update({
                "step_id": str(uuid4()),
                "index": index,
                "status": "pending",
                "attempts": [],
            })
            steps.append(step)

        execution = {
            "execution_id": execution_id,
            "instruction": instruction,
            "provider": provider,
            "model": model,
            "execution_mode": execution_mode,
            "state": "ready",
            "timeout_seconds": self.timeout_seconds,
            "max_auto_attempts": self.max_auto_attempts,
            "current_step_index": None,
            "active_attempt": None,
            "steps": steps,
            "events": [],
            "version": 0,
            "created_at": created_at,
            "updated_at": created_at,
        }
        self._executions[execution_id] = execution
        self._locks[execution_id] = asyncio.Lock()
        self._subscribers[execution_id] = set()
        self._report_ids[execution_id] = set()
        self._record_event_locked(execution, "execution.created", {
            "state": "ready",
            "step_count": len(steps),
        })
        return self._snapshot_locked(execution)

    async def set_mode(self, execution_id: str, mode: str) -> dict[str, Any]:
        if mode not in EXECUTION_MODES:
            raise ExecutionConflictError("不支持的执行模式")
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            if execution["state"] != "ready":
                raise ExecutionConflictError("只能在开始执行前切换执行模式")
            execution["execution_mode"] = mode
            self._record_event_locked(execution, "execution.mode.changed", {"mode": mode})
            return self._snapshot_locked(execution)

    async def claim_visual_monitor(
        self, camera_id: str, model_requested: str | None = None,
    ) -> dict[str, Any] | None:
        """Claim the oldest active attempt with a registered visual contract."""
        for execution_id in list(self._executions):
            execution = self._executions[execution_id]
            async with self._locks[execution_id]:
                attempt = execution.get("active_attempt")
                mode = execution.get("execution_mode", "robot_agent")
                if (
                    execution["state"] != "running"
                    or mode not in {"visual_monitor", "chain_visual_monitor"}
                    or not attempt
                    or attempt["status"] != "waiting"
                ):
                    continue
                if mode == "chain_visual_monitor":
                    if not all(supports_visual_contract(step.get("action_id"), step.get("logic")) for step in execution["steps"]):
                        continue
                    monitor = execution.get("chain_visual_monitor")
                    if monitor and monitor.get("camera_id") != camera_id:
                        continue
                    if not monitor:
                        monitor = {
                            "monitor_session_id": str(uuid4()),
                            "camera_id": camera_id,
                            "state": "awaiting_baseline",
                            "monitor_epoch": 1,
                            "latest": None,
                            "baseline_url": None,
                            "claimed_at": _iso(),
                            "model_requested": model_requested,
                        }
                        execution["chain_visual_monitor"] = monitor
                        self._record_event_locked(execution, "chain_visual_monitor.claimed", {
                            "camera_id": camera_id,
                            "monitor_session_id": monitor["monitor_session_id"],
                        })
                    elif model_requested and not monitor.get("model_requested"):
                        monitor["model_requested"] = model_requested
                    public_steps = [{
                        key: copy.deepcopy(step.get(key))
                        for key in ("step_id", "index", "action_id", "action", "logic", "slots", "zh", "en")
                    } for step in execution["steps"]]
                    previous_by_step = {
                        item.get("step_id"): item
                        for item in (monitor.get("latest") or {}).get("step_updates", [])
                    }
                    current_index = execution["current_step_index"]
                    return {
                        "execution_id": execution_id,
                        "attempt_id": monitor["monitor_session_id"],
                        "monitor_scope": "chain",
                        "instruction": execution["instruction"],
                        "steps": public_steps,
                        "confirmed_steps": [
                            {"step_id": step["step_id"], "status": "succeeded"}
                            for step in execution["steps"] if step["status"] == "succeeded"
                        ],
                        "current_step_index": current_index,
                        "unfinished_steps": [{
                            "step_id": step["step_id"],
                            "status": (previous_by_step.get(step["step_id"]) or {}).get("status", "unknown"),
                            "description_zh": (previous_by_step.get(step["step_id"]) or {}).get("description_zh"),
                        } for step in execution["steps"][current_index:]],
                        "previous_status": (monitor.get("latest") or {}).get("status"),
                        "monitor_state": monitor.get("state"),
                        "monitor_epoch": monitor.get("monitor_epoch", 1),
                        "model_requested": monitor.get("model_requested"),
                    }
                step = self._current_step_locked(execution)
                if not supports_visual_contract(step.get("action_id"), step.get("logic")):
                    continue
                contract_meta = visual_contract_metadata(step["action_id"], step["logic"])
                monitor = attempt.get("visual_monitor")
                if monitor and monitor.get("camera_id") != camera_id:
                    continue
                if not monitor:
                    attempt["visual_monitor"] = {
                        "camera_id": camera_id,
                        "state": "awaiting_baseline",
                        "monitor_epoch": 1,
                        "latest": None,
                        "baseline_url": None,
                        "claimed_at": _iso(),
                        "model_requested": model_requested,
                        **contract_meta,
                    }
                    self._record_event_locked(execution, "visual_monitor.claimed", {
                        "step_id": step["step_id"],
                        "attempt_id": attempt["attempt_id"],
                        "camera_id": camera_id,
                    })
                return {
                    "execution_id": execution_id,
                    "step_id": step["step_id"],
                    "attempt_id": attempt["attempt_id"],
                    "action_id": step.get("action_id"),
                    "logic": step.get("logic"),
                    "action": step.get("action"),
                    "zh": step.get("zh"),
                    "slots": copy.deepcopy(step.get("slots", {})),
                    "previous_status": ((attempt.get("visual_monitor") or {}).get("latest") or {}).get("status"),
                    "monitor_state": (attempt.get("visual_monitor") or {}).get("state"),
                    "monitor_epoch": (attempt.get("visual_monitor") or {}).get("monitor_epoch", 1),
                    **contract_meta,
                }
        return None

    async def begin_visual_checkpoint(
        self,
        execution_id: str,
        *,
        attempt_id: str,
        camera_id: str,
        sequence: int,
    ) -> dict[str, Any]:
        """Atomically reserve the one allowed inference slot for an attempt."""
        execution = self._require(execution_id)
        if execution.get("execution_mode") == "chain_visual_monitor":
            return await self._begin_chain_visual_checkpoint(
                execution_id, monitor_session_id=attempt_id, camera_id=camera_id, sequence=sequence,
            )
        async with self._locks[execution_id]:
            attempt = execution.get("active_attempt")
            if (
                execution["state"] != "running"
                or execution.get("execution_mode") != "visual_monitor"
                or not attempt
                or attempt["attempt_id"] != attempt_id
                or attempt["status"] != "waiting"
            ):
                raise ExecutionConflictError("Visual Monitor attempt 已过期")
            monitor = attempt.get("visual_monitor")
            if not monitor or monitor.get("camera_id") != camera_id:
                raise ExecutionConflictError("Visual Monitor camera claim 不匹配")
            state = monitor.get("state")
            if state == "awaiting_confirmation":
                raise ExecutionConflictError("Visual Monitor 正在等待人工确认")
            if state == "inferencing":
                raise ExecutionConflictError("Visual Monitor 当前已有推理请求")
            if state == "awaiting_baseline":
                raise ExecutionConflictError("请先上传 BEFORE baseline")
            monitor["state"] = "inferencing"
            monitor["active_sequence"] = sequence
            self._record_event_locked(execution, "visual_monitor.inference.started", {
                "step_id": self._current_step_locked(execution)["step_id"],
                "attempt_id": attempt_id,
                "camera_id": camera_id,
                "sequence": sequence,
            })
            return self._snapshot_locked(execution)

    async def resume_visual_monitor(
        self,
        execution_id: str,
        *,
        attempt_id: str,
    ) -> dict[str, Any]:
        """Discard a terminal VLM observation and start a fresh observation epoch."""
        execution = self._require(execution_id)
        if execution.get("execution_mode") == "chain_visual_monitor":
            return await self._resume_chain_visual_monitor(execution_id, monitor_session_id=attempt_id)
        async with self._locks[execution_id]:
            attempt = execution.get("active_attempt")
            if (
                execution["state"] != "running"
                or execution.get("execution_mode") != "visual_monitor"
                or not attempt
                or attempt["attempt_id"] != attempt_id
                or attempt["status"] != "waiting"
            ):
                raise ExecutionConflictError("Visual Monitor attempt 已过期")
            monitor = attempt.get("visual_monitor")
            if not monitor or monitor.get("state") != "awaiting_confirmation":
                raise ExecutionConflictError("当前没有等待确认的视觉终态")
            monitor.update({
                "state": "awaiting_baseline",
                "monitor_epoch": int(monitor.get("monitor_epoch", 1)) + 1,
                "latest": None,
                "baseline_url": None,
                "baseline_captured_at": None,
                "active_sequence": None,
            })
            self._record_event_locked(execution, "visual_monitor.resumed", {
                "step_id": self._current_step_locked(execution)["step_id"],
                "attempt_id": attempt_id,
                "monitor_epoch": monitor["monitor_epoch"],
            })
            self._schedule_timeout_locked(
                execution_id,
                attempt_id,
                timeout_seconds=float(os.getenv("VISUAL_MONITOR_ATTEMPT_TIMEOUT_SECONDS", "120")),
            )
            return self._snapshot_locked(execution)

    async def update_visual_monitor(
        self,
        execution_id: str,
        *,
        attempt_id: str,
        camera_id: str,
        patch: dict[str, Any],
        event_type: str,
    ) -> dict[str, Any]:
        execution = self._require(execution_id)
        if execution.get("execution_mode") == "chain_visual_monitor":
            return await self._update_chain_visual_monitor(
                execution_id, monitor_session_id=attempt_id, camera_id=camera_id,
                patch=patch, event_type=event_type,
            )
        async with self._locks[execution_id]:
            attempt = execution.get("active_attempt")
            if (
                execution["state"] != "running"
                or execution.get("execution_mode") != "visual_monitor"
                or not attempt
                or attempt["attempt_id"] != attempt_id
                or attempt["status"] != "waiting"
            ):
                raise ExecutionConflictError("Visual Monitor attempt 已过期")
            monitor = attempt.get("visual_monitor")
            if not monitor or monitor.get("camera_id") != camera_id:
                raise ExecutionConflictError("Visual Monitor camera claim 不匹配")
            if event_type == "visual_monitor.baseline.ready" and monitor.get("state") != "awaiting_baseline":
                raise ExecutionConflictError("当前不接受新的 BEFORE baseline")
            if event_type in {"visual_monitor.observation", "visual_monitor.error"} and monitor.get("state") != "inferencing":
                raise ExecutionConflictError("Visual Monitor 推理结果已过期")
            monitor.update(copy.deepcopy(patch))
            if monitor.get("state") == "awaiting_confirmation":
                self._cancel_timer_locked(execution_id)
            self._record_event_locked(execution, event_type, {
                "step_id": self._current_step_locked(execution)["step_id"],
                "attempt_id": attempt_id,
                "camera_id": camera_id,
                "status": (patch.get("latest") or {}).get("status"),
            })
            return self._snapshot_locked(execution)

    async def complete_visual_success(
        self,
        execution_id: str,
        *,
        attempt_id: str,
        camera_id: str,
        latest: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically persist a successful visual observation and advance."""
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            attempt = execution.get("active_attempt")
            if (
                execution["state"] != "running"
                or execution.get("execution_mode") != "visual_monitor"
                or not attempt
                or attempt["attempt_id"] != attempt_id
                or attempt["status"] != "waiting"
            ):
                raise ExecutionConflictError("Visual Monitor attempt 已过期")
            monitor = attempt.get("visual_monitor")
            if not monitor or monitor.get("camera_id") != camera_id:
                raise ExecutionConflictError("Visual Monitor camera claim 不匹配")
            if monitor.get("state") != "inferencing":
                raise ExecutionConflictError("Visual Monitor 推理结果已过期")
            if latest.get("status") != "succeeded":
                raise ExecutionConflictError("自动推进只接受 succeeded 结果")
            if monitor.get("active_sequence") != latest.get("sequence"):
                raise ExecutionConflictError("Visual Monitor sequence 已过期")

            monitor.update({
                "state": "succeeded",
                "active_sequence": None,
                "latest": copy.deepcopy(latest),
            })
            step = self._current_step_locked(execution)
            self._record_event_locked(execution, "visual_monitor.observation", {
                "step_id": step["step_id"],
                "attempt_id": attempt_id,
                "camera_id": camera_id,
                "status": "succeeded",
                "auto_advance": True,
            })
            self._cancel_timer_locked(execution_id)
            self._cancel_heartbeat_timer_locked(execution_id)
            self._resolve_attempt_locked(
                execution,
                outcome="success",
                source="visual_monitor",
                detail=latest.get("description_zh"),
                cancel_timer=False,
            )
            return self._snapshot_locked(execution)

    async def _begin_chain_visual_checkpoint(
        self, execution_id: str, *, monitor_session_id: str, camera_id: str, sequence: int,
    ) -> dict[str, Any]:
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            monitor = execution.get("chain_visual_monitor") or {}
            if (
                execution["state"] != "running"
                or execution.get("execution_mode") != "chain_visual_monitor"
                or monitor.get("monitor_session_id") != monitor_session_id
                or monitor.get("camera_id") != camera_id
            ):
                raise ExecutionConflictError("整链 Visual Monitor session 已过期")
            if monitor.get("state") == "awaiting_confirmation":
                raise ExecutionConflictError("整链 Visual Monitor 正在等待人工确认")
            if monitor.get("state") == "inferencing":
                raise ExecutionConflictError("整链 Visual Monitor 当前已有推理请求")
            if monitor.get("state") == "awaiting_baseline":
                raise ExecutionConflictError("请先上传整链 BEFORE baseline")
            monitor["state"] = "inferencing"
            monitor["active_sequence"] = sequence
            self._record_event_locked(execution, "chain_visual_monitor.inference.started", {
                "camera_id": camera_id, "sequence": sequence,
                "monitor_session_id": monitor_session_id,
            })
            return self._snapshot_locked(execution)

    async def _update_chain_visual_monitor(
        self, execution_id: str, *, monitor_session_id: str, camera_id: str,
        patch: dict[str, Any], event_type: str,
    ) -> dict[str, Any]:
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            monitor = execution.get("chain_visual_monitor") or {}
            if (
                execution["state"] != "running"
                or execution.get("execution_mode") != "chain_visual_monitor"
                or monitor.get("monitor_session_id") != monitor_session_id
                or monitor.get("camera_id") != camera_id
            ):
                raise ExecutionConflictError("整链 Visual Monitor session 已过期")
            if event_type.endswith("baseline.ready") and monitor.get("state") != "awaiting_baseline":
                raise ExecutionConflictError("当前不接受新的整链 BEFORE baseline")
            if event_type in {"visual_monitor.observation", "visual_monitor.error"} and monitor.get("state") != "inferencing":
                raise ExecutionConflictError("整链 Visual Monitor 推理结果已过期")
            monitor.update(copy.deepcopy(patch))
            self._record_event_locked(execution, event_type.replace("visual_monitor", "chain_visual_monitor", 1), {
                "camera_id": camera_id,
                "status": (patch.get("latest") or {}).get("status"),
            })
            return self._snapshot_locked(execution)

    async def _resume_chain_visual_monitor(
        self, execution_id: str, *, monitor_session_id: str,
    ) -> dict[str, Any]:
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            monitor = execution.get("chain_visual_monitor") or {}
            if (
                execution["state"] != "running"
                or monitor.get("monitor_session_id") != monitor_session_id
                or monitor.get("state") != "awaiting_confirmation"
            ):
                raise ExecutionConflictError("当前没有等待确认的整链视觉终态")
            monitor.update({"state": "observing", "active_sequence": None})
            self._record_event_locked(execution, "chain_visual_monitor.resumed", {
                "monitor_session_id": monitor_session_id,
            })
            return self._snapshot_locked(execution)

    async def apply_chain_visual_result(
        self, execution_id: str, *, attempt_id: str, camera_id: str, latest: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge one chain observation without allowing skips, regressions or stale writes."""
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            monitor = execution.get("chain_visual_monitor") or {}
            if (
                execution["state"] != "running"
                or execution.get("execution_mode") != "chain_visual_monitor"
                or monitor.get("monitor_session_id") != attempt_id
                or monitor.get("camera_id") != camera_id
                or monitor.get("state") != "inferencing"
                or monitor.get("active_sequence") != latest.get("sequence")
            ):
                raise ExecutionConflictError("整链 Visual Monitor 结果已过期")
            updates = latest.get("step_updates") or []
            current = execution["current_step_index"]
            if current is None:
                raise ExecutionConflictError("会话当前没有步骤")
            expected_ids = [step["step_id"] for step in execution["steps"][current:]]
            if [item.get("step_id") for item in updates] != expected_ids:
                raise ExecutionConflictError("整链结果与当前规划不匹配")
            for index in range(current):
                if execution["steps"][index]["status"] != "succeeded":
                    raise ExecutionConflictError("整链步骤账本不连续")

            monitor.update({"latest": copy.deepcopy(latest), "active_sequence": None})
            self._record_event_locked(execution, "chain_visual_monitor.observation", {
                "camera_id": camera_id, "status": latest.get("status"),
                "sequence": latest.get("sequence"),
            })
            while execution["state"] == "running" and execution["current_step_index"] is not None:
                index = execution["current_step_index"]
                update = updates[index - current]
                if update["status"] != "succeeded":
                    break
                attempt = execution.get("active_attempt")
                if not attempt or attempt.get("status") != "waiting":
                    raise ExecutionConflictError("当前步骤没有可完成的 attempt")
                attempt["chain_visual_observation"] = copy.deepcopy(update)
                self._cancel_timer_locked(execution_id)
                self._resolve_attempt_locked(
                    execution, outcome="success", source="visual_monitor",
                    detail=update.get("description_zh"), cancel_timer=False,
                )
            if execution["state"] == "completed":
                monitor["state"] = "succeeded"
            else:
                active_update = updates[execution["current_step_index"] - current]
                monitor["state"] = "awaiting_confirmation" if active_update["status"] == "failed" else "observing"
                if monitor["state"] == "awaiting_confirmation":
                    self._cancel_timer_locked(execution_id)
            self._record_event_locked(execution, "chain_visual_monitor.merged", {
                "camera_id": camera_id,
                "state": monitor["state"],
                "current_step_index": execution.get("current_step_index"),
                "sequence": latest.get("sequence"),
            })
            return self._snapshot_locked(execution)

    async def get(self, execution_id: str) -> dict[str, Any]:
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            return self._snapshot_locked(execution)

    async def resume_timers(self) -> None:
        """Re-arm deadlines after restoring snapshots from SQLite."""
        for execution_id in list(self._executions):
            execution = self._executions[execution_id]
            async with self._locks[execution_id]:
                if execution["state"] != "running" or not execution.get("active_attempt"):
                    continue
                attempt = execution["active_attempt"]
                workflow = attempt.get("workflow")
                if workflow and workflow.get("state") not in {
                    "succeeded", "needs_operator",
                }:
                    self._schedule_heartbeat_timeout_locked(
                        execution_id,
                        attempt["attempt_id"],
                        workflow["run_id"],
                    )
                    continue
                if execution.get("execution_mode") == "chain_visual_monitor":
                    monitor = execution.get("chain_visual_monitor") or {}
                    if monitor.get("state") == "awaiting_confirmation":
                        continue
                    if monitor.get("state") == "inferencing":
                        monitor.update({"state": "error", "active_sequence": None})
                        self._record_event_locked(execution, "chain_visual_monitor.error", {
                            "camera_id": monitor.get("camera_id"),
                            "detail": "server restarted during inference",
                        })
                elif execution.get("execution_mode") == "visual_monitor":
                    monitor = attempt.get("visual_monitor") or {}
                    if monitor.get("state") == "awaiting_confirmation":
                        # Human confirmation intentionally has no automatic timeout.
                        continue
                    if monitor.get("state") == "inferencing":
                        # The request task was process-local and cannot survive restart.
                        monitor.update({"state": "error", "active_sequence": None})
                        self._record_event_locked(execution, "visual_monitor.error", {
                            "step_id": self._current_step_locked(execution)["step_id"],
                            "attempt_id": attempt["attempt_id"],
                            "camera_id": monitor.get("camera_id"),
                            "status": None,
                            "detail": "server restarted during inference",
                        })
                step = self._current_step_locked(execution)
                timeout_seconds = (
                    float(os.getenv("VISUAL_MONITOR_ATTEMPT_TIMEOUT_SECONDS", "120"))
                    if execution.get("execution_mode") in {"visual_monitor", "chain_visual_monitor"}
                    else self.pick_claim_timeout_seconds
                    if step.get("action_id") == "A_001"
                    else execution["timeout_seconds"]
                )
                self._schedule_timeout_locked(
                    execution_id,
                    attempt["attempt_id"],
                    timeout_seconds=timeout_seconds,
                )

    async def register_workflow(
        self,
        *,
        robot_id: str,
        agent_instance_id: str,
        workflow: dict[str, Any],
    ) -> dict[str, Any]:
        descriptor = self._validate_workflow_descriptor(workflow)
        registered = {
            **descriptor,
            "robot_id": robot_id,
            "agent_instance_id": agent_instance_id,
            "registered_at": _iso(),
        }
        self._workflow_registry[(robot_id, descriptor["capability_id"])] = registered
        if self._store:
            self._store.save_workflow(registered)
        for execution_id, execution in self._executions.items():
            async with self._locks[execution_id]:
                attempt = execution.get("active_attempt")
                if (
                    execution["state"] == "running"
                    and execution.get("execution_mode", "robot_agent") == "robot_agent"
                    and attempt
                    and self._current_step_locked(execution).get("action_id")
                    == descriptor["capability_id"]
                    and not attempt.get("workflow")
                    and (
                        not attempt.get("workflow_preview")
                        or attempt["workflow_preview"].get("digest")
                        != descriptor["digest"]
                        or attempt["workflow_preview"].get("robot_id")
                        != robot_id
                    )
                ):
                    attempt["workflow_preview"] = self._workflow_preview_locked(
                        descriptor["capability_id"]
                    )
                    self._record_event_locked(
                        execution,
                        "workflow.registered",
                        {
                            "step_id": self._current_step_locked(execution)["step_id"],
                            "workflow_id": descriptor["workflow_id"],
                            "version": descriptor["version"],
                            "robot_id": robot_id,
                        },
                    )
        return copy.deepcopy(registered)

    def registered_workflow(
        self,
        robot_id: str,
        capability_id: str,
    ) -> dict[str, Any] | None:
        workflow = self._workflow_registry.get((robot_id, capability_id))
        return copy.deepcopy(workflow) if workflow else None

    def _workflow_preview_locked(
        self,
        capability_id: str,
    ) -> dict[str, Any] | None:
        candidates = [
            workflow
            for (_, capability), workflow in self._workflow_registry.items()
            if capability == capability_id
        ]
        if not candidates:
            return None
        registered = max(candidates, key=lambda item: item["registered_at"])
        return {
            "workflow_id": registered["workflow_id"],
            "label": registered["label"],
            "version": registered["version"],
            "digest": registered["digest"],
            "robot_id": registered["robot_id"],
            "state": "registered",
            "nodes": [
                {
                    **copy.deepcopy(node),
                    "state": "pending",
                    "started_at": None,
                    "finished_at": None,
                    "exit_code": None,
                    "marker": None,
                    "message": None,
                    "log_tail": None,
                    "service_health": None,
                }
                for node in registered["nodes"]
            ],
        }

    @staticmethod
    def _validate_workflow_descriptor(workflow: dict[str, Any]) -> dict[str, Any]:
        required_text = ("workflow_id", "version", "digest", "capability_id", "label")
        for field in required_text:
            value = workflow.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ExecutionConflictError(f"workflow {field} 必须是非空字符串")
        digest = workflow["digest"]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ExecutionConflictError("workflow digest 必须是小写 SHA-256")
        nodes = workflow.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ExecutionConflictError("workflow nodes 不能为空")
        if len(nodes) > 128:
            raise ExecutionConflictError("workflow nodes 超过 128 个")

        normalized = []
        node_ids = set()
        for raw in nodes:
            if not isinstance(raw, dict):
                raise ExecutionConflictError("workflow node 必须是对象")
            node_id = raw.get("node_id")
            label = raw.get("label")
            node_type = raw.get("type")
            depends_on = raw.get("depends_on", [])
            start_after = raw.get("start_after")
            if not isinstance(node_id, str) or not node_id:
                raise ExecutionConflictError("workflow node_id 必须是非空字符串")
            if node_id in node_ids:
                raise ExecutionConflictError(f"重复 workflow node: {node_id}")
            if not isinstance(label, str) or not label:
                raise ExecutionConflictError(f"workflow node label 缺失: {node_id}")
            if node_type not in WORKFLOW_NODE_TYPES:
                raise ExecutionConflictError(f"不支持的 workflow node type: {node_type}")
            if not isinstance(depends_on, list) or not all(
                isinstance(item, str) and item for item in depends_on
            ):
                raise ExecutionConflictError(f"depends_on 非法: {node_id}")
            if start_after is not None and (
                not isinstance(start_after, dict)
                or not isinstance(start_after.get("node_id"), str)
                or not start_after["node_id"]
                or not isinstance(start_after.get("marker"), str)
                or not start_after["marker"]
            ):
                raise ExecutionConflictError(f"start_after 非法: {node_id}")
            node_ids.add(node_id)
            normalized.append({
                "node_id": node_id,
                "label": label,
                "type": node_type,
                "depends_on": list(depends_on),
                "start_after": (
                    {
                        "node_id": start_after["node_id"],
                        "marker": start_after["marker"],
                    }
                    if start_after
                    else None
                ),
                "host": str(raw.get("host", "arm")),
                "service": node_type == "service",
                "description": str(raw.get("description", "")),
            })

        for node in normalized:
            unknown = set(node["depends_on"]) - node_ids
            if unknown:
                raise ExecutionConflictError(
                    f"workflow node {node['node_id']} 依赖未知节点: {sorted(unknown)}"
                )
            if node["node_id"] in node["depends_on"]:
                raise ExecutionConflictError(f"workflow node 不能依赖自身: {node['node_id']}")
            if node["start_after"]:
                source_id = node["start_after"]["node_id"]
                if source_id not in node_ids:
                    raise ExecutionConflictError(
                        f"workflow node {node['node_id']} start_after "
                        f"未知节点: {source_id}"
                    )
                if source_id == node["node_id"]:
                    raise ExecutionConflictError(
                        f"workflow node 不能 start_after 自身: {node['node_id']}"
                    )

        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {node["node_id"]: node for node in normalized}

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ExecutionConflictError("workflow DAG 存在循环依赖")
            if node_id in visited:
                return
            visiting.add(node_id)
            node = by_id[node_id]
            dependencies = list(node["depends_on"])
            if node["start_after"]:
                dependencies.append(node["start_after"]["node_id"])
            for dependency in dependencies:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in by_id:
            visit(node_id)

        return {
            "workflow_id": workflow["workflow_id"],
            "version": workflow["version"],
            "digest": digest,
            "capability_id": workflow["capability_id"],
            "label": workflow["label"],
            "nodes": normalized,
        }

    async def claim_pick(
        self,
        robot_id: str,
        agent_instance_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Claim the oldest active A_001 attempt for an outbound-only robot agent."""
        agent_instance_id = agent_instance_id or robot_id
        for execution_id in list(self._executions):
            execution = self._executions[execution_id]
            async with self._locks[execution_id]:
                if execution["state"] != "running" or not execution.get("active_attempt"):
                    continue
                if execution.get("execution_mode", "robot_agent") != "robot_agent":
                    continue
                step = self._current_step_locked(execution)
                attempt = execution["active_attempt"]
                if step.get("action_id") != "A_001" or attempt["status"] != "waiting":
                    continue
                registered = self._workflow_registry.get((robot_id, step["action_id"]))
                if not registered:
                    continue
                if registered["agent_instance_id"] != agent_instance_id:
                    continue
                workflow = attempt.get("workflow")
                if workflow and workflow.get("robot_id") != robot_id:
                    continue
                if workflow and workflow.get("agent_instance_id", robot_id) != agent_instance_id:
                    workflow["state"] = "failed"
                    self._block_pending_nodes_locked(workflow)
                    self._record_event_locked(execution, "workflow.agent.replaced", {
                        "step_id": step["step_id"],
                        "attempt_id": attempt["attempt_id"],
                        "run_id": workflow["run_id"],
                        "detail": "agent process restarted; automatic workflow resume is disabled",
                    })
                    self._resolve_attempt_locked(
                        execution,
                        outcome="failure",
                        source="server",
                        detail="agent process restarted; manual retry required",
                        cancel_timer=True,
                    )
                    return None
                if not workflow:
                    now = _iso()
                    run_id = str(uuid4())
                    workflow = {
                        "run_id": run_id,
                        "workflow_id": registered["workflow_id"],
                        "label": registered["label"],
                        "version": registered["version"],
                        "digest": registered["digest"],
                        "capability_id": registered["capability_id"],
                        "robot_id": robot_id,
                        "agent_instance_id": agent_instance_id,
                        "state": "running",
                        "claimed_at": now,
                        "heartbeat_at": now,
                        "last_sequence": 0,
                        "nodes": [
                            {
                                **copy.deepcopy(node),
                                "state": "pending",
                                "started_at": None,
                                "finished_at": None,
                                "exit_code": None,
                                "marker": None,
                                "message": None,
                                "log_tail": None,
                                "service_health": None,
                            }
                            for node in registered["nodes"]
                        ],
                    }
                    attempt["workflow"] = workflow
                    attempt["deadline_at"] = None
                    self._cancel_timer_locked(execution_id)
                    self._record_event_locked(execution, "workflow.claimed", {
                        "step_id": step["step_id"],
                        "attempt_id": attempt["attempt_id"],
                        "run_id": run_id,
                        "robot_id": robot_id,
                    })
                self._schedule_heartbeat_timeout_locked(
                    execution_id,
                    attempt["attempt_id"],
                    workflow["run_id"],
                )
                return {
                    "execution_id": execution_id,
                    "step_id": step["step_id"],
                    "attempt_id": attempt["attempt_id"],
                    "run_id": workflow["run_id"],
                    "workflow_id": workflow["workflow_id"],
                    "workflow_version": workflow["version"],
                    "workflow_digest": workflow["digest"],
                    "last_sequence": workflow["last_sequence"],
                    "action_id": step.get("action_id"),
                    "slots": copy.deepcopy(step.get("slots", {})),
                    "nodes": copy.deepcopy(registered["nodes"]),
                }
        return None

    async def heartbeat(
        self,
        execution_id: str,
        *,
        attempt_id: str,
        run_id: str,
        robot_id: str,
    ) -> dict[str, Any]:
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            attempt, workflow = self._require_workflow_locked(
                execution, attempt_id, run_id, robot_id
            )
            workflow["heartbeat_at"] = _iso()
            self._schedule_heartbeat_timeout_locked(execution_id, attempt_id, run_id)
            self._persist_locked(execution)
            return {"accepted": True, "state": workflow["state"]}

    async def workflow_event(
        self,
        execution_id: str,
        *,
        event_id: str,
        attempt_id: str,
        run_id: str,
        robot_id: str,
        sequence: int,
        node_id: str,
        state: str,
        exit_code: int | None = None,
        marker: str | None = None,
        message: str | None = None,
        log_tail: str | None = None,
        service_health: str | None = None,
    ) -> dict[str, Any]:
        scope = f"workflow:{execution_id}:{attempt_id}:{run_id}"
        if self._store and self._store.seen(scope, event_id):
            return {"duplicate": True, "execution": await self.get(execution_id)}
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            attempt, workflow = self._require_workflow_locked(
                execution, attempt_id, run_id, robot_id
            )
            seen_ids = workflow.setdefault("event_ids", [])
            if event_id in seen_ids:
                return {"duplicate": True, "execution": self._snapshot_locked(execution)}
            if sequence <= int(workflow.get("last_sequence", 0)):
                raise ExecutionConflictError("workflow event sequence 已过期")
            node = next(
                (item for item in workflow["nodes"] if item["node_id"] == node_id),
                None,
            )
            if node is None:
                raise ExecutionConflictError(f"未知 workflow node: {node_id}")
            allowed_states = {
                "pending", "starting", "running", "waiting_input",
                "passed", "failed", "blocked", "cancelled",
            }
            if state not in allowed_states:
                raise ExecutionConflictError(f"不支持的 workflow state: {state}")

            now = _iso()
            if state in {"starting", "running", "waiting_input"} and not node["started_at"]:
                node["started_at"] = now
            if state in {"passed", "failed", "blocked", "cancelled"}:
                node["finished_at"] = now
            node.update({
                "state": state,
                "exit_code": exit_code,
                "marker": marker,
                "message": message,
                "log_tail": log_tail,
                "service_health": service_health,
            })
            workflow["last_sequence"] = sequence
            workflow["heartbeat_at"] = now
            seen_ids.append(event_id)
            if len(seen_ids) > 256:
                del seen_ids[:-256]
            if state == "waiting_input":
                workflow["state"] = "waiting_input"
            elif workflow["state"] == "waiting_input":
                workflow["state"] = "running"

            self._record_event_locked(execution, "workflow.node.updated", {
                "step_id": self._current_step_locked(execution)["step_id"],
                "attempt_id": attempt_id,
                "run_id": run_id,
                "node_id": node_id,
                "state": state,
                "exit_code": exit_code,
                "marker": marker,
                "detail": message,
            })
            if self._store:
                self._store.remember(scope, event_id)

            if state == "failed":
                workflow["state"] = "failed"
                self._block_pending_nodes_locked(workflow)
            self._schedule_heartbeat_timeout_locked(execution_id, attempt_id, run_id)
            return {"duplicate": False, "execution": self._snapshot_locked(execution)}

    async def complete_workflow(
        self,
        execution_id: str,
        *,
        completion_id: str,
        attempt_id: str,
        run_id: str,
        robot_id: str,
        outcome: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        scope = f"workflow-complete:{execution_id}:{attempt_id}:{run_id}"
        if self._store and self._store.seen(scope, completion_id):
            return {"duplicate": True, "execution": await self.get(execution_id)}
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            historical_workflow = next(
                (
                    attempt.get("workflow")
                    for step in execution["steps"]
                    for attempt in step["attempts"]
                    if attempt["attempt_id"] == attempt_id
                    and attempt.get("workflow", {}).get("run_id") == run_id
                    and attempt.get("workflow", {}).get("robot_id") == robot_id
                ),
                None,
            )
            if (
                historical_workflow
                and completion_id
                in historical_workflow.get("completion_ids", [])
            ):
                return {
                    "duplicate": True,
                    "execution": self._snapshot_locked(execution),
                }
            attempt, workflow = self._require_workflow_locked(
                execution, attempt_id, run_id, robot_id
            )
            seen_ids = workflow.setdefault("completion_ids", [])
            if completion_id in seen_ids:
                return {"duplicate": True, "execution": self._snapshot_locked(execution)}
            if outcome not in {"succeeded", "failed", "needs_operator"}:
                raise ExecutionConflictError(f"不支持的 workflow outcome: {outcome}")
            if outcome == "succeeded":
                incomplete = [
                    node["node_id"] for node in workflow["nodes"]
                    if node["state"] != "passed"
                ]
                if incomplete:
                    raise ExecutionConflictError(
                        f"workflow 仍有未通过节点: {incomplete}"
                    )
            else:
                self._block_pending_nodes_locked(workflow)

            seen_ids.append(completion_id)
            workflow["state"] = outcome
            workflow["completed_at"] = _iso()
            workflow["message"] = message
            self._cancel_heartbeat_timer_locked(execution_id)
            self._record_event_locked(execution, "workflow.completed", {
                "step_id": self._current_step_locked(execution)["step_id"],
                "attempt_id": attempt_id,
                "run_id": run_id,
                "outcome": outcome,
                "detail": message,
            })
            if self._store:
                self._store.remember(scope, completion_id)
            self._resolve_attempt_locked(
                execution,
                outcome="success" if outcome == "succeeded" else "failure",
                source="robot",
                detail=message or f"robot workflow {outcome}",
                cancel_timer=False,
            )
            return {"duplicate": False, "execution": self._snapshot_locked(execution)}

    async def start(self, execution_id: str) -> dict[str, Any]:
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            if execution["state"] != "ready":
                raise ExecutionConflictError("只有 ready 会话可以开始执行")
            if not execution["steps"]:
                raise ExecutionConflictError("执行计划没有步骤")
            execution["state"] = "running"
            execution["current_step_index"] = 0
            self._record_event_locked(execution, "execution.started", {"state": "running"})
            self._activate_step_locked(execution, manual_retry=False)
            return self._snapshot_locked(execution)

    async def report(
        self,
        execution_id: str,
        *,
        report_id: str,
        step_id: str,
        attempt_id: str,
        outcome: str,
        source: str,
        detail: str | None = None,
    ) -> dict[str, Any]:
        execution = self._require(execution_id)
        scope = f"report:{execution_id}"
        if self._store and self._store.seen(scope, report_id):
            return {"duplicate": True, "execution": await self.get(execution_id)}
        async with self._locks[execution_id]:
            if report_id in self._report_ids[execution_id]:
                return {"duplicate": True, "execution": self._snapshot_locked(execution)}
            if execution["state"] != "running":
                self._record_rejection_locked(execution, report_id, step_id, attempt_id, source,
                                              "会话当前不接受 monitor 回报")
                raise ExecutionConflictError("会话当前不接受 monitor 回报")
            step = self._current_step_locked(execution)
            attempt = execution["active_attempt"]
            if not attempt or attempt["status"] != "waiting":
                self._record_rejection_locked(execution, report_id, step_id, attempt_id, source,
                                              "当前没有等待回报的 attempt")
                raise ExecutionConflictError("当前没有等待回报的 attempt")
            if step["step_id"] != step_id or attempt["attempt_id"] != attempt_id:
                self._record_rejection_locked(execution, report_id, step_id, attempt_id, source,
                                              "回报对应的 step/attempt 已过期")
                raise ExecutionConflictError("回报对应的 step/attempt 已过期")

            self._report_ids[execution_id].add(report_id)
            if self._store:
                self._store.remember(scope, report_id)
            self._cancel_timer_locked(execution_id)
            self._cancel_heartbeat_timer_locked(execution_id)
            self._resolve_attempt_locked(
                execution,
                outcome=outcome,
                source=source,
                detail=detail,
                cancel_timer=False,
            )
            return {"duplicate": False, "execution": self._snapshot_locked(execution)}

    async def retry(self, execution_id: str) -> dict[str, Any]:
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            if execution["state"] != "paused":
                raise ExecutionConflictError("只有 paused 会话可以人工重试")
            step = self._current_step_locked(execution)
            if step["status"] != "blocked":
                raise ExecutionConflictError("当前步骤没有处于 blocked 状态")
            execution["state"] = "running"
            if execution.get("execution_mode") == "chain_visual_monitor":
                monitor = execution.get("chain_visual_monitor") or {}
                monitor.update({
                    "state": "awaiting_baseline",
                    "monitor_epoch": int(monitor.get("monitor_epoch", 1)) + 1,
                    "active_sequence": None,
                    "baseline_url": None,
                    "baseline_captured_at": None,
                })
            self._record_event_locked(execution, "execution.resumed", {
                "step_id": step["step_id"],
                "reason": "manual_retry",
            })
            self._activate_step_locked(execution, manual_retry=True)
            return self._snapshot_locked(execution)

    async def terminate(self, execution_id: str) -> dict[str, Any]:
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            if execution["state"] in {"completed", "terminated"}:
                raise ExecutionConflictError("会话已经结束")
            self._cancel_timer_locked(execution_id)
            self._cancel_heartbeat_timer_locked(execution_id)
            attempt = execution.get("active_attempt")
            if attempt and attempt["status"] == "waiting":
                attempt["status"] = "cancelled"
                attempt["resolved_at"] = _iso()
                attempt["source"] = "human"
            if execution["current_step_index"] is not None:
                step = self._current_step_locked(execution)
                if step["status"] == "active":
                    step["status"] = "blocked"
            execution["active_attempt"] = None
            execution["state"] = "terminated"
            self._record_event_locked(execution, "execution.terminated", {"state": "terminated"})
            return self._snapshot_locked(execution)

    async def subscribe(self, execution_id: str) -> asyncio.Queue:
        execution = self._require(execution_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._locks[execution_id]:
            self._subscribers[execution_id].add(queue)
            queue.put_nowait({
                "event_id": str(uuid4()),
                "type": "snapshot",
                "occurred_at": _iso(),
                "data": {},
                "snapshot": self._snapshot_locked(execution),
            })
        return queue

    def unsubscribe(self, execution_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(execution_id)
        if subscribers is not None:
            subscribers.discard(queue)

    async def close(self) -> None:
        timers = list(self._timers.values()) + list(self._heartbeat_timers.values())
        self._timers.clear()
        self._heartbeat_timers.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        if self._store:
            self._store.close()

    def _require(self, execution_id: str) -> dict[str, Any]:
        try:
            return self._executions[execution_id]
        except KeyError as exc:
            raise ExecutionNotFoundError(execution_id) from exc

    def _current_step_locked(self, execution: dict[str, Any]) -> dict[str, Any]:
        index = execution["current_step_index"]
        if index is None or index >= len(execution["steps"]):
            raise ExecutionConflictError("会话当前没有步骤")
        return execution["steps"][index]

    def _require_workflow_locked(
        self,
        execution: dict[str, Any],
        attempt_id: str,
        run_id: str,
        robot_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if execution["state"] != "running":
            raise ExecutionConflictError("execution 当前不接受 Agent 事件")
        attempt = execution.get("active_attempt")
        if not attempt or attempt["attempt_id"] != attempt_id:
            raise ExecutionConflictError("Agent attempt 已过期")
        workflow = attempt.get("workflow")
        if (
            not workflow
            or workflow.get("run_id") != run_id
            or workflow.get("robot_id") != robot_id
        ):
            raise ExecutionConflictError("Agent workflow claim 不匹配")
        return attempt, workflow

    @staticmethod
    def _block_pending_nodes_locked(workflow: dict[str, Any]) -> None:
        now = _iso()
        for node in workflow["nodes"]:
            if node["state"] == "pending":
                node["state"] = "blocked"
                node["finished_at"] = now
                node["message"] = "上游步骤失败"

    def _activate_step_locked(self, execution: dict[str, Any], *, manual_retry: bool) -> None:
        step = self._current_step_locked(execution)
        step["status"] = "active"
        started = _now()
        is_pick = step.get("action_id") == "A_001"
        visual_mode = execution.get("execution_mode") in {"visual_monitor", "chain_visual_monitor"}
        visual_timeout = float(os.getenv("VISUAL_MONITOR_ATTEMPT_TIMEOUT_SECONDS", "120"))
        timeout_seconds = (
            visual_timeout if visual_mode
            else self.pick_claim_timeout_seconds if is_pick
            else execution["timeout_seconds"]
        )
        attempt = {
            "attempt_id": str(uuid4()),
            "attempt_no": len(step["attempts"]) + 1,
            "status": "waiting",
            "manual_retry": manual_retry,
            "started_at": _iso(started),
            "deadline_at": _iso(started + timedelta(seconds=timeout_seconds)),
            "resolved_at": None,
            "source": None,
            "detail": None,
        }
        if is_pick and not visual_mode:
            preview = self._workflow_preview_locked(step["action_id"])
            if preview:
                attempt["workflow_preview"] = preview
        step["attempts"].append(attempt)
        execution["active_attempt"] = attempt
        self._schedule_timeout_locked(
            execution["execution_id"],
            attempt["attempt_id"],
            timeout_seconds=timeout_seconds,
        )
        self._record_event_locked(execution, "step.started", {
            "execution_id": execution["execution_id"],
            "step_id": step["step_id"],
            "attempt_id": attempt["attempt_id"],
            "attempt_no": attempt["attempt_no"],
            "manual_retry": manual_retry,
            "deadline_at": attempt["deadline_at"],
            "action_id": step.get("action_id"),
            "action": step.get("action"),
            "logic": step.get("logic"),
            "slots": copy.deepcopy(step.get("slots", {})),
            "zh": step.get("zh"),
            "en": step.get("en"),
        })

    def _resolve_attempt_locked(
        self,
        execution: dict[str, Any],
        *,
        outcome: str,
        source: str,
        detail: str | None,
        cancel_timer: bool,
    ) -> None:
        if cancel_timer:
            self._cancel_timer_locked(execution["execution_id"])
        step = self._current_step_locked(execution)
        attempt = execution["active_attempt"]
        if not attempt or attempt["status"] != "waiting":
            return

        attempt["status"] = outcome
        attempt["resolved_at"] = _iso()
        attempt["source"] = source
        attempt["detail"] = detail
        execution["active_attempt"] = None
        event_type = {
            "success": "attempt.succeeded",
            "failure": "attempt.failed",
            "timeout": "attempt.timed_out",
        }[outcome]
        self._record_event_locked(execution, event_type, {
            "step_id": step["step_id"],
            "attempt_id": attempt["attempt_id"],
            "attempt_no": attempt["attempt_no"],
            "source": source,
            "detail": detail,
        })

        if outcome == "success":
            step["status"] = "succeeded"
            if execution["current_step_index"] == len(execution["steps"]) - 1:
                execution["state"] = "completed"
                self._record_event_locked(execution, "execution.completed", {"state": "completed"})
                return
            execution["current_step_index"] += 1
            self._activate_step_locked(execution, manual_retry=False)
            return

        allow_auto_retry = step.get("action_id") != "A_001"
        should_auto_retry = (
            allow_auto_retry
            and
            not attempt["manual_retry"]
            and attempt["attempt_no"] < execution["max_auto_attempts"]
        )
        if should_auto_retry:
            self._activate_step_locked(execution, manual_retry=False)
            return

        step["status"] = "blocked"
        execution["state"] = "paused"
        self._record_event_locked(execution, "execution.paused", {
            "step_id": step["step_id"],
            "reason": outcome,
            "attempt_no": attempt["attempt_no"],
        })

    def _schedule_timeout_locked(
        self,
        execution_id: str,
        attempt_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        self._cancel_timer_locked(execution_id)
        self._timers[execution_id] = asyncio.create_task(
            self._timeout_after(
                execution_id,
                attempt_id,
                timeout_seconds=timeout_seconds,
            ),
            name=f"monitor-timeout:{execution_id}:{attempt_id}",
        )

    def _cancel_timer_locked(self, execution_id: str) -> None:
        timer = self._timers.pop(execution_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def _timeout_after(
        self,
        execution_id: str,
        attempt_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        try:
            execution = self._require(execution_id)
            await asyncio.sleep(
                execution["timeout_seconds"] if timeout_seconds is None else timeout_seconds
            )
            async with self._locks[execution_id]:
                current = execution.get("active_attempt")
                if (
                    execution["state"] != "running"
                    or not current
                    or current["attempt_id"] != attempt_id
                    or current["status"] != "waiting"
                ):
                    return
                self._timers.pop(execution_id, None)
                self._resolve_attempt_locked(
                    execution,
                    outcome="timeout",
                    source="server",
                    detail="monitor deadline exceeded",
                    cancel_timer=False,
                )
        except (asyncio.CancelledError, ExecutionNotFoundError):
            return

    def _schedule_heartbeat_timeout_locked(
        self,
        execution_id: str,
        attempt_id: str,
        run_id: str,
    ) -> None:
        self._cancel_heartbeat_timer_locked(execution_id)
        self._heartbeat_timers[execution_id] = asyncio.create_task(
            self._heartbeat_timeout_after(execution_id, attempt_id, run_id),
            name=f"agent-heartbeat:{execution_id}:{attempt_id}",
        )

    def _cancel_heartbeat_timer_locked(self, execution_id: str) -> None:
        timer = self._heartbeat_timers.pop(execution_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def _heartbeat_timeout_after(
        self,
        execution_id: str,
        attempt_id: str,
        run_id: str,
    ) -> None:
        try:
            await asyncio.sleep(self.agent_stale_seconds)
            execution = self._require(execution_id)
            async with self._locks[execution_id]:
                attempt = execution.get("active_attempt")
                workflow = attempt.get("workflow") if attempt else None
                if (
                    execution["state"] != "running"
                    or not attempt
                    or attempt["attempt_id"] != attempt_id
                    or not workflow
                    or workflow["run_id"] != run_id
                    or workflow["state"] in {"succeeded", "needs_operator"}
                ):
                    return
                self._heartbeat_timers.pop(execution_id, None)
                workflow["state"] = "failed"
                active_nodes = [
                    node
                    for node in workflow["nodes"]
                    if node["state"] in {"starting", "running", "waiting_input"}
                ]
                if not active_nodes:
                    active_nodes = [workflow["nodes"][0]]
                for node in active_nodes:
                    node["state"] = "failed"
                    node["finished_at"] = _iso()
                    node["message"] = "robot agent heartbeat expired"
                    node["service_health"] = (
                        "stale" if node.get("service") else node.get("service_health")
                    )
                self._block_pending_nodes_locked(workflow)
                self._record_event_locked(execution, "workflow.agent.stale", {
                    "step_id": self._current_step_locked(execution)["step_id"],
                    "attempt_id": attempt_id,
                    "run_id": run_id,
                    "detail": "robot agent heartbeat expired",
                })
                self._resolve_attempt_locked(
                    execution,
                    outcome="failure",
                    source="server",
                    detail="robot agent heartbeat expired",
                    cancel_timer=False,
                )
        except (asyncio.CancelledError, ExecutionNotFoundError):
            return

    def _record_event_locked(
        self,
        execution: dict[str, Any],
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        occurred_at = _iso()
        event = {
            "event_id": str(uuid4()),
            "type": event_type,
            "occurred_at": occurred_at,
            "data": data,
        }
        execution["events"].append(event)
        execution["version"] += 1
        execution["updated_at"] = occurred_at
        self._persist_locked(execution)
        payload = copy.deepcopy(event)
        payload["snapshot"] = self._snapshot_locked(execution)
        stale = []
        for queue in self._subscribers.get(execution["execution_id"], set()):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                stale.append(queue)
        for queue in stale:
            self._subscribers[execution["execution_id"]].discard(queue)

    def _persist_locked(self, execution: dict[str, Any]) -> None:
        if self._store:
            self._store.save(execution)

    def _record_rejection_locked(
        self,
        execution: dict[str, Any],
        report_id: str,
        step_id: str,
        attempt_id: str,
        source: str,
        reason: str,
    ) -> None:
        self._record_event_locked(execution, "report.rejected", {
            "report_id": report_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "source": source,
            "detail": reason,
        })

    @staticmethod
    def _snapshot_locked(execution: dict[str, Any]) -> dict[str, Any]:
        snapshot = copy.deepcopy(execution)
        succeeded = sum(step["status"] == "succeeded" for step in snapshot["steps"])
        snapshot["progress"] = {
            "succeeded": succeeded,
            "total": len(snapshot["steps"]),
            "ratio": succeeded / len(snapshot["steps"]) if snapshot["steps"] else 0,
        }
        return snapshot
