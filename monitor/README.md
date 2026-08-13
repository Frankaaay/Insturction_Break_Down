# ROS2 Camera Monitor

`monitor/` 包含 Planner Monitor 的相机采集端和离线视频验证工具。

机器人部署使用：

- `ros2_camera_client.py`：订阅 ROS2 `sensor_msgs/msg/Image` 并提供 `probe`/`run` 入口。
- `camera_monitor_client.py`：与相机类型无关的采集缓冲、H.264 编码、上传和实时预览逻辑。
- `requirements-ros2-camera.txt`：ARM 相机客户端的 Python 依赖；ROS2 二进制包继续使用系统环境。

完整的 ARM 安装、ROS2 topic 验证、上传 probe、systemd 服务、底盘端口转发、
更新和排障步骤统一维护在仓库根目录的 [`README.md`](../README.md)。

离线录制视频实验仍保留在 `scripts/`、`prompts/` 和 `data/` 中，但不属于机器人
部署的必需步骤。实验产生的视频、模型响应、日志和密钥不应提交到仓库。
