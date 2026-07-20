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
import json
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
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
async def start_execution(execution_id: str) -> dict:
    try:
        return {"execution": await execution_manager.start(execution_id)}
    except (ExecutionNotFoundError, ExecutionConflictError) as exc:
        raise _http_error(exc) from exc


@app.post("/api/executions/{execution_id}/reports")
async def report_execution(execution_id: str, req: MonitorReportRequest) -> dict:
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
async def retry_execution(execution_id: str) -> dict:
    try:
        return {"execution": await execution_manager.retry(execution_id)}
    except (ExecutionNotFoundError, ExecutionConflictError) as exc:
        raise _http_error(exc) from exc


@app.post("/api/executions/{execution_id}/terminate")
async def terminate_execution(execution_id: str) -> dict:
    try:
        return {"execution": await execution_manager.terminate(execution_id)}
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


# 放在 API 路由之后,兜底提供前端页面
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
