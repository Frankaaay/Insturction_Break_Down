# -*- coding: utf-8 -*-
"""指令拆解 Web 服务。

    python server.py                      # 监听 0.0.0.0:8000
    uvicorn server:app --host 0.0.0.0     # 或用 uvicorn 直接起

接口:
    GET  /                  前端页面 (static/index.html)
    GET  /api/providers     可用提供商列表
    POST /api/decompose     {"instruction": "...", "provider": "deepseek", "model": null}
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from decompose import decompose
from providers import PROVIDERS

app = FastAPI(title="指令拆解 Instruction Break-Down")

STATIC_DIR = Path(__file__).parent / "static"


class DecomposeRequest(BaseModel):
    instruction: str
    provider: str = "deepseek"
    model: str | None = None


@app.get("/api/providers")
def list_providers() -> dict:
    return {"providers": list(PROVIDERS), "default": "deepseek"}


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


# 放在 API 路由之后,兜底提供前端页面
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
