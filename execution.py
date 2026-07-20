# -*- coding: utf-8 -*-
"""后端驱动的原子操作执行状态机。

执行数据当前仅保存在单个 Python 进程内。网页和未来的机器人 monitor
都只能通过 report() 上报结果；步骤推进、重试和超时均由本模块决定。
"""

from __future__ import annotations

import asyncio
import copy
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4


class ExecutionNotFoundError(KeyError):
    """执行会话不存在。"""


class ExecutionConflictError(RuntimeError):
    """请求与执行会话的当前状态冲突。"""


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
    """管理内存执行会话、服务端计时器和 SSE 订阅。"""

    def __init__(self, timeout_seconds: float | None = None, max_auto_attempts: int = 3):
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else timeout_from_env()
        self.max_auto_attempts = max_auto_attempts
        self._executions: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._report_ids: dict[str, set[str]] = {}

    async def create(
        self,
        instruction: str,
        provider: str,
        model: str | None,
        planner_steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
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

    async def get(self, execution_id: str) -> dict[str, Any]:
        execution = self._require(execution_id)
        async with self._locks[execution_id]:
            return self._snapshot_locked(execution)

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
            self._cancel_timer_locked(execution_id)
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
        timers = list(self._timers.values())
        self._timers.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)

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

    def _activate_step_locked(self, execution: dict[str, Any], *, manual_retry: bool) -> None:
        step = self._current_step_locked(execution)
        step["status"] = "active"
        started = _now()
        attempt = {
            "attempt_id": str(uuid4()),
            "attempt_no": len(step["attempts"]) + 1,
            "status": "waiting",
            "manual_retry": manual_retry,
            "started_at": _iso(started),
            "deadline_at": _iso(started + timedelta(seconds=execution["timeout_seconds"])),
            "resolved_at": None,
            "source": None,
            "detail": None,
        }
        step["attempts"].append(attempt)
        execution["active_attempt"] = attempt
        self._schedule_timeout_locked(execution["execution_id"], attempt["attempt_id"])
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

        should_auto_retry = (
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

    def _schedule_timeout_locked(self, execution_id: str, attempt_id: str) -> None:
        self._cancel_timer_locked(execution_id)
        self._timers[execution_id] = asyncio.create_task(
            self._timeout_after(execution_id, attempt_id),
            name=f"monitor-timeout:{execution_id}:{attempt_id}",
        )

    def _cancel_timer_locked(self, execution_id: str) -> None:
        timer = self._timers.pop(execution_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def _timeout_after(self, execution_id: str, attempt_id: str) -> None:
        try:
            execution = self._require(execution_id)
            await asyncio.sleep(execution["timeout_seconds"])
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
