# -*- coding: utf-8 -*-
"""Windows RealSense RGB client for the Planner Monitor server."""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


def utc_iso(timestamp: float | None = None) -> str:
    value = datetime.fromtimestamp(timestamp or time.time(), timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require_camera_modules():
    try:
        import numpy as np
        import pyrealsense2 as rs
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "缺少 RealSense 客户端依赖，请运行: pip install -r monitor/requirements-realsense.txt"
        ) from exc
    return np, rs, Image


def encode_mp4(frames: list[Any], output: Path, fps: int = 6) -> None:
    if not frames:
        raise RuntimeError("没有可编码的视频帧")
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError("缺少 imageio-ffmpeg") from exc
    height, width = frames[0].shape[:2]
    writer = imageio_ffmpeg.write_frames(
        str(output), (width, height), fps=fps, codec="libx264", pix_fmt_in="rgb24",
        output_params=["-pix_fmt", "yuv420p", "-crf", "28", "-movflags", "+faststart", "-an"],
    )
    writer.send(None)
    try:
        for frame in frames:
            writer.send(frame.tobytes())
    finally:
        writer.close()


def sample_window(buffer: deque, start: float, end: float, fps: int = 6) -> list[Any]:
    source = [(ts, frame) for ts, frame in buffer if start <= ts <= end]
    if not source:
        return []
    count = max(1, round((end - start) * fps))
    targets = [start + index / fps for index in range(count)]
    result = []
    cursor = 0
    for target in targets:
        while cursor + 1 < len(source) and abs(source[cursor + 1][0] - target) <= abs(source[cursor][0] - target):
            cursor += 1
        result.append(source[cursor][1])
    return result


