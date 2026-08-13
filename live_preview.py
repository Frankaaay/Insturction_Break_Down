# -*- coding: utf-8 -*-
"""In-memory latest-frame relay for low-latency camera previews."""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass


PREVIEW_HEADER = struct.Struct("!dI")
MAX_PREVIEW_JPEG_BYTES = 200 * 1024


@dataclass(frozen=True)
class PreviewFrame:
    camera_id: str
    captured_at: float
    sequence: int
    packet: bytes


def pack_preview_frame(captured_at: float, sequence: int, jpeg: bytes) -> bytes:
    if not jpeg or len(jpeg) > MAX_PREVIEW_JPEG_BYTES:
        raise ValueError("实时预览 JPEG 大小必须在 1 到 200 KB 之间")
    if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
        raise ValueError("实时预览帧不是合法 JPEG")
    if captured_at <= 0 or not 0 <= sequence <= 0xFFFFFFFF:
        raise ValueError("实时预览帧元数据非法")
    return PREVIEW_HEADER.pack(captured_at, sequence) + jpeg


def unpack_preview_frame(camera_id: str, packet: bytes) -> PreviewFrame:
    if len(packet) <= PREVIEW_HEADER.size:
        raise ValueError("实时预览数据包过短")
    captured_at, sequence = PREVIEW_HEADER.unpack_from(packet)
    jpeg = packet[PREVIEW_HEADER.size:]
    # Reuse all JPEG/size validation in one place.
    pack_preview_frame(captured_at, sequence, jpeg)
    return PreviewFrame(camera_id, captured_at, sequence, packet)


class LivePreviewHub:
    """Fan out only the newest frame; slow viewers never create backlog."""

    def __init__(self) -> None:
        self._latest: dict[str, PreviewFrame] = {}
        self._subscribers: dict[str, set[asyncio.Queue[bytes]]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, camera_id: str, packet: bytes) -> PreviewFrame:
        frame = unpack_preview_frame(camera_id, packet)
        async with self._lock:
            self._latest[camera_id] = frame
            queues = list(self._subscribers.get(camera_id, ()))
            for queue in queues:
                if queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(packet)
        return frame

    async def subscribe(self, camera_id: str) -> tuple[asyncio.Queue[bytes], bytes | None]:
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._subscribers.setdefault(camera_id, set()).add(queue)
            latest = self._latest.get(camera_id)
        return queue, latest.packet if latest else None

    async def unsubscribe(self, camera_id: str, queue: asyncio.Queue[bytes]) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(camera_id)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(camera_id, None)

    async def viewer_count(self, camera_id: str) -> int:
        async with self._lock:
            return len(self._subscribers.get(camera_id, ()))
