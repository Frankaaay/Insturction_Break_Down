# -*- coding: utf-8 -*-
"""Camera-independent upload, preview, and assignment client for Planner Monitor."""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import ssl
import struct
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from dotenv import load_dotenv


def utc_iso(timestamp: float | None = None) -> str:
    value = datetime.fromtimestamp(timestamp or time.time(), timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require_common_modules():
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "缺少相机客户端依赖，请运行: pip install -r monitor/requirements-ros2-camera.txt"
        ) from exc
    return np, Image


class FrameSource(Protocol):
    """Minimal boundary between a camera transport and the Monitor pipeline."""

    camera_id: str

    def start(self) -> None: ...

    def read(self, timeout_seconds: float) -> tuple[float, Any] | None: ...

    def stop(self) -> None: ...


def encode_mp4(
    frames: list[Any], output: Path, fps: int = 6, ffmpeg: str = "ffmpeg",
) -> None:
    if not frames:
        raise RuntimeError("没有可编码的视频帧")
    height, width = frames[0].shape[:2]
    if width % 2 or height % 2:
        raise RuntimeError(f"H.264 输入尺寸必须为偶数，当前为 {width}x{height}")
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s:v", f"{width}x{height}",
        "-r", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(output),
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for frame in frames:
            if frame.shape[:2] != (height, width):
                raise RuntimeError("视频窗口中出现了不同尺寸的帧")
            assert process.stdin is not None
            process.stdin.write(frame.tobytes())
        assert process.stdin is not None
        process.stdin.close()
        stderr = process.stderr.read() if process.stderr is not None else b""
        return_code = process.wait(timeout=30)
    except Exception:
        process.kill()
        process.wait(timeout=5)
        raise
    if return_code:
        raise RuntimeError(f"FFmpeg 编码失败: {stderr.decode('utf-8', 'replace').strip()}")


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


def assignment_identity(assignment: dict[str, Any] | None) -> tuple[str, str, int] | None:
    if not assignment:
        return None
    return (
        str(assignment["execution_id"]),
        str(assignment["attempt_id"]),
        int(assignment.get("monitor_epoch", 1)),
    )


def uploads_paused(assignment: dict[str, Any] | None) -> bool:
    return bool(assignment and assignment.get("monitor_state") == "awaiting_confirmation")


def checkpoint_block_reason(assignment: dict[str, Any], pending_count: int) -> str | None:
    if assignment.get("monitor_state") == "inferencing":
        return "server inference still running"
    if pending_count >= 2:
        return "local upload slots busy"
    return None


