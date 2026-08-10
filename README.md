# Planner Monitor

将自然语言 Planner、后端执行编排器和视觉 Monitor 实验代码统一到一个仓库。
当前线上功能把自然语言指令拆解为机器人可执行的**原子操作序列**；`monitor/`
保存后续接入执行闭环的视觉理解脚本、Prompt 和设计文档。

输入「把水壶放到桌子上」,Planner 输出:

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
- **GraspArm 子流程**:`A_001 Pick` 可由 arm X5 Agent 领取；网页按 Agent YAML 动态展示内部 DAG、服务健康、运行阶段和失败日志
- **服务端超时**:每个原子操作默认等待 20 秒,失败或超时自动重试,连续 3 次后暂停等待人工处理
- **RealSense Visual Monitor**:Windows 客户端每 6 秒上传一个完整 6 秒 RGB 视频窗口，服务器调用百炼并通过 SSE 展示观察结果；VLM 不自动推进任务

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

### RealSense Visual Monitor

Windows 端直接使用 `pyrealsense2`，不需要安装 ROS 或 `realsense-ros`。先安装本地采集依赖：

```powershell
python -m pip install -r monitor/requirements-realsense.txt
```

服务器 `.env` 至少配置 `DASHSCOPE_API_KEY` 和 `VISUAL_MONITOR_TOKEN`。本地只配置同一个 monitor token，不保存百炼 key。先测 6 秒视频的采集、编码和上传链路（不会请求百炼）：

```powershell
python monitor/realsense_client.py probe --server https://112.74.61.202
```

客户端会自动读取仓库根目录中被 Git 忽略的 `.env`；也可以用环境变量或
`--token` 显式覆盖 `VISUAL_MONITOR_TOKEN`。

如果代理规则还没将服务器设为直连，可临时加 `--no-proxy`；如果服务器使用不受信任的测试证书才加 `--insecure`。输出分别包含 `capture_ms`、`encode_ms`、`client_upload_roundtrip_ms`、`server_write_ms`、帧数和 MP4 大小。

正式本地监控：网页生成计划后选择“仅视觉 Monitor”并开始，再运行：

```powershell
python monitor/realsense_client.py run --server https://112.74.61.202
```

客户端使用 640×480@30 FPS 采集，向上传视频降采样为 6 FPS、H.264 CRF 28；每积累完整 6 秒便提交这 6 秒窗口，默认窗口和提交周期均为 6 秒。独立线程每 1 秒领取一次当前 assignment，编码和上传也在线程中进行，因此相机采集不会等待网络。每次切换步骤都会生成新的 generation、清空旧帧、重拍 BEFORE 并重新积累完整 6 秒窗口；旧 generation 的上传结果会被忽略。如果检查点到期时百炼仍在推理或本地上传槽繁忙，客户端每 0.5 秒重试并使用最新滚动窗口，不再浪费一个完整周期。VLM 返回 `succeeded` 或 `failed` 后，客户端停止 VLM 窗口的缓存、编码和百炼上传，但保持相机、实时预览与轻量 assignment 轮询；网页人工确认后进入下一步骤，或点击“判断不准确，继续监控”提升 monitor epoch、重拍 BEFORE。服务端以原子推理预留阻止同一 attempt 的并发或终态后重复百炼请求。服务器只允许 2 个跨相机并发推理，媒体保留 24 小时。当前已为 `A_001 Pick/logic0`、`A_003 Carry/logic0` 和 `A_002 Place/logic1` 建立动作专用视觉契约，其中 Carry 使用宽松成功和极窄失败边界。百炼默认 `qwen3.7-plus` 且关闭思考。网页显示最新状态、失败自然语言原因、完成证据帧和分段耗时，但必须由人点击确认后才推进操作链。

同一采集循环还会分出独立实时预览：默认以 3 FPS、640×480、JPEG quality 65 压缩，通过二进制 WebSocket 上传，不使用 Base64。服务端和每个网页订阅者都只保留最新一帧；慢连接覆盖旧帧，不会阻塞相机或 VLM。网页显示最近 2 秒实际收到的预览 FPS 和采集到展示的延迟。VLM 进入等待人工确认后，6 秒窗口和百炼请求暂停，但实时预览继续。可用 `--preview-fps`（最大 10）、`--preview-width`、`--preview-height` 和 `--preview-quality` 调整预览。

