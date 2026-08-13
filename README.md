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
- **ROS2 Camera Visual Monitor**:支持原子视觉与整链视觉；机器人客户端以 7 秒为名义周期上传无缺口的动态 RGB 视频窗口，服务器调用百炼并通过 SSE 展示观察结果

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

### ROS2 Camera Visual Monitor

这一分支用于机器人本体部署。Web/VLM 服务端和 ROS2 相机客户端位于同一仓库、
同一台 ARM，但保持为两个独立进程；底盘只负责把 ARM 的 Web 端口转发到办公
局域网：

```text
浏览器 ── http://192.168.51.168:8000 ──> 底盘 SSH 转发
                                                │
                                                v
ARM 127.0.0.1:8000  ── planner-monitor-web.service
ARM ROS2 topic       ── planner-monitor-ros2-camera.service
                           └─ /camera/head_left/image_rect
```

当前验证过的环境是 ROS2 Humble、Python 3、FFmpeg，以及头部左相机原生
`640×352` BGR8 图像。客户端默认从原图裁取 `x=160..480、y=92..308` 的
`320×216` 中央操作区域；CHAIN_BEFORE、动态视频、PREV_NOW、CURRENT_NOW 和实时预览使用完全相同的
ROI。下面的命令都从仓库根目录执行。

#### 1. 在 ARM 安装代码和依赖

从底盘进入 ARM，检出专用分支：

```bash
ssh root@192.168.51.168
ssh arm

mkdir -p /root/workspace
cd /root/workspace
git clone https://github.com/Frankaaay/planner_monitor.git planner_monitor
cd planner_monitor
git switch frank/ros2-camera-monitor
git pull --ff-only origin frank/ros2-camera-monitor
```

如果仓库已经存在，只执行最后三行。安装系统编码器，并创建能够读取系统 ROS2
Python 包的虚拟环境：

```bash
apt-get update
apt-get install -y ffmpeg python3-venv

cd /root/workspace/planner_monitor
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r monitor/requirements-ros2-camera.txt
```

`--system-site-packages` 不能省略，否则虚拟环境通常找不到系统安装的 `rclpy`、
`sensor_msgs` 和 `cv_bridge`。

#### 2. 配置 ARM 环境变量

在 `/root/workspace/planner_monitor/.env` 中配置：

```dotenv
DASHSCOPE_API_KEY=填入百炼北京地域的Key
VISUAL_MONITOR_TOKEN=服务端和相机客户端共享的随机Token

# 可选；设置后网页控制操作也需要该Token
# OPERATOR_TOKEN=另一个随机Token

VISUAL_MONITOR_MODEL=qwen3.7-plus
VISUAL_MONITOR_RETENTION_HOURS=24
VISUAL_MONITOR_MAX_CONCURRENCY=1
```

不要提交 `.env`。`DASHSCOPE_API_KEY` 只由 Web/VLM 服务读取；相机客户端只需
`VISUAL_MONITOR_TOKEN`。部署在同一台 ARM 时，客户端默认连接
`http://127.0.0.1:8000`，无需经过底盘或公网。

#### 3. 验证 ROS2 相机

先确认 topic、类型、编码和分辨率：

```bash
source /opt/ros/humble/setup.bash
ros2 topic list | grep /camera/head_left/image_rect
ros2 topic type /camera/head_left/image_rect
ros2 topic hz /camera/head_left/image_rect
ros2 topic echo --once /camera/head_left/image_rect | sed -n '1,20p'
```

预期 topic 类型为 `sensor_msgs/msg/Image`，当前实机图像为 BGR8、`640×352`。
如果机器人还需要额外 workspace 才能发现 topic，应在 `source
/opt/ros/humble/setup.bash` 后继续 source 该 workspace 的 `install/setup.bash`，
并把同样的 source 命令加入相机 systemd unit 的 `ExecStart`。

#### 4. 启动 Web 服务并运行只上传 probe

先手工启动 Web 服务：

```bash
cd /root/workspace/planner_monitor
.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1
```

另开一个 ARM 终端运行 probe。它按默认配置采集完整 7 秒、编码 42 帧 H.264 MP4并上传，
但不会调用百炼：

```bash
cd /root/workspace/planner_monitor
source /opt/ros/humble/setup.bash
.venv/bin/python -m monitor.ros2_camera_client probe --no-proxy
```