class LivePreviewSender:
    """Encode and upload only the newest preview frame on an isolated thread."""

    _header = struct.Struct("!dI")

    def __init__(self, args: argparse.Namespace, token: str, camera_id: str, image_module: Any) -> None:
        self.args = args
        self.token = token
        self.camera_id = camera_id
        self.Image = image_module
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._latest: tuple[int, float, Any] | None = None
        self._sequence = 0
        self._thread = threading.Thread(target=self._run, name="live-preview", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def offer(self, frame: Any, captured_at: float) -> None:
        with self._condition:
            self._sequence = (self._sequence + 1) & 0xFFFFFFFF
            self._latest = (self._sequence, captured_at, frame)
            self._condition.notify()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout=max(3.0, self.args.http_timeout + 1.0))

    def _websocket_url(self) -> str:
        parsed = urlsplit(self.args.server.rstrip("/"))
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/api/visual-monitor/live/ingest/{quote(self.camera_id, safe='')}"

    def _encode_packet(self, sequence: int, captured_at: float, frame: Any) -> bytes:
        image = self.Image.fromarray(frame)
        if image.size != (self.args.preview_width, self.args.preview_height):
            image = image.resize(
                (self.args.preview_width, self.args.preview_height),
                self.Image.Resampling.BILINEAR,
            )
        payload = io.BytesIO()
        image.save(payload, format="JPEG", quality=self.args.preview_quality, optimize=False)
        jpeg = payload.getvalue()
        if len(jpeg) > 200 * 1024:
            raise RuntimeError(f"实时预览帧超过 200 KB: {len(jpeg)}")
        return self._header.pack(captured_at, sequence) + jpeg

    def _run(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            print(json.dumps({"event": "preview.disabled", "error": "缺少 websockets 依赖"}, ensure_ascii=False))
            return
        ssl_context = None
        if self.args.insecure and self._websocket_url().startswith("wss://"):
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        last_sent = -1
        while not self._stop.is_set():
            try:
                kwargs: dict[str, Any] = {
                    "open_timeout": self.args.http_timeout,
                    "close_timeout": 2,
                    "max_size": 256 * 1024,
                    "compression": None,
                }
                if ssl_context is not None:
                    kwargs["ssl"] = ssl_context
                # websockets 15 supports explicit proxy bypass; older versions connect
                # directly and don't expose this argument.
                if self.args.no_proxy:
                    import inspect
                    if "proxy" in inspect.signature(connect).parameters:
                        kwargs["proxy"] = None
                with connect(self._websocket_url(), **kwargs) as websocket:
                    websocket.send(json.dumps({"token": self.token}))
                    ready = json.loads(websocket.recv(timeout=5))
                    if ready.get("type") != "ready":
                        raise RuntimeError("实时预览服务认证失败")
                    print(json.dumps({"event": "preview.connected", "camera_id": self.camera_id}, ensure_ascii=False))
                    while not self._stop.is_set():
                        with self._condition:
                            while (
                                not self._stop.is_set()
                                and (self._latest is None or self._latest[0] == last_sent)
                            ):
                                self._condition.wait(timeout=1.0)
                            if self._stop.is_set():
                                break
                            sequence, captured_at, frame = self._latest
                        websocket.send(self._encode_packet(sequence, captured_at, frame))
                        last_sent = sequence
            except Exception as exc:
                if not self._stop.is_set():
                    print(json.dumps({"event": "preview.reconnecting", "error": str(exc)}, ensure_ascii=False))
                    self._stop.wait(1.0)


class AssignmentPoller:
    """Poll assignments independently so camera capture never waits on the server."""

    def __init__(self, args: argparse.Namespace, token: str, camera_id: str) -> None:
        self.args = args
        self.token = token
        self.camera_id = camera_id
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._assignment: dict[str, Any] | None = None
        self._generation = 0
        self._thread = threading.Thread(target=self._run, name="assignment-poller", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(2.0, self.args.http_timeout + 1.0))

    def snapshot(self) -> tuple[int, dict[str, Any] | None]:
        with self._lock:
            return self._generation, self._assignment

    def _run(self) -> None:
        with httpx.Client(
            base_url=self.args.server.rstrip("/"),
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.args.http_timeout,
            verify=not self.args.insecure,
            trust_env=not self.args.no_proxy,
        ) as client:
            while not self._stop.is_set():
                try:
                    response = client.post("/api/visual-monitor/claim", json={"camera_id": self.camera_id})
                    response.raise_for_status()
                    assignment = response.json().get("assignment")
                    with self._lock:
                        if assignment_identity(assignment) != assignment_identity(self._assignment):
                            self._generation += 1
                            self._assignment = assignment
                        elif assignment is not None:
                            self._assignment = assignment
                except Exception as exc:
                    print(json.dumps({"event": "assignment.poll_error", "error": str(exc)}, ensure_ascii=False))
                self._stop.wait(self.args.assignment_poll_seconds)


class MonitorClient:
    """Camera-independent Monitor state machine and server transport."""

    def __init__(self, args: argparse.Namespace, source: FrameSource) -> None:
        self.args = args
        self.source = source
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
        self.np, self.Image = require_common_modules()

    def capture_probe(self) -> dict[str, Any]:
        self.source.start()
        discovery_started = time.perf_counter()
        first = self.source.read(self.args.http_timeout)
        discovery_ms = (time.perf_counter() - discovery_started) * 1000
        if first is None:
            self.source.stop()
            raise RuntimeError("ROS2 相机在等待首帧期间没有提供画面")
        # The first frame proves DDS discovery is complete but is intentionally
        # excluded. The six-second probe window starts from the next frame.
        window_started_at = time.time()
        started = time.perf_counter()
        capture_finished = started
        stop_ms = 0.0
        frames = []
        try:
            deadline = time.perf_counter() + self.args.window_seconds
            while time.perf_counter() < deadline:
                sample = self.source.read(min(1.0, max(0.05, deadline - time.perf_counter())))
                if sample:
                    frames.append(sample)
        finally:
            capture_finished = time.perf_counter()
            stop_started = time.perf_counter()
            self.source.stop()
            stop_ms = (time.perf_counter() - stop_started) * 1000
        if not frames:
            raise RuntimeError("ROS2 相机在 probe 期间没有提供任何画面")
        capture_ms = (capture_finished - started) * 1000
        selected = sample_fixed_window(
            deque(frames), window_started_at, self.args.window_seconds, self.args.video_fps,
        )
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            video_path = Path(tmp.name)
        try:
            encode_started = time.perf_counter()
            encode_mp4(selected, video_path, self.args.video_fps, self.args.ffmpeg)
            encode_ms = (time.perf_counter() - encode_started) * 1000
            upload_started = time.perf_counter()
            with video_path.open("rb") as handle:
                response = self.http.post(
                    "/api/visual-monitor/upload-probe",
                    data={
                        "camera_id": self.source.camera_id,
                        "capture_ms": capture_ms,
                        "encode_ms": encode_ms,
                    },
                    files={"video": ("window.mp4", handle, "video/mp4")},
                )
            upload_ms = (time.perf_counter() - upload_started) * 1000
            response.raise_for_status()
            result = response.json()
            result["client_upload_roundtrip_ms"] = round(upload_ms, 1)
            result["frame_count_captured"] = len(frames)
            result["frame_count_uploaded"] = len(selected)
            result["local_file_bytes"] = video_path.stat().st_size
            result["source_stop_ms"] = round(stop_ms, 1)
            result["source_discovery_ms"] = round(discovery_ms, 1)
            result["camera_id"] = self.source.camera_id
            result["source_resolution"] = [int(frames[0][1].shape[1]), int(frames[0][1].shape[0])]
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

    def report_phase(
        self, assignment: dict[str, Any], camera_id: str, sequence: int,
        phase: str, phase_started_at: float, *,
        window_started_at: float | None = None,
        window_ended_at: float | None = None,
    ) -> None:
        payload = {
            "execution_id": assignment["execution_id"],
            "attempt_id": assignment["attempt_id"],
            "camera_id": camera_id,
            "sequence": sequence,
            "phase": phase,
            "phase_started_at": utc_iso(phase_started_at),
            "window_started_at": utc_iso(window_started_at) if window_started_at is not None else None,
            "window_ended_at": utc_iso(window_ended_at) if window_ended_at is not None else None,
        }
        try:
            response = self.http.post("/api/visual-monitor/telemetry", json=payload)
            if response.status_code not in {200, 409}:
                response.raise_for_status()
        except Exception as exc:
            print(json.dumps({
                "event": "pipeline.telemetry_error", "phase": phase,
                "sequence": sequence, "error": str(exc),
            }, ensure_ascii=False))

    def upload_checkpoint(
        self, assignment: dict[str, Any], camera_id: str, sequence: int, generation: int,
        frames: list[Any], now_frame: Any, window_start: float, window_end: float,
    ) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            video_path = Path(tmp.name)
        try:
            encode_started_wall = time.time()
            self.report_phase(
                assignment, camera_id, sequence, "encoding", encode_started_wall,
                window_started_at=window_start, window_ended_at=window_end,
            )
            encode_started = time.perf_counter()
            encode_mp4(frames, video_path, self.args.video_fps, self.args.ffmpeg)
            now_payload = io.BytesIO()
            self.Image.fromarray(now_frame).save(
                now_payload, format="JPEG", quality=82, optimize=True,
            )
            encode_ms = (time.perf_counter() - encode_started) * 1000
            upload_started_wall = time.time()
            self.report_phase(
                assignment, camera_id, sequence, "uploading", upload_started_wall,
                window_started_at=window_start, window_ended_at=window_end,
            )
            upload_started = time.perf_counter()
            with video_path.open("rb") as handle:
                response = self.http.post(
                    "/api/visual-monitor/checkpoints",
                    data={
                        "execution_id": assignment["execution_id"], "attempt_id": assignment["attempt_id"],
                        "camera_id": camera_id, "sequence": sequence,
                        "window_started_at": utc_iso(window_start), "window_ended_at": utc_iso(window_end),
                        "capture_ms": (window_end - window_start) * 1000, "encode_ms": encode_ms,
                        "upload_started_at": utc_iso(upload_started_wall),
                    },
                    files={
                        "video": (f"window-{sequence:04d}.mp4", handle, "video/mp4"),
                        "now_image": (f"now-{sequence:04d}.jpg", now_payload.getvalue(), "image/jpeg"),
                    },
                )
            upload_ms = (time.perf_counter() - upload_started) * 1000
            if response.status_code == 429:
                return {"accepted": False, "generation": generation, "sequence": sequence, "reason": response.json().get("detail"), "upload_roundtrip_ms": round(upload_ms, 1)}
            if response.status_code == 409:
                return {"accepted": False, "stale": True, "generation": generation, "sequence": sequence, "reason": response.json().get("detail"), "upload_roundtrip_ms": round(upload_ms, 1)}
            response.raise_for_status()
            return {**response.json(), "generation": generation, "upload_roundtrip_ms": round(upload_ms, 1), "encoded_bytes": video_path.stat().st_size, "frames": len(frames)}
        finally:
            video_path.unlink(missing_ok=True)

    def run(self) -> None:
        self.source.start()
        camera_id = self.source.camera_id
        ring: deque = deque()
        assignment = None
        generation = 0
        baseline_sent = False
        next_checkpoint = None
        sequence = 0
        pause_announced = False
        workers = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        pending: set[concurrent.futures.Future] = set()
        poller = AssignmentPoller(self.args, self.token, camera_id)
        poller.start()
        preview = LivePreviewSender(self.args, self.token, camera_id, self.Image)
        preview.start()
        next_preview = 0.0
        print(json.dumps({"event": "camera.ready", "camera_id": camera_id}, ensure_ascii=False))
        try:
            while True:
                sample = self.source.read(3.0)
                if sample is None:
                    print(json.dumps({"event": "camera.frame_timeout", "camera_id": camera_id}, ensure_ascii=False))
                    continue
                now, frame = sample
                if now >= next_preview:
                    preview.offer(frame, now)
                    next_preview = now + 1.0 / self.args.preview_fps
                polled_generation, polled_assignment = poller.snapshot()
                if polled_generation != generation:
                    generation = polled_generation
                    assignment = polled_assignment
                    baseline_sent = False
                    next_checkpoint = None
                    sequence = 0
                    pause_announced = False
                    ring.clear()
                    if assignment:
                        print(json.dumps({"event": "assignment.claimed", "generation": generation, **assignment}, ensure_ascii=False))
                    else:
                        print(json.dumps({"event": "assignment.cleared", "generation": generation}, ensure_ascii=False))
                else:
                    # State changes (inferencing/awaiting_confirmation) do not change
                    # assignment identity, but must still take effect within one poll.
                    assignment = polled_assignment
                if uploads_paused(assignment):
                    ring.clear()
                    if not pause_announced:
                        print(json.dumps({
                            "event": "monitor.awaiting_confirmation",
                            "generation": generation,
                            "status": assignment.get("previous_status"),
                        }, ensure_ascii=False))
                        pause_announced = True
                    continue
                ring.append((now, frame))
                while ring and ring[0][0] < now - self.args.window_seconds - 1:
                    ring.popleft()
                for future in list(pending):
                    if future.done():
                        pending.remove(future)
                        try:
                            result = future.result()
                            if result.get("generation") != generation:
                                print(json.dumps({"event": "checkpoint.stale_ignored", **result}, ensure_ascii=False))
                            else:
                                print(json.dumps({"event": "checkpoint.uploaded", **result}, ensure_ascii=False))
                        except Exception as exc:
                            print(json.dumps({"event": "checkpoint.error", "error": str(exc)}, ensure_ascii=False))
                if assignment is None:
                    continue
                if not baseline_sent:
                    self.upload_baseline(assignment, camera_id, frame, now)
                    baseline_sent = True
                    next_checkpoint = now + self.args.window_seconds
                    self.report_phase(
                        assignment, camera_id, 1, "capturing", now,
                        window_started_at=now,
                        window_ended_at=next_checkpoint,
                    )
                    print(json.dumps({"event": "baseline.uploaded", "captured_at": utc_iso(now)}, ensure_ascii=False))
                if now >= next_checkpoint:
                    blocked = checkpoint_block_reason(assignment, len(pending))
                    if blocked:
                        print(json.dumps({"event": "checkpoint.deferred", "sequence": sequence + 1, "reason": blocked}, ensure_ascii=False))
                        # Keep the latest rolling window and retry promptly after the
                        # in-flight result/upload finishes; don't lose a full cycle.
                        next_checkpoint = now + 0.5
                        continue
                    window_start = now - self.args.window_seconds
                    selected = sample_window(ring, window_start, now, self.args.video_fps)
                    if selected:
                        sequence += 1
                        pending.add(workers.submit(
                            self.upload_checkpoint, assignment, camera_id, sequence,
                            generation,
                            selected, frame.copy(), window_start, now,
                        ))
                        next_checkpoint = now + self.args.cycle_seconds
                    else:
                        print(json.dumps({"event": "checkpoint.deferred", "sequence": sequence + 1, "reason": "window has no frames"}, ensure_ascii=False))
                        next_checkpoint = now + 0.5
        except KeyboardInterrupt:
            print(json.dumps({"event": "client.stopped"}, ensure_ascii=False))
        finally:
            preview.stop()
            poller.stop()
            self.source.stop()
            workers.shutdown(wait=True, cancel_futures=True)
            self.http.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="ROS2 camera visual monitor client")
    result.add_argument("command", choices=["probe", "run"])
    result.add_argument("--server", default="http://127.0.0.1:8000")
    result.add_argument("--token")
    result.add_argument("--window-seconds", type=float, default=6.0)
    result.add_argument("--cycle-seconds", type=float, default=6.0)
    result.add_argument("--assignment-poll-seconds", type=float, default=1.0)
    result.add_argument("--video-fps", type=int, default=6)
    result.add_argument("--preview-fps", type=float, default=10.0)
    result.add_argument("--preview-width", type=int, default=640)
    result.add_argument("--preview-height", type=int, default=352)
    result.add_argument("--preview-quality", type=int, default=65)
    result.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    result.add_argument("--http-timeout", type=float, default=30.0)
    result.add_argument("--no-proxy", action="store_true", help="不读取 HTTP_PROXY/HTTPS_PROXY")
    result.add_argument("--insecure", action="store_true", help="仅用于自签名证书测试")
    return result


def sample_fixed_window(
    buffer: deque, start: float, duration_seconds: float, fps: int = 6,
) -> list[Any]:
    """Return exactly duration*fps frames after camera discovery is complete."""
    selected = sample_window(buffer, start, start + duration_seconds, fps)
    expected = max(1, round(duration_seconds * fps))
    if selected and len(selected) != expected:
        raise RuntimeError(f"视频窗口应为 {expected} 帧，实际为 {len(selected)} 帧")
    return selected


def validate_args(args: argparse.Namespace) -> None:
    if (
        args.window_seconds <= 0 or args.cycle_seconds <= 0
        or args.assignment_poll_seconds <= 0 or args.video_fps <= 0
        or not 0 < args.preview_fps <= 10
        or args.preview_width <= 0 or args.preview_height <= 0
        or not 1 <= args.preview_quality <= 95
    ):
        raise SystemExit("窗口、周期和视频 FPS 必须大于 0，实时预览 FPS 不能超过 10")


def load_client_environment() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
