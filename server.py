# -*- coding: utf-8 -*-
"""Planner Monitor Web 服务。

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

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
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
from realtime_monitor import VisualMonitorService, utc_iso

app = FastAPI(title="Planner Monitor")
execution_manager = ExecutionManager()
async def _update_visual_execution(**kwargs):
    return await execution_manager.update_visual_monitor(**kwargs)


visual_monitor = VisualMonitorService(_update_visual_execution)
visual_baselines: dict[tuple[str, str, str], Path] = {}

STATIC_DIR = Path(__file__).parent / "static"


class DecomposeRequest(BaseModel):
    instruction: str
    provider: str = "deepseek"
    model: str | None = None


class ExecutionModeRequest(BaseModel):
    mode: Literal["robot_agent", "visual_monitor"]


class VisualClaimRequest(BaseModel):
    camera_id: str = Field(min_length=1, max_length=128)


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


def _require_visual_monitor(authorization: str | None) -> None:
    _require_token("VISUAL_MONITOR_TOKEN", authorization, "Bearer ")


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


@app.post("/api/executions/{execution_id}/mode")
async def set_execution_mode(
    execution_id: str,
    req: ExecutionModeRequest,
    x_operator_token: str | None = Header(default=None),
) -> dict:
    _require_operator(x_operator_token)
    try:
        return {"execution": await execution_manager.set_mode(execution_id, req.mode)}
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


@app.post("/api/visual-monitor/claim")
async def claim_visual_monitor(
    req: VisualClaimRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_visual_monitor(authorization)
    return {"assignment": await execution_manager.claim_visual_monitor(req.camera_id)}


@app.post("/api/visual-monitor/baseline")
async def upload_visual_baseline(
    execution_id: str = Form(...),
    attempt_id: str = Form(...),
    camera_id: str = Form(...),
    captured_at: str = Form(...),
    image: UploadFile = File(...),
    authorization: str | None = Header(default=None),
) -> dict:
    _require_visual_monitor(authorization)
    try:
        path, size, write_ms = await visual_monitor.save_upload(image, ".jpg")
        visual_baselines[(execution_id, attempt_id, camera_id)] = path
        snapshot = await execution_manager.update_visual_monitor(
            execution_id,
            attempt_id=attempt_id,
            camera_id=camera_id,
            patch={
                "state": "ready",
                "baseline_url": f"/api/visual-monitor/media/{path.name}",
                "baseline_captured_at": captured_at,
            },
            event_type="visual_monitor.baseline.ready",
        )
        visual_monitor.cleanup_expired()
        return {"accepted": True, "bytes": size, "server_write_ms": round(write_ms, 1), "received_at": utc_iso(), "execution": snapshot}
    except (ValueError, ExecutionNotFoundError, ExecutionConflictError) as exc:
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        raise _http_error(exc) from exc


@app.post("/api/visual-monitor/checkpoints", status_code=202)
async def upload_visual_checkpoint(
    execution_id: str = Form(...),
    attempt_id: str = Form(...),
    camera_id: str = Form(...),
    sequence: int = Form(..., ge=1),
    window_started_at: str = Form(...),
    window_ended_at: str = Form(...),
    capture_ms: float = Form(..., ge=0),
    encode_ms: float = Form(..., ge=0),
    video: UploadFile = File(...),
    authorization: str | None = Header(default=None),
) -> dict:
    _require_visual_monitor(authorization)
    assignment = await execution_manager.claim_visual_monitor(camera_id)
    if not assignment or assignment["execution_id"] != execution_id or assignment["attempt_id"] != attempt_id:
        raise HTTPException(status_code=409, detail="Visual Monitor assignment 已过期")
    baseline = visual_baselines.get((execution_id, attempt_id, camera_id))
    if not baseline or not baseline.is_file():
        raise HTTPException(status_code=409, detail="请先上传 BEFORE baseline")
    try:
        path, size, write_ms = await visual_monitor.save_upload(video, ".mp4")
        visual_monitor.cleanup_expired()
        await visual_monitor.submit(
            assignment=assignment,
            camera_id=camera_id,
            sequence=sequence,
            baseline_path=baseline,
            video_path=path,
            client_timings={
                "capture": capture_ms,
                "encode": encode_ms,
                "server_write": round(write_ms, 1),
            },
        )
        return {"accepted": True, "sequence": sequence, "bytes": size, "server_write_ms": round(write_ms, 1), "in_flight": visual_monitor.in_flight, "received_at": utc_iso()}
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@app.post("/api/visual-monitor/upload-probe")
async def upload_visual_probe(
    camera_id: str = Form(...),
    capture_ms: float = Form(..., ge=0),
    encode_ms: float = Form(..., ge=0),
    video: UploadFile = File(...),
    authorization: str | None = Header(default=None),
) -> dict:
    """Measure camera capture/encode/upload without creating a Bailian request."""
    _require_visual_monitor(authorization)
    try:
        path, size, write_ms = await visual_monitor.save_upload(video, ".mp4")
        visual_monitor.cleanup_expired()
        return {
            "accepted": True,
            "camera_id": camera_id,
            "bytes": size,
            "capture_ms": capture_ms,
            "encode_ms": encode_ms,
            "server_write_ms": round(write_ms, 1),
            "media_url": f"/api/visual-monitor/media/{path.name}",
            "received_at": utc_iso(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc


@app.get("/api/visual-monitor/media/{media_name}")
async def get_visual_media(media_name: str) -> FileResponse:
    if Path(media_name).name != media_name:
        raise HTTPException(status_code=404, detail="媒体不存在")
    path = visual_monitor.config.storage_root / media_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="媒体不存在")
    return FileResponse(path)


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
    await visual_monitor.close()
    await execution_manager.close()


@app.on_event("startup")
async def resume_execution_timers() -> None:
    await execution_manager.resume_timers()


# 放在 API 路由之后,兜底提供前端页面
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