输出应包含 `source_resolution=[320,216]`、`frame_count_captured`、
`frame_count_uploaded=42`、`capture_ms`、`encode_ms`、
`client_upload_roundtrip_ms`、`server_write_ms` 和 `local_file_bytes`。当前实机基线
输入是从原生 `640×352` 裁取的 `320×216` ROI、6 FPS、7 秒；`capture_ms` 应
接近 7000 ms。`--no-proxy` 只表示
Python HTTP 客户端忽略 `HTTP_PROXY/HTTPS_PROXY`，不会改变 ROS2 或底盘转发。

#### 5. 安装 ARM systemd 服务

probe 成功后安装服务模板：

```bash
cd /root/workspace/planner_monitor
mkdir -p /root/.ros/log/planner-monitor
install -m 0644 deploy/planner-monitor-web.service /etc/systemd/system/
install -m 0644 deploy/planner-monitor-ros2-camera.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now planner-monitor-web.service
systemctl enable --now planner-monitor-ros2-camera.service
```

检查实际进程、分支、提交和日志：

```bash
cd /root/workspace/planner_monitor
git branch --show-current
git rev-parse HEAD
systemctl --no-pager --full status planner-monitor-web.service
systemctl --no-pager --full status planner-monitor-ros2-camera.service
journalctl -u planner-monitor-web.service -n 100 --no-pager
journalctl -u planner-monitor-ros2-camera.service -n 100 --no-pager
```

#### 6. 在底盘暴露局域网端口

退出到 `root@192.168.51.168` 底盘终端，确认 `ssh arm` 已能免密登录，再安装
转发服务：

```bash
scp arm:/root/workspace/planner_monitor/deploy/planner-monitor-lan-forward.service /tmp/planner-monitor-lan-forward.service
install -m 0644 /tmp/planner-monitor-lan-forward.service /etc/systemd/system/planner-monitor-lan-forward.service
systemctl daemon-reload
systemctl enable --now planner-monitor-lan-forward.service
systemctl --no-pager --full status planner-monitor-lan-forward.service
ss -lntp | grep 192.168.51.168:8000
```

模板文件来自仓库的 `deploy/planner-monitor-lan-forward.service`。浏览器随后访问：

```text
http://192.168.51.168:8000
```

页面创建计划后选择“原子视觉”或“整链视觉”并开始。相机服务会自动领取
assignment，无需再手工运行客户端。

#### 7. 更新、回滚前检查和常用诊断

更新当前分支时先记录提交，再快进拉取并重启两个 ARM 服务：

```bash
cd /root/workspace/planner_monitor
git status --short
git rev-parse HEAD
git pull --ff-only origin frank/ros2-camera-monitor
systemctl restart planner-monitor-web.service planner-monitor-ros2-camera.service
systemctl is-active planner-monitor-web.service planner-monitor-ros2-camera.service
```

局域网打不开时依次检查 ARM Web 服务、底盘转发和浏览器入口；有画面但不触发
VLM 时检查相机客户端的 assignment 与 checkpoint 日志：

```bash
# ARM
curl -fsS http://127.0.0.1:8000/api/providers
journalctl -u planner-monitor-ros2-camera.service -f

# 底盘
curl -fsS http://192.168.51.168:8000/api/providers
journalctl -u planner-monitor-lan-forward.service -n 100 --no-pager
```

首次接入或修改相机参数后，应重新执行 probe，再恢复持续相机服务。

客户端默认订阅 `/camera/head_left/image_rect`，从相机原生 `640×352` 图像裁取
`--crop-x 160 --crop-y 92 --crop-width 320 --crop-height 216`。裁切发生在统一
采集入口，不做放大，因此 BEFORE、VLM 视频、NOW 和实时预览不会出现范围不一致。
修改 ROI 后必须重新运行 probe；ROI 必须位于原图内，宽高必须为偶数。VLM 视频
降采样为6 FPS、H.264 CRF 28，默认名义窗口和新覆盖周期均为7秒。独立线程每1秒领取一次当前 assignment，编码和上传也在线程中进行，
因此ROS回调不会等待网络。每次切换步骤都会生成新的generation、清空旧帧、重拍
CHAIN_BEFORE并重新积累首个7秒窗口；旧generation的上传结果会被忽略。客户端只允许一个上传/推理请求占用当前 assignment。已被服务端接受的窗口结束时间是连续覆盖游标；如果百炼耗时超过7秒，下一段从上一结束点前1秒开始，一直覆盖到最新帧。单段最长15秒，积压更长时拆成连续的15秒片段逐段追回，不会改成“只取最新7秒”而漏掉推理期间的动作。每次请求还上传上一窗口结尾的 PREV_NOW，帮助模型连接跨窗口状态。VLM返回
`succeeded`时后端自动推进；返回`failed`时暂停等待人工确认。

