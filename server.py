# -*- coding: utf-8 -*-
"""指令拆解 Web 服务。

    python server.py                      # 监听 0.0.0.0:8000
    uvicorn server:app --host 0.0.0.0     # 或用 uvicorn 直接起

接口:
    GET  /                         前端页面 (static/index.html)
    GET  /api/providers            可用提供商列表
    POST /api/decompose            仅拆解（兼容接口）
    POST /api/executions           拆解并创建执行会话
    GET  /api/executions/{id}      获取权威执行快照
    GET  /api/executions/{id}/events 订阅 SSE 事件
"""

import asyncio
import hmac
import json
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from decompose import decompose
from execution import (
    ExecutionConflictError,
    ExecutionManager,
    ExecutionNotFoundError,
)
from primitives import load_atomic_catalog, load_expert_catalog
from providers import PROVIDERS

app = FastAPI(title="指令拆解 Instruction Break-Down")
execution_manager = ExecutionManager()

STATIC_DIR = Path(__file__).parent / "static"


class DecomposeRequest(BaseModel):
    instruction: str
    provider: str = "deepseek"
    model: str | None = None


class MonitorReportRequest(BaseModel):
    report_id: str = Field(min_length=1, max_length=128)
    step_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    outcome: Literal["success", "failure"]
    source: Literal["human", "robot"]
    detail: str | None = Field(default=None, max_length=2000)


class AgentClaimRequest(BaseModel):
    robot_id: str = Field(min_length=1, max_length=128)
    agent_instance_id: str | None = Field(default=None, min_length=1, max_length=128)


class WorkflowRegisterRequest(BaseModel):
    robot_id: str = Field(min_length=1, max_length=128)
    agent_instance_id: str = Field(min_length=1, max_length=128)
    workflow: dict


class AgentHeartbeatRequest(BaseModel):
    execution_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    robot_id: str = Field(min_length=1, max_length=128)


class WorkflowEventRequest(AgentHeartbeatRequest):
    event_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(gt=0)
    node_id: str = Field(min_length=1, max_length=128)
    state: Literal[
        "pending", "starting", "running", "waiting_input",
        "passed", "failed", "blocked", "cancelled",
    ]
    exit_code: int | None = None
    marker: str | None = Field(default=None, max_length=256)
    message: str | None = Field(default=None, max_length=4000)
    log_tail: str | None = Field(default=None, max_length=65536)
    service_health: Literal["healthy", "stale", "exited"] | None = None


class WorkflowCompleteRequest(AgentHeartbeatRequest):
    completion_id: str = Field(min_length=1, max_length=128)
    outcome: Literal["succeeded", "failed", "needs_operator"]
    message: str | None = Field(default=None, max_length=4000)


def _require_token(config_name: str, supplied: str | None, prefix: str = "") -> None:
    expected = os.getenv(config_name, "")
    if not expected:
        return
    candidate = supplied or ""
    if prefix and candidate.startswith(prefix):
        candidate = candidate[len(prefix):]
    if not hmac.compare_digest(candidate, expected):
        raise HTTPException(status_code=401, detail="认证失败")


def _require_operator(supplied: str | None) -> None:
    _require_token("OPERATOR_TOKEN", supplied)


def _require_agent(authorization: str | None) -> None:
    _require_token("GRASPARM_AGENT_TOKEN", authorization, "Bearer ")


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ExecutionNotFoundError):
        return HTTPException(status_code=404, detail="执行会话不存在或已失效")
    return HTTPException(status_code=409, detail=str(exc))


@app.get("/api/providers")
def list_providers() -> dict:
    return {"providers": list(PROVIDERS), "default": "deepseek"}


@app.get("/api/operations")
def list_operations() -> dict:
    return {"atomic": load_atomic_catalog(), "expert": load_expert_catalog()}


@app.post("/api/decompose")
def api_decompose(req: DecomposeRequest) -> dict:
    instruction = req.instruction.strip()
    if not instruction:
        return {"status": "error", "reason": "指令不能为空"}
    try:
        result = decompose(instruction, req.provider, req.model)
    except Exception as e:  # key 未配置 / 网络 / LLM 返回格式坏 → 前端统一展示
        return {"status": "error", "reason": str(e)}
    result["instruction"] = instruction
    return result


@app.post("/api/executions")
async def create_execution(req: DecomposeRequest) -> dict:
    instruction = req.instruction.strip()
    if not instruction:
        return {"status": "error", "reason": "指令不能为空"}
    try:
        result = await run_in_threadpool(decompose, instruction, req.provider, req.model)
    except Exception as exc:
        return {"status": "error", "reason": str(exc), "instruction": instruction}
    result["instruction"] = instruction
    if result.get("status") != "ok":
        return result
    execution = await execution_manager.create(
        instruction=instruction,
        provider=req.provider,
        model=req.model,
        planner_steps=result["steps"],
    )
    return {"status": "ok", "execution": execution}