每个 VLM 检查点上传 `BEFORE`、6 秒 H.264 视频和独立 `NOW` 结尾帧。状态以 NOW 为准：窗口中途曾达到成功条件、但结尾已不满足时不能返回 `succeeded`；完成证据必须位于窗口最后 1 秒。以 Pick 为例，拿起后又放回原支撑面属于 `in_progress`。

公网部署需要为 `/api/visual-monitor/live/` 转发 WebSocket Upgrade；当前服务器配置模板见 `deploy/nginx-instruction-breakdown.conf`。观看端使用 `OPERATOR_TOKEN`、采集端使用 `VISUAL_MONITOR_TOKEN`，认证消息在 WebSocket 建立后通过 TLS 发送，不放入 URL。

HTTP API(供其他程序调用):

- `GET /api/providers` — 可用提供商列表
- `GET /api/operations` — 17 个原子操作与专家操作目录
- `POST /api/decompose` — body: `{"instruction": "把牛奶放进冰箱", "provider": "deepseek"}`
- `POST /api/executions` — 拆解指令并创建 `ready` 执行会话,body 与 `/api/decompose` 相同
- `GET /api/executions/{id}` — 获取执行会话权威快照
- `POST /api/executions/{id}/start` — 确认并启动执行
- `POST /api/executions/{id}/mode` — 开始前切换 `robot_agent` / `visual_monitor`
- `POST /api/executions/{id}/reports` — 人工/机器人 Monitor 上报当前 attempt 结果
- `POST /api/executions/{id}/retry` — 暂停后再尝试当前步骤一次
- `POST /api/executions/{id}/terminate` — 终止执行
- `GET /api/executions/{id}/events` — SSE 执行事件流
- `POST /api/agent/workflows/register` — arm Agent 注册脱敏后的 YAML workflow DAG
- `POST /api/agent/claim` — arm Agent 主动领取当前 `A_001 Pick`
- `POST /api/agent/heartbeat` — 保持机器人 workflow claim 存活
- `POST /api/agent/events` — 幂等上报内部节点状态、退出码和有限日志
- `POST /api/agent/complete` — 幂等提交 `succeeded`、`failed` 或 `needs_operator`
- `POST /api/visual-monitor/claim` — RealSense 客户端领取当前 Visual Pick attempt
- `POST /api/visual-monitor/baseline` — 上传操作开始前的 JPEG
- `POST /api/visual-monitor/checkpoints` — 上传 6 秒 H.264 检查窗口并异步触发百炼
- `POST /api/visual-monitor/upload-probe` — 只测采集/编码/上传，不请求模型

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

状态机定时器和 SSE 订阅仍由单个 Python 进程管理，因此即使启用 SQLite 也必须
使用单 worker：

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1
```

设置 `EXECUTION_DB_PATH` 后执行会话、Agent 注册的 workflow、每次 claim 的
不可变 DAG 快照和幂等事件会保存到 SQLite WAL。没有配置时仍保持原来的纯内存
开发模式。Agent YAML 是 workflow 的唯一权威；服务器只保存不含 shell 命令的
注册描述，前端按节点数量、类型和依赖动态渲染。注册完成后，尚未 claim 的
`A_001` attempt 也会显示 `等待 Agent` 的动态节点预览。

系统不增加用户账号。公开部署时应设置 `OPERATOR_TOKEN` 和
`GRASPARM_AGENT_TOKEN`:前者保护开始/重试/终止等控制操作,后者只供机器人
Agent 上报。页面在服务返回 401 时询问 operator token,并仅保存在当前浏览器。

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
| `monitor/` | Visual Monitor Lab：视频/图像理解脚本、Prompt、设计文档与独立依赖 |

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
| `EXECUTION_DB_PATH` | 空 | SQLite 持久化路径；空值保持内存模式 |
| `OPERATOR_TOKEN` | 空 | 可选的网站控制 token |
| `GRASPARM_AGENT_TOKEN` | 空 | 可选的 arm Agent Bearer token |

`.env` 已在 `.gitignore` 中,不会被提交。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试不调用外部 LLM,覆盖成功推进、失败重试、超时暂停、人工恢复、终止、幂等和过期回报拒绝。
