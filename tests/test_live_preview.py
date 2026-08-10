import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from live_preview import LivePreviewHub, PREVIEW_HEADER, pack_preview_frame, unpack_preview_frame
import server


JPEG_A = b"\xff\xd8frame-a\xff\xd9"
JPEG_B = b"\xff\xd8frame-b\xff\xd9"
JPEG_C = b"\xff\xd8frame-c\xff\xd9"


class LivePreviewPacketTests(unittest.TestCase):
    def test_packet_round_trip_keeps_capture_time_sequence_and_jpeg(self):
        packet = pack_preview_frame(1_786_000_000.25, 17, JPEG_A)
        frame = unpack_preview_frame("camera-1", packet)
        self.assertEqual(frame.camera_id, "camera-1")
        self.assertEqual(frame.captured_at, 1_786_000_000.25)
        self.assertEqual(frame.sequence, 17)
        self.assertEqual(frame.packet[PREVIEW_HEADER.size:], JPEG_A)

    def test_invalid_or_oversized_jpeg_is_rejected(self):
        with self.assertRaises(ValueError):
            pack_preview_frame(1.0, 1, b"not-jpeg")
        with self.assertRaises(ValueError):
            pack_preview_frame(1.0, 1, b"\xff\xd8" + b"x" * (201 * 1024) + b"\xff\xd9")


class LivePreviewHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_viewer_receives_only_latest_frame(self):
        hub = LivePreviewHub()
        queue, latest = await hub.subscribe("camera-1")
        self.assertIsNone(latest)
        first = pack_preview_frame(1.0, 1, JPEG_A)
        second = pack_preview_frame(2.0, 2, JPEG_B)
        third = pack_preview_frame(3.0, 3, JPEG_C)
        await hub.publish("camera-1", first)
        await hub.publish("camera-1", second)
        await hub.publish("camera-1", third)
        self.assertEqual(queue.qsize(), 1)
        self.assertEqual(await queue.get(), third)
        await hub.unsubscribe("camera-1", queue)
        self.assertEqual(await hub.viewer_count("camera-1"), 0)

    async def test_new_viewer_immediately_gets_latest_frame(self):
        hub = LivePreviewHub()
        packet = pack_preview_frame(4.0, 4, JPEG_A)
        await hub.publish("camera-2", packet)
        queue, latest = await hub.subscribe("camera-2")
        self.assertEqual(latest, packet)
        self.assertEqual(queue.qsize(), 0)
        await hub.unsubscribe("camera-2", queue)


class LivePreviewWebSocketTests(unittest.TestCase):
    def setUp(self):
        self.previous_hub = server.live_preview_hub
        server.live_preview_hub = LivePreviewHub()
        self.environment = patch.dict("os.environ", {
            "VISUAL_MONITOR_TOKEN": "camera-secret",
            "OPERATOR_TOKEN": "operator-secret",
        })
        self.environment.start()
        self.client = TestClient(server.app)

    def tearDown(self):
        self.environment.stop()
        server.live_preview_hub = self.previous_hub

    def test_binary_frame_flows_from_authenticated_camera_to_viewer(self):
        packet = pack_preview_frame(1_786_000_001.0, 5, JPEG_A)
        with self.client.websocket_connect(
            "/api/visual-monitor/live/ingest/camera-1"
        ) as producer:
            producer.send_json({"token": "camera-secret"})
            self.assertEqual(producer.receive_json()["type"], "ready")
            with self.client.websocket_connect(
                "/api/visual-monitor/live/view/camera-1"
            ) as viewer:
                viewer.send_json({"token": "operator-secret"})
                self.assertEqual(viewer.receive_json()["type"], "ready")
                producer.send_bytes(packet)
                self.assertEqual(viewer.receive_bytes(), packet)

    def test_bad_viewer_token_is_closed(self):
        with self.client.websocket_connect(
            "/api/visual-monitor/live/view/camera-1"
        ) as viewer:
            viewer.send_json({"token": "wrong"})
            with self.assertRaises(WebSocketDisconnect) as context:
                viewer.receive_json()
            self.assertEqual(context.exception.code, 4401)
