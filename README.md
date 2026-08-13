# Planner Monitor ROS2 部署

本分支把机器人头部 ROS2 相机接入 Planner Monitor：ARM 上的相机客户端订阅
`sensor_msgs/msg/Image`，编码当前视频窗口并发送给同机 VLM 服务；底盘只负责将
ARM 的服务端口转发到办公局域网。

本文只说明机器人环境的安装、验证、启动、更新和排障。相机采集目录的简要说明
见 [`monitor/README.md`](monitor/README.md)。

## 部署结构

```text
办公局域网
    │ 192.168.51.168:8000
    v
机器人底盘
    │ systemd SSH local forward
    v
ARM 127.0.0.1:8000 ── planner-monitor-web.service
ARM ROS2 topic      ── planner-monitor-ros2-camera.service
                          └─ /camera/head_left/image_rect
```

服务端与 ROS2 相机客户端位于同一仓库、同一台 ARM，但必须保持为两个独立
进程。相机采集、视频编码和网络请求不应阻塞 ROS2 回调。

## 当前实机默认值

| 项目 | 默认值 |
|---|---|
| ROS2 | Humble |
| Topic | `/camera/head_left/image_rect` |
| 消息类型 | `sensor_msgs/msg/Image` |
| 原始图像 | `640×352` BGR8 |
| 裁切 ROI | `x=160, y=92, width=320, height=216` |
| VLM 视频 | H.264、6 FPS、CRF 28、无音频 |
| 名义窗口/周期 | 7 秒 / 7 秒 |
| 相邻窗口重叠 | 1 秒 |
| 单窗口上限 | 15 秒 |
| assignment 轮询 | 1 秒 |
| 实时预览上限 | 10 FPS、JPEG quality 65 |
| 默认模型 | `qwen3.7-plus` |

每次 VLM 请求只有一段当前视频。整链模式会由服务端附加 `CHAIN_BEFORE`、
每个已确认步骤的一张冻结成功关键帧和 `CURRENT_NOW`；不会上传历史视频。

## 1. 登录 ARM 并检出分支

先进入底盘，再通过底盘的 SSH 配置进入 ARM：

```bash
ssh root@192.168.51.168
ssh arm
```

首次部署：

```bash
mkdir -p /root/workspace
cd /root/workspace
git clone https://github.com/Frankaaay/planner_monitor.git planner_monitor
cd planner_monitor
git switch frank/ros2-camera-monitor
git pull --ff-only origin frank/ros2-camera-monitor
```

仓库已经存在时：

```bash
cd /root/workspace/planner_monitor
git status --short
git branch --show-current
git pull --ff-only origin frank/ros2-camera-monitor
```

如果 `git status --short` 非空，先确认这些改动的归属；不要直接覆盖或清理。

## 2. 安装 ARM 依赖

```bash
apt-get update
apt-get install -y ffmpeg python3-venv

cd /root/workspace/planner_monitor
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r monitor/requirements-ros2-camera.txt
```

必须使用 `--system-site-packages`。机器人上的 `rclpy`、`sensor_msgs`、
`cv_bridge` 和 OpenCV 来自 ROS2/厂商系统环境；重新从 PyPI 安装可能造成 ABI
不兼容。

确认关键依赖：

```bash
source /opt/ros/humble/setup.bash
.venv/bin/python - <<'PY'
import cv2
import rclpy
from cv_bridge import CvBridge
from sensor_msgs.msg import Image
print("ROS2 Python dependencies: OK")
PY

ffmpeg -version | sed -n '1,3p'
```

## 3. 配置环境变量

```bash
cd /root/workspace/planner_monitor
test -e .env || cp .env.example .env
chmod 600 .env
```

编辑 `/root/workspace/planner_monitor/.env`，至少确认以下配置：

```dotenv
DASHSCOPE_API_KEY=百炼北京地域Key
VISUAL_MONITOR_TOKEN=服务端与相机客户端共享的随机Token

VISUAL_MONITOR_MODEL=qwen3.7-plus
VISUAL_MONITOR_STORAGE_ROOT=./monitor/runtime_data
VISUAL_MONITOR_RETENTION_HOURS=24
VISUAL_MONITOR_MAX_CONCURRENCY=1
VISUAL_MONITOR_API_TIMEOUT_SECONDS=30
VISUAL_MONITOR_ATTEMPT_TIMEOUT_SECONDS=120

# 推荐启用；否则服务重启后执行会话只存在于内存中。
EXECUTION_DB_PATH=./data/executions.db

# 可选：保护控制接口。
OPERATOR_TOKEN=
```

不要提交 `.env`。相机客户端只需要 `VISUAL_MONITOR_TOKEN`；百炼 Key 只由服务端
请求使用。systemd 通过 `EnvironmentFile` 读取 `.env`，不要在交互式 shell 中
直接 `source .env`。

## 4. 验证 ROS2 相机

```bash
source /opt/ros/humble/setup.bash
ros2 topic list | grep /camera/head_left/image_rect
ros2 topic type /camera/head_left/image_rect
ros2 topic hz /camera/head_left/image_rect
ros2 topic echo --once /camera/head_left/image_rect | sed -n '1,24p'
```

预期类型为 `sensor_msgs/msg/Image`，实机当前编码为 BGR8、分辨率为 `640×352`。
如果机器人必须额外 source 厂商 workspace 才能发现 topic，应先验证对应的
`install/setup.bash`，再把同一条 source 命令加入
`deploy/planner-monitor-ros2-camera.service` 的 `ExecStart`。

## 5. 启动服务端并运行上传 probe

先只安装和启动服务端：