@app.get("/api/executions/{execution_id}")
async def get_execution(execution_id: str) -> dict:
    try:
        return {"execution": await execution_manager.get(execution_id)}
    except (ExecutionNotFoundError, ExecutionConflictError) as exc:
        raise _http_error(exc) from exc


@app.post("/api/executions/{execution_id}/start")
async def start_execution(
    execution_id: str,
    x_operator_token: str | None = Header(default=None),
) -> dict:
    _require_operator(x_operator_token)
    try:
        return {"execution": await execution_manager.start(execution_id)}
    except (ExecutionNotFoundError, ExecutionConflictError) as exc:
        raise _http_error(exc) from exc


@app.post("/api/executions/{execution_id}/reports")
async def report_execution(
    execution_id: str,
    req: MonitorReportRequest,
    x_operator_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    if req.source == "human":
        _require_operator(x_operator_token)
    else:
        _require_agent(authorization)
    try:
        return await execution_manager.report(
            execution_id,
            report_id=req.report_id,
            step_id=req.step_id,
            attempt_id=req.attempt_id,
            outcome=req.outcome,
            source=req.source,
            detail=req.detail,
        )
    except (ExecutionNotFoundError, ExecutionConflictError) as exc:
        raise _http_error(exc) from exc


@app.post("/api/executions/{execution_id}/retry")
async def retry_execution(
    execution_id: str,
    x_operator_token: str | None = Header(default=None),
) -> dict:
    _require_operator(x_operator_token)
    try:
        return {"execution": await execution_manager.retry(execution_id)}
    except (ExecutionNotFoundError, ExecutionConflictError) as exc:
        raise _http_error(exc) from exc


@app.post("/api/executions/{execution_id}/terminate")
async def terminate_execution(
    execution_id: str,
    x_operator_token: str | None = Header(default=None),
) -> dict:
    _require_operator(x_operator_token)
    try:
        return {"execution": await execution_manager.terminate(execution_id)}
    except (ExecutionNotFoundError, ExecutionConflictError) as exc:
        raise _http_error(exc) from exc


@app.post("/api/agent/claim")
async def claim_agent_work(
    req: AgentClaimRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_agent(authorization)
    return {
        "assignment": await execution_manager.claim_pick(
            req.robot_id,
            req.agent_instance_id,
        )
    }


@app.post("/api/agent/workflows/register")
async def register_agent_workflow(
    req: WorkflowRegisterRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_agent(authorization)
    try:
        workflow = await execution_manager.register_workflow(
            robot_id=req.robot_id,
            agent_instance_id=req.agent_instance_id,
            workflow=req.workflow,
        )
        return {"accepted": True, "workflow": workflow}
    except ExecutionConflictError as exc:
        raise _http_error(exc) from exc


@app.post("/api/agent/heartbeat")
async def heartbeat_agent(
    req: AgentHeartbeatRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_agent(authorization)
    try:
        return await execution_manager.heartbeat(
            req.execution_id,
            attempt_id=req.attempt_id,
            run_id=req.run_id,
            robot_id=req.robot_id,
        )
    except (ExecutionNotFoundError, ExecutionConflictError) as exc:
        raise _http_error(exc) from exc


@app.post("/api/agent/events")
async def report_workflow_event(
    req: WorkflowEventRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_agent(authorization)
    try:
        return await execution_manager.workflow_event(
            req.execution_id,
            event_id=req.event_id,
            attempt_id=req.attempt_id,
            run_id=req.run_id,
            robot_id=req.robot_id,
            sequence=req.sequence,
            node_id=req.node_id,
            state=req.state,
            exit_code=req.exit_code,
            marker=req.marker,
            message=req.message,
            log_tail=req.log_tail,
            service_health=req.service_health,
        )
    except (ExecutionNotFoundError, ExecutionConflictError) as exc:
        raise _http_error(exc) from exc


@app.post("/api/agent/complete")
async def complete_agent_workflow(
    req: WorkflowCompleteRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_agent(authorization)
    try:
        return await execution_manager.complete_workflow(
            req.execution_id,
            completion_id=req.completion_id,
            attempt_id=req.attempt_id,
            run_id=req.run_id,
            robot_id=req.robot_id,
            outcome=req.outcome,
            message=req.message,
        )
    except (ExecutionNotFoundError, ExecutionConflictError) as exc:
        raise _http_error(exc) from exc


@app.get("/api/executions/{execution_id}/events")
async def execution_events(execution_id: str) -> StreamingResponse:
    try:
        queue = await execution_manager.subscribe(execution_id)
    except ExecutionNotFoundError as exc:
        raise _http_error(exc) from exc

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event['event_id']}\nevent: execution\ndata: {payload}\n\n"
        finally:
            execution_manager.unsubscribe(execution_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.on_event("shutdown")
async def close_execution_manager() -> None:
    await execution_manager.close()


@app.on_event("startup")
async def resume_execution_timers() -> None:
    await execution_manager.resume_timers()


# 放在 API 路由之后,兜底提供前端页面
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