class RealSenseMonitorClient:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.token = args.token or os.getenv("VISUAL_MONITOR_TOKEN", "")
        if not self.token:
            raise RuntimeError("请通过 --token 或 VISUAL_MONITOR_TOKEN 配置客户端令牌")
        self.http = httpx.Client(
            base_url=args.server.rstrip("/"),
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=args.http_timeout,
            verify=not args.insecure,
            trust_env=not args.no_proxy,
        )
        self.np, self.rs, self.Image = require_camera_modules()

    def open_camera(self):
        pipeline = self.rs.pipeline()
        config = self.rs.config()
        if self.args.serial:
            config.enable_device(self.args.serial)
        config.enable_stream(self.rs.stream.color, 640, 480, self.rs.format.rgb8, 30)
        profile = pipeline.start(config)
        sensor = profile.get_device().first_color_sensor()
        return pipeline, profile.get_device().get_info(self.rs.camera_info.serial_number), sensor

    @staticmethod
    def warm_up(pipeline, frame_count: int = 30) -> None:
        """Let auto-exposure settle before recording a baseline or probe."""
        for _ in range(frame_count):
            pipeline.wait_for_frames(3000)

    def capture_probe(self) -> dict[str, Any]:
        pipeline, serial, _ = self.open_camera()
        self.warm_up(pipeline)
        started = time.perf_counter()
        frames = []
        try:
            deadline = time.perf_counter() + self.args.window_seconds
            while time.perf_counter() < deadline:
                color = pipeline.wait_for_frames(3000).get_color_frame()
                if color:
                    frames.append((time.time(), self.np.asanyarray(color.get_data()).copy()))
        finally:
            pipeline.stop()
        capture_ms = (time.perf_counter() - started) * 1000
        selected = sample_window(deque(frames), frames[0][0], frames[-1][0], self.args.video_fps)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            video_path = Path(tmp.name)
        try:
            encode_started = time.perf_counter()
            encode_mp4(selected, video_path, self.args.video_fps)
            encode_ms = (time.perf_counter() - encode_started) * 1000
            upload_started = time.perf_counter()
            with video_path.open("rb") as handle:
                response = self.http.post(
                    "/api/visual-monitor/upload-probe",
                    data={"camera_id": serial, "capture_ms": capture_ms, "encode_ms": encode_ms},
                    files={"video": ("window.mp4", handle, "video/mp4")},
                )
            upload_ms = (time.perf_counter() - upload_started) * 1000
            response.raise_for_status()
            result = response.json()
            result["client_upload_roundtrip_ms"] = round(upload_ms, 1)
            result["frame_count_captured"] = len(frames)
            result["frame_count_uploaded"] = len(selected)
            result["local_file_bytes"] = video_path.stat().st_size
            return result
        finally:
            video_path.unlink(missing_ok=True)

    def claim(self, camera_id: str) -> dict[str, Any] | None:
        response = self.http.post("/api/visual-monitor/claim", json={"camera_id": camera_id})
        response.raise_for_status()
        return response.json().get("assignment")

    def upload_baseline(self, assignment: dict[str, Any], camera_id: str, frame: Any, captured_at: float) -> None:
        image = self.Image.fromarray(frame)
        payload = io.BytesIO()
        image.save(payload, format="JPEG", quality=82, optimize=True)
        response = self.http.post(
            "/api/visual-monitor/baseline",
            data={
                "execution_id": assignment["execution_id"], "attempt_id": assignment["attempt_id"],
                "camera_id": camera_id, "captured_at": utc_iso(captured_at),
            },
            files={"image": ("before.jpg", payload.getvalue(), "image/jpeg")},
        )
        response.raise_for_status()

    def upload_checkpoint(
        self, assignment: dict[str, Any], camera_id: str, sequence: int,
        frames: list[Any], window_start: float, window_end: float,
    ) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            video_path = Path(tmp.name)
        try:
            encode_started = time.perf_counter()
            encode_mp4(frames, video_path, self.args.video_fps)
            encode_ms = (time.perf_counter() - encode_started) * 1000
            upload_started = time.perf_counter()
            with video_path.open("rb") as handle:
                response = self.http.post(
                    "/api/visual-monitor/checkpoints",
                    data={
                        "execution_id": assignment["execution_id"], "attempt_id": assignment["attempt_id"],
                        "camera_id": camera_id, "sequence": sequence,
                        "window_started_at": utc_iso(window_start), "window_ended_at": utc_iso(window_end),
                        "capture_ms": (window_end - window_start) * 1000, "encode_ms": encode_ms,
                    },
                    files={"video": (f"window-{sequence:04d}.mp4", handle, "video/mp4")},
                )
            upload_ms = (time.perf_counter() - upload_started) * 1000
            if response.status_code == 429:
                return {"accepted": False, "sequence": sequence, "reason": response.json().get("detail"), "upload_roundtrip_ms": round(upload_ms, 1)}
            if response.status_code == 409:
                return {"accepted": False, "stale": True, "sequence": sequence, "reason": response.json().get("detail"), "upload_roundtrip_ms": round(upload_ms, 1)}
            response.raise_for_status()
            return {**response.json(), "upload_roundtrip_ms": round(upload_ms, 1), "encoded_bytes": video_path.stat().st_size, "frames": len(frames)}
        finally:
            video_path.unlink(missing_ok=True)

    def run(self) -> None:
        pipeline, camera_id, _ = self.open_camera()
        self.warm_up(pipeline)
        ring: deque = deque()
        assignment = None
        baseline_sent = False
        next_checkpoint = None
        sequence = 0
        workers = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        pending: set[concurrent.futures.Future] = set()
        print(json.dumps({"event": "camera.ready", "camera_id": camera_id}, ensure_ascii=False))
        try:
            while True:
                color = pipeline.wait_for_frames(3000).get_color_frame()
                if not color:
                    continue
                now = time.time()
                frame = self.np.asanyarray(color.get_data()).copy()
                ring.append((now, frame))
                while ring and ring[0][0] < now - self.args.window_seconds - 1:
                    ring.popleft()
                for future in list(pending):
                    if future.done():
                        pending.remove(future)
                        try:
                            result = future.result()
                            print(json.dumps({"event": "checkpoint.uploaded", **result}, ensure_ascii=False))
                            if result.get("stale"):
                                assignment = None
                                baseline_sent = False
                                next_checkpoint = None
                                sequence = 0
                        except Exception as exc:
                            print(json.dumps({"event": "checkpoint.error", "error": str(exc)}, ensure_ascii=False))
                if assignment is None:
                    assignment = self.claim(camera_id)
                    if assignment:
                        print(json.dumps({"event": "assignment.claimed", **assignment}, ensure_ascii=False))
                    else:
                        time.sleep(0.5)
                        continue
                if not baseline_sent:
                    self.upload_baseline(assignment, camera_id, frame, now)
                    baseline_sent = True
                    next_checkpoint = now + self.args.window_seconds
                    print(json.dumps({"event": "baseline.uploaded", "captured_at": utc_iso(now)}, ensure_ascii=False))
                if now >= next_checkpoint:
                    sequence += 1
                    window_start = now - self.args.window_seconds
                    selected = sample_window(ring, window_start, now, self.args.video_fps)
                    if len(pending) < 2 and selected:
                        pending.add(workers.submit(
                            self.upload_checkpoint, assignment, camera_id, sequence,
                            selected, window_start, now,
                        ))
                    else:
                        print(json.dumps({"event": "checkpoint.skipped", "sequence": sequence, "reason": "local upload slots busy"}, ensure_ascii=False))
                    next_checkpoint += self.args.cycle_seconds
        except KeyboardInterrupt:
            print(json.dumps({"event": "client.stopped"}, ensure_ascii=False))
        finally:
            pipeline.stop()
            workers.shutdown(wait=True, cancel_futures=True)
            self.http.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="RealSense visual monitor client")
    result.add_argument("command", choices=["probe", "run"])
    result.add_argument("--server", default="https://112.74.61.202")
    result.add_argument("--token")
    result.add_argument("--serial")
    result.add_argument("--window-seconds", type=float, default=7.0)
    result.add_argument("--cycle-seconds", type=float, default=7.0)
    result.add_argument("--video-fps", type=int, default=6)
    result.add_argument("--http-timeout", type=float, default=30.0)
    result.add_argument("--no-proxy", action="store_true", help="不读取 HTTP_PROXY/HTTPS_PROXY")
    result.add_argument("--insecure", action="store_true", help="仅用于自签名证书测试")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.window_seconds <= 0 or args.cycle_seconds <= 0 or args.video_fps <= 0:
        raise SystemExit("窗口、周期和视频 FPS 必须大于 0")
    client = RealSenseMonitorClient(args)
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
