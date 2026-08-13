import unittest
from collections import deque
import struct

from monitor.camera_monitor_client import (
    LivePreviewSender, MonitorClient, assignment_identity,
    checkpoint_block_reason, parser, sample_fixed_window, sample_window,
    uploads_paused,
)
from monitor.ros2_camera_client import (
    DEFAULT_CAMERA_ID, DEFAULT_TOPIC, parser as ros2_parser, validate_ros2_args,
)


class FakePreviewImage:
    size = (640, 352)

    def save(self, payload, **kwargs):
        payload.write(b"\xff\xd8preview-jpeg\xff\xd9")


class FakeImageModule:
    class Resampling:
        BILINEAR = 1

    @staticmethod
    def fromarray(frame):
        return FakePreviewImage()


class FakeHttpResponse:
    status_code = 200

    def raise_for_status(self):
        return None


class FakeHttpClient:
    def __init__(self):
        self.calls = []

    def post(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return FakeHttpResponse()


class CameraMonitorClientTests(unittest.TestCase):
    def test_default_window_and_submission_cycle_are_both_six_seconds(self):
        args = parser().parse_args(["probe"])
        self.assertEqual(args.window_seconds, 6.0)
        self.assertEqual(args.cycle_seconds, 6.0)
        self.assertEqual(args.assignment_poll_seconds, 1.0)
        self.assertEqual(args.preview_fps, 10.0)
        self.assertEqual((args.preview_width, args.preview_height), (640, 352))
        self.assertEqual(args.preview_quality, 65)
        self.assertEqual(args.server, "http://127.0.0.1:8000")

    def test_preview_fps_parser_accepts_ten(self):
        self.assertEqual(parser().parse_args(["run", "--preview-fps", "10"]).preview_fps, 10.0)

    def test_ros2_defaults_use_native_head_left_camera(self):
        args = ros2_parser().parse_args(["run"])
        validate_ros2_args(args)
        self.assertEqual(args.topic, DEFAULT_TOPIC)
        self.assertEqual(args.camera_id, DEFAULT_CAMERA_ID)
        self.assertEqual((args.preview_width, args.preview_height), (640, 352))

    def test_assignment_identity_isolated_by_attempt(self):
        first = {"execution_id": "execution-1", "attempt_id": "attempt-1"}
        second = {"execution_id": "execution-1", "attempt_id": "attempt-2"}
        self.assertNotEqual(assignment_identity(first), assignment_identity(second))
        self.assertIsNone(assignment_identity(None))

    def test_monitor_epoch_forces_fresh_baseline_generation(self):
        first = {"execution_id": "execution-1", "attempt_id": "attempt-1", "monitor_epoch": 1}
        resumed = {"execution_id": "execution-1", "attempt_id": "attempt-1", "monitor_epoch": 2}
        self.assertNotEqual(assignment_identity(first), assignment_identity(resumed))

    def test_chain_step_progress_does_not_reset_capture_generation(self):
        first = {"execution_id": "execution-1", "attempt_id": "chain-session", "monitor_epoch": 1, "current_step_index": 0}
        advanced = {"execution_id": "execution-1", "attempt_id": "chain-session", "monitor_epoch": 1, "current_step_index": 2}
        self.assertEqual(assignment_identity(first), assignment_identity(advanced))

    def test_terminal_observation_pauses_uploads_only_while_awaiting_confirmation(self):
        self.assertTrue(uploads_paused({"monitor_state": "awaiting_confirmation"}))
        self.assertFalse(uploads_paused({"monitor_state": "inferencing"}))
        self.assertFalse(uploads_paused({"monitor_state": "observing"}))

    def test_inference_or_busy_upload_defers_without_consuming_a_cycle(self):
        self.assertEqual(
            checkpoint_block_reason({"monitor_state": "inferencing"}, 0),
            "server inference still running",
        )
        self.assertEqual(
            checkpoint_block_reason({"monitor_state": "observing"}, 2),
            "local upload slots busy",
        )
        self.assertIsNone(checkpoint_block_reason({"monitor_state": "observing"}, 0))

    def test_preview_packet_is_binary_jpeg_with_timestamp_and_sequence(self):
        args = parser().parse_args(["run", "--server", "https://example.com"])
        sender = LivePreviewSender(args, "secret", "camera:one", FakeImageModule)
        frame = object()
        packet = sender._encode_packet(7, 1234.5, frame)
        captured_at, sequence = struct.unpack("!dI", packet[:12])
        self.assertEqual(captured_at, 1234.5)
        self.assertEqual(sequence, 7)
        self.assertTrue(packet[12:].startswith(b"\xff\xd8"))
        self.assertTrue(packet[12:].endswith(b"\xff\xd9"))
        self.assertEqual(
            sender._websocket_url(),
            "wss://example.com/api/visual-monitor/live/ingest/camera%3Aone",
        )

    def test_pipeline_phase_telemetry_contains_window_timestamps(self):
        client = MonitorClient.__new__(MonitorClient)
        client.http = FakeHttpClient()
        client.report_phase(
            {"execution_id": "execution-1", "attempt_id": "chain-session"},
            "camera-1", 2, "uploading", 100.0,
            window_started_at=94.0, window_ended_at=100.0,
        )
        path, kwargs = client.http.calls[0]
        self.assertEqual(path, "/api/visual-monitor/telemetry")
        self.assertEqual(kwargs["json"]["phase"], "uploading")
        self.assertEqual(kwargs["json"]["sequence"], 2)
        self.assertTrue(kwargs["json"]["window_started_at"].endswith("Z"))

    def test_six_second_window_is_downsampled_to_six_fps(self):
        frames = deque(
            (index / 30, index)
            for index in range(181)
        )
        selected = sample_window(frames, 0.0, 6.0, fps=6)
        self.assertEqual(len(selected), 36)

    def test_probe_window_is_full_length_after_discovery_delay(self):
        frames = deque(
            (100.2 + index / 8, index)
            for index in range(48)
        )
        selected = sample_fixed_window(frames, 100.0, 6.0, fps=6)
        self.assertEqual(len(selected), 36)

    def test_empty_window_returns_no_frames(self):
        self.assertEqual(sample_window(deque(), 0, 6, fps=6), [])