整链视觉模式仍使用 Planner 生成的稳定步骤 ID，但每次 Prompt 同时包含原始指令、完整动作契约、后端已确认步骤、当前及后续未完成步骤。模型业务状态只有 `succeeded`、`in_progress`、`failed`：遮挡、画质或身份无法确认也返回 `in_progress`，并在描述中写明原因。模型只返回从当前步骤开始的连续前缀，并在第一个 `in_progress` 或 `failed` 处停止；不再为更后面的未执行步骤输出空 evidence 占位结果。后端只接受连续成功前缀，因此一个窗口可以推进多个步骤，但不能跳步、回退或越过失败。第一个窗口没有完成的动作可以在后续窗口继续判断，已确认成功的步骤进入跨窗口账本且不会回退。Prompt 不再回灌上一轮 `in_progress` 的自由文本描述，跨窗口只依赖步骤账本、PREV_NOW、1秒重叠视频和当前窗口证据。Pick 使用 transition_or_state、Carry 使用 transition_event、Place 使用 persistent_state 的动作时间语义；最后步骤仍必须在 CURRENT_NOW 中成立。百炼偶发返回单元素对象数组时会有限解包；其他异常结构仍严格拒绝。服务端同时保存原始响应、标准化 JSON 和验证错误，便于区分模型判断问题和输出契约问题。

同一采集循环还会分出独立实时预览：目标上限为10 FPS、默认 `320×216` ROI、
JPEG quality 65，通过二进制WebSocket上传，不使用Base64；实际FPS不会超过ROS2
相机真实发布频率。服务端和每个网页订阅者都只保留最新一帧，慢连接覆盖旧帧，
不会阻塞相机或VLM。网页显示最近2秒实际收到的预览FPS和采集到展示的延迟。
VLM进入等待人工确认后，动态窗口和百炼请求暂停，但实时预览继续。网页会显示本轮实际窗口长度和模型相对实时画面的落后时间。

每个 VLM 检查点上传 `CHAIN_BEFORE`、`PREV_NOW`、7～15 秒动态 H.264 视频和独立 `CURRENT_NOW` 结尾帧。实际 `window_duration_s` 同步驱动 Prompt、严格 JSON Schema、时间戳校验和完成证据提取，不再写死6秒。最终状态以 CURRENT_NOW 为准：窗口中途曾达到成功条件、但结尾已不满足时不能返回 `succeeded`；完成证据必须位于窗口最后1秒。以 Pick 为例，拿起后又放回原支撑面属于 `in_progress`。

公网部署需要为 `/api/visual-monitor/live/` 转发 WebSocket Upgrade；当前服务器配置模板见 `deploy/nginx-instruction-breakdown.conf`。观看端使用 `OPERATOR_TOKEN`、采集端使用 `VISUAL_MONITOR_TOKEN`，认证消息在 WebSocket 建立后通过 TLS 发送，不放入 URL。

HTTP API(供其他程序调用):

- `GET /api/providers` — 可用提供商列表
- `GET /api/operations` — 17 个原子操作与专家操作目录
- `POST /api/decompose` — body: `{"instruction": "把牛奶放进冰箱", "provider": "deepseek"}`
- `POST /api/executions` — 拆解指令并创建 `ready` 执行会话,body 与 `/api/decompose` 相同
- `GET /api/executions/{id}` — 获取执行会话权威快照
- `POST /api/executions/{id}/start` — 确认并启动执行
- `POST /api/executions/{id}/mode` — 开始前切换 `robot_agent` / `visual_monitor` / `chain_visual_monitor`
- `POST /api/executions/{id}/reports` — 人工/机器人 Monitor 上报当前 attempt 结果
- `POST /api/executions/{id}/retry` — 暂停后再尝试当前步骤一次
- `POST /api/executions/{id}/terminate` — 终止执行
- `GET /api/executions/{id}/events` — SSE 执行事件流
- `POST /api/agent/workflows/register` — arm Agent 注册脱敏后的 YAML workflow DAG
- `POST /api/agent/claim` — arm Agent 主动领取当前 `A_001 Pick`
- `POST /api/agent/heartbeat` — 保持机器人 workflow claim 存活
- `POST /api/agent/events` — 幂等上报内部节点状态、退出码和有限日志
- `POST /api/agent/complete` — 幂等提交 `succeeded`、`failed` 或 `needs_operator`
- `POST /api/visual-monitor/claim` — ROS2相机客户端领取当前视觉Monitor任务
- `POST /api/visual-monitor/baseline` — 上传操作开始前的 JPEG
- `POST /api/visual-monitor/checkpoints` — 上传动态 H.264 窗口、PREV_NOW/CURRENT_NOW 和实际时长并异步触发百炼
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
