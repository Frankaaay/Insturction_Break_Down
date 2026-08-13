# -*- coding: utf-8 -*-
"""ROS2 Image-topic adapter for the Planner Monitor camera client."""

from __future__ import annotations

import json
import time
from argparse import ArgumentParser, Namespace
from typing import Any

from monitor.camera_monitor_client import (
    MonitorClient,
    load_client_environment,
    parser as common_parser,
    validate_args,
)


DEFAULT_TOPIC = "/camera/head_left/image_rect"
DEFAULT_CAMERA_ID = "ros2.head_left.image_rect"
DEFAULT_CROP_X = 160
DEFAULT_CROP_Y = 92
DEFAULT_CROP_WIDTH = 320
DEFAULT_CROP_HEIGHT = 216


def crop_frame(frame: Any, x: int, y: int, width: int, height: int) -> Any:
    """Return one validated ROI without resizing or changing its pixels."""
    source_height, source_width = frame.shape[:2]
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise RuntimeError("相机 ROI 的坐标必须非负，宽高必须大于 0")
    if x + width > source_width or y + height > source_height:
        raise RuntimeError(
            f"相机 ROI {x},{y},{width},{height} 超出原图 {source_width}x{source_height}"
        )
    return frame[y:y + height, x:x + width].copy()


def require_ros2_modules() -> tuple[Any, Any, Any, Any, Any]:
    try:
        import cv2
        import rclpy
        from cv_bridge import CvBridge
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import Image as RosImage
    except ImportError as exc:
        raise RuntimeError(
            "缺少ROS2相机依赖；请先 source /opt/ros/humble/setup.bash，并使用"
            "包含系统ROS包的Python环境"
        ) from exc
    return rclpy, CvBridge, qos_profile_sensor_data, RosImage, cv2


class Ros2ImageFrameSource:
    """Pull the newest RGB frame from one sensor_msgs/Image topic."""

    def __init__(
        self, topic: str, camera_id: str, *,
        crop_x: int = DEFAULT_CROP_X, crop_y: int = DEFAULT_CROP_Y,
        crop_width: int = DEFAULT_CROP_WIDTH, crop_height: int = DEFAULT_CROP_HEIGHT,
    ) -> None:
        self.topic = topic
        self.camera_id = camera_id
        self.crop_x = crop_x
        self.crop_y = crop_y
        self.crop_width = crop_width
        self.crop_height = crop_height
        self._rclpy, bridge_type, self._qos, self._image_type, self._cv2 = require_ros2_modules()
        self._bridge = bridge_type()
        self._node: Any | None = None
        self._subscription: Any | None = None
        self._latest: tuple[int, float, Any] | None = None
        self._consumed_sequence = 0
        self._sequence = 0
        self._owns_context = False

    def start(self) -> None:
        if self._node is not None:
            return
        if not self._rclpy.ok():
            self._rclpy.init()
            self._owns_context = True
        self._node = self._rclpy.create_node("planner_monitor_ros2_camera")
        self._subscription = self._node.create_subscription(
            self._image_type,
            self.topic,
            self._on_image,
            self._qos,
        )

    def _on_image(self, message: Any) -> None:
        bgr = self._bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
        rgb = self._cv2.cvtColor(bgr, self._cv2.COLOR_BGR2RGB)
        rgb = crop_frame(
            rgb, self.crop_x, self.crop_y, self.crop_width, self.crop_height,
        )
        self._sequence += 1
        # Arrival wall time is used by the HTTP telemetry contract. The ROS
        # header clock may be simulated or configured independently.
        self._latest = (self._sequence, time.time(), rgb.copy())

    def read(self, timeout_seconds: float) -> tuple[float, Any] | None:
        if self._node is None:
            raise RuntimeError("ROS2 frame source 尚未启动")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            self._rclpy.spin_once(self._node, timeout_sec=min(0.2, remaining))
            latest = self._latest
            if latest is not None and latest[0] > self._consumed_sequence:
                self._consumed_sequence = latest[0]
                return latest[1], latest[2]
        return None

    def stop(self) -> None:
        if self._node is not None:
            self._node.destroy_node()
            self._node = None
            self._subscription = None
        if self._owns_context and self._rclpy.ok():
            self._rclpy.shutdown()
        self._owns_context = False


def parser() -> ArgumentParser:
    result = common_parser()
    result.add_argument("--topic", default=DEFAULT_TOPIC)
    result.add_argument("--camera-id", default=DEFAULT_CAMERA_ID)
    result.add_argument("--crop-x", type=int, default=DEFAULT_CROP_X)
    result.add_argument("--crop-y", type=int, default=DEFAULT_CROP_Y)
    result.add_argument("--crop-width", type=int, default=DEFAULT_CROP_WIDTH)
    result.add_argument("--crop-height", type=int, default=DEFAULT_CROP_HEIGHT)
    result.set_defaults(
        preview_width=DEFAULT_CROP_WIDTH,
        preview_height=DEFAULT_CROP_HEIGHT,
    )
    return result


def validate_ros2_args(args: Namespace) -> None:
    validate_args(args)
    if not args.topic.startswith("/"):
        raise SystemExit("ROS2 topic 必须是以 / 开头的绝对名称")
    if not args.camera_id or len(args.camera_id) > 128:
        raise SystemExit("camera-id 必须为1到128个字符")
    if (
        args.crop_x < 0 or args.crop_y < 0
        or args.crop_width <= 0 or args.crop_height <= 0
        or args.crop_width % 2 or args.crop_height % 2
    ):
        raise SystemExit("ROI 坐标必须非负，ROI 宽高必须为正偶数")


def main() -> int:
    args = parser().parse_args()
    validate_ros2_args(args)
    load_client_environment()
    source = Ros2ImageFrameSource(
        args.topic, args.camera_id,
        crop_x=args.crop_x, crop_y=args.crop_y,
        crop_width=args.crop_width, crop_height=args.crop_height,
    )
    client = MonitorClient(args, source)
    if args.command == "probe":
        try:
            print(json.dumps(client.capture_probe(), ensure_ascii=False, indent=2))
        finally:
            client.http.close()
    else:
        client.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
