# Instruction Break-Down 指令拆解

把自然语言指令拆解为机器人可执行的**原子操作序列**。输入「把水壶放到桌子上」,输出:

```
1. [A_001 Pick/logic0]   拿起水壶。        (Pick up the kettle.)
2. [A_003 Carry/logic0]  搬运水壶。        (Carry the kettle.)
3. [A_002 Place/logic1]  把水壶放在桌子上。 (Place the kettle on the table.)
```

拆解由 LLM 完成,支持智谱 GLM / DeepSeek / OpenRouter 三家提供商(OpenAI 兼容接口,可一行扩展)。原子操作定义(17 个,A_001~A_017)见 [action primitives](action%20primitives),该文件是唯一数据源:prompt 与校验规则都从它生成,修改它无需改代码。

## 特性

- **三种使用方式**:命令行单条 / 交互式控制台 / Web 页面(含 HTTP API)
- **隐含步骤推理**:「把牛奶放进冰箱」自动补出拉开冰箱门、推上冰箱门
- **异常输入处理**:指令不明确(「放到那边」)→ 返回缺少什么信息;超出原子操作能力(「切成两半」)→ 返回无法完成的原因
- **输出可校验**:LLM 输出 JSON 后经程序校验(action_id / logic 模板是否合法),不合法自动重试
- **执行闭环**:拆解结果可创建后端执行会话,由虚拟 Monitor 或真机通过同一接口上报成功/失败
- **服务端超时**:每个原子操作默认等待 20 秒,失败或超时自动重试,连续 3 次后暂停等待人工处理

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env        # 填入至少一家的 API key
```

### 命令行

```bash
python decompose.py "把水壶放到桌子上" --provider deepseek   # 单条
python decompose.py                                          # 交互式控制台,exit/quit/q 退出
```

参数:`--provider glm | deepseek | openrouter`(默认 deepseek)、`--model` 覆盖默认模型、`--json` 输出结构化结果。

退出码:`0` 成功,`2` 指令不明确,`3` 无法完成,`1` 其他错误。

### Web 页面

```bash
python server.py            # 监听 0.0.0.0:8000
```

浏览器打开 `http://localhost:8000`:左侧是可折叠/搜索的原子与专家操作库;主区域可审阅拆解计划、启动执行、观察步骤高亮与倒计时,并用虚拟 Monitor 上报成功/失败。

HTTP API(供其他程序调用):

- `GET /api/providers` — 可用提供商列表
- `GET /api/operations` — 17 个原子操作与专家操作目录
- `POST /api/decompose` — body: `{"instruction": "把牛奶放进冰箱", "provider": "deepseek"}`
- `POST /api/executions` — 拆解指令并创建 `ready` 执行会话,body 与 `/api/decompose` 相同
- `GET /api/executions/{id}` — 获取执行会话权威快照
- `POST /api/executions/{id}/start` — 确认并启动执行
- `POST /api/executions/{id}/reports` — 人工/机器人 Monitor 上报当前 attempt 结果
- `POST /api/executions/{id}/retry` — 暂停后再尝试当前步骤一次
- `POST /api/executions/{id}/terminate` — 终止执行
- `GET /api/executions/{id}/events` — SSE 执行事件流

Monitor 上报示例（`step_id` 与 `attempt_id` 从执行快照或 `step.started` 事件获得）:

```json
{
  "report_id": "monitor-event-0001",
  "step_id": "...",
  "attempt_id": "...",
  "outcome": "success",
  "source": "robot",
  "detail": "optional detector output"
}
```

`report_id` 用于请求幂等;过期的 step/attempt 会返回 HTTP `409`,不会误推进任务。

执行状态 v1 仅保存在当前 Python 进程内,因此必须使用单 worker:

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1
```

> 注意:服务重启会丢失执行会话;API 暂无鉴权,仅适合本机或受信网络。公网部署前需增加持久化、反向代理和访问控制。

## 异常输入示例

```
$ python decompose.py "把水壶放到那边"
[指令不明确] 「那边」未指明具体位置,请说明要把水壶放到哪个表面(如桌子)或空间(如柜子)。

$ python decompose.py "把苹果切成两半"
[无法完成] 「切成两半」需要切割动作,不在 17 个原子操作能力范围内,无法拆解。
```

更多测试用例见 [TEST_INSTRUCTIONS.md](TEST_INSTRUCTIONS.md)(正常 / 隐含步骤 / 全操作覆盖 / 不明确 / 无法完成 / 边界用例,共 30 条)。

## 代码结构

| 文件 | 职责 |
|---|---|
| `decompose.py` | CLI 入口 + 拆解主流程:调 LLM → 校验 → 渲染输出 |
| `server.py` | Web 服务 (FastAPI):提供页面与 HTTP API |
| `execution.py` | 执行状态机:会话、attempt、服务端超时、重试与 SSE 订阅 |
| `static/index.html` | 前端单页(纯 HTML/CSS/JS,无构建依赖) |
| `static/app.js` | 执行控制台交互、SSE 同步与虚拟 Monitor 上报 |
| `prompt_builder.py` | 构建 system prompt:嵌入 action primitives 原文 + 拆解规则 + few-shot 示例 |
| `providers.py` | 提供商注册表:base_url / 默认模型 / key 环境变量,新增提供商加一行 |
| `primitives.py` | 解析 action primitives 文件,校验 LLM 输出 |
| `action primitives` | 原子操作定义(唯一数据源) |

## 配置

API key 通过环境变量或 `.env` 提供(见 `.env.example`):

| 环境变量 | 提供商 | 默认模型 |
|---|---|---|
| `ZHIPU_API_KEY` | 智谱 GLM | glm-4.6 |
| `DEEPSEEK_API_KEY` | DeepSeek | deepseek-chat |
| `OPENROUTER_API_KEY` | OpenRouter | deepseek/deepseek-chat-v3-0324 |

执行配置:

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `MONITOR_TIMEOUT_SECONDS` | `20` | 当前原子操作等待 monitor 回报的服务端超时秒数 |

`.env` 已在 `.gitignore` 中,不会被提交。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试不调用外部 LLM,覆盖成功推进、失败重试、超时暂停、人工恢复、终止、幂等和过期回报拒绝。