```bash
cd /root/workspace/planner_monitor
install -m 0644 deploy/planner-monitor-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now planner-monitor-web.service
systemctl is-active planner-monitor-web.service
curl -fsS http://127.0.0.1:8000/api/providers >/dev/null
```

运行只采集、编码和上传的 probe。它不会请求百炼：

```bash
cd /root/workspace/planner_monitor
source /opt/ros/humble/setup.bash
.venv/bin/python -m monitor.ros2_camera_client probe --no-proxy
```

正常结果应包含：

- `source_resolution=[320,216]`
- `frame_count_uploaded=42`
- `capture_ms` 接近 7000 ms
- `encode_ms`
- `client_upload_roundtrip_ms`
- `server_write_ms`
- `local_file_bytes`

`--no-proxy` 只让 Python HTTP 客户端忽略 `HTTP_PROXY`/`HTTPS_PROXY`，不会改变
ROS2 通信或底盘端口转发。

如需临时验证其他 ROI，应先运行 probe，不要直接修改 systemd：

```bash
.venv/bin/python -m monitor.ros2_camera_client probe --no-proxy \
  --crop-x 160 --crop-y 92 --crop-width 320 --crop-height 216
```

ROI 必须位于原图内部，宽高必须为正偶数。BEFORE、视频、NOW 和实时预览共用
同一 ROI。

## 6. 安装持续相机服务

probe 通过后安装相机客户端：

```bash
cd /root/workspace/planner_monitor
mkdir -p /root/.ros/log/planner-monitor
install -m 0644 deploy/planner-monitor-ros2-camera.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now planner-monitor-ros2-camera.service
```

检查两个 ARM 服务：

```bash
systemctl is-active planner-monitor-web.service planner-monitor-ros2-camera.service
systemctl --no-pager --full status planner-monitor-web.service
systemctl --no-pager --full status planner-monitor-ros2-camera.service
journalctl -u planner-monitor-web.service -n 100 --no-pager
journalctl -u planner-monitor-ros2-camera.service -n 100 --no-pager
```

相机日志正常时应出现 `camera.ready`、周期性 assignment 轮询，以及有任务时的
baseline/checkpoint 事件。

## 7. 在底盘配置局域网转发

回到底盘 `root@192.168.51.168`，确认 `ssh arm` 已免密可用：

```bash
ssh -o BatchMode=yes arm true
```

安装转发服务：

```bash
scp arm:/root/workspace/planner_monitor/deploy/planner-monitor-lan-forward.service \
  /tmp/planner-monitor-lan-forward.service
install -m 0644 /tmp/planner-monitor-lan-forward.service \
  /etc/systemd/system/planner-monitor-lan-forward.service
systemctl daemon-reload
systemctl enable --now planner-monitor-lan-forward.service
```

验证：

```bash
systemctl is-active planner-monitor-lan-forward.service
ss -lntp | grep 192.168.51.168:8000
curl -fsS http://192.168.51.168:8000/api/providers >/dev/null
```

转发模板使用 SSH local forward：

```text
192.168.51.168:8000 -> ARM 127.0.0.1:8000
```

## 8. 更新部署

先读取状态，再执行快进更新：

```bash
ssh root@192.168.51.168
ssh arm

cd /root/workspace/planner_monitor
git status --short
git branch --show-current
git rev-parse HEAD
git fetch origin frank/ros2-camera-monitor
git log --oneline HEAD..origin/frank/ros2-camera-monitor
git pull --ff-only origin frank/ros2-camera-monitor
```

确认更新成功后重启：

```bash
systemctl restart planner-monitor-web.service planner-monitor-ros2-camera.service
systemctl is-active planner-monitor-web.service planner-monitor-ros2-camera.service
git rev-parse HEAD
```

如果未配置 `EXECUTION_DB_PATH`，重启服务端会丢失内存中的执行会话。即使已启用
SQLite，也应在无正在推理的任务时更新。

## 9. 常用诊断

### ARM 服务端不可用

```bash
systemctl --no-pager --full status planner-monitor-web.service
journalctl -u planner-monitor-web.service -n 150 --no-pager
curl -v http://127.0.0.1:8000/api/providers
```

### ROS2 相机没有画面

```bash
source /opt/ros/humble/setup.bash
ros2 topic hz /camera/head_left/image_rect
journalctl -u planner-monitor-ros2-camera.service -n 150 --no-pager
```

重点检查 topic 是否存在、类型是否为 `sensor_msgs/msg/Image`、ROI 是否越界，以及
systemd 是否 source 了完整 ROS2 环境。

### 有画面但没有 checkpoint

```bash
journalctl -u planner-monitor-ros2-camera.service -f
```

依次查看 `assignment.claimed`、`baseline.uploaded`、`checkpoint.uploaded`，以及
`checkpoint.deferred` 的原因。同一 assignment 只允许一个上传/推理请求在途；
百炼尚未返回时，相机缓冲仍继续采集，下一个窗口会从连续覆盖游标继续。

### 局域网端口不可达

```bash
# 底盘
systemctl --no-pager --full status planner-monitor-lan-forward.service
journalctl -u planner-monitor-lan-forward.service -n 100 --no-pager
ssh -o BatchMode=yes arm true
ss -lntp | grep 192.168.51.168:8000
```

### 查看实际代码版本

```bash
cd /root/workspace/planner_monitor
git branch --show-current
git rev-parse HEAD
git status --short
```

报告问题时同时提供上述 Git 信息、两个 ARM 服务状态、相关日志，以及发生问题的
checkpoint sequence 和保存的媒体文件名。

## 10. 验证代码

在开发机或 ARM 仓库根目录运行：

```bash
python -m unittest discover -s tests
python -m unittest discover -s monitor/tests
```

这些测试不发起付费模型请求。
