import unittest
from collections import deque
import struct

import numpy as np
from PIL import Image

from monitor.realsense_client import LivePreviewSender, assignment_identity, parser, sample_window, uploads_paused


class RealSenseClientTests(unittest.TestCase):
    def test_default_window_and_submission_cycle_are_both_seven_seconds(self):
        args = parser().parse_args(["probe"])
        self.assertEqual(args.window_seconds, 7.0)
        self.assertEqual(args.cycle_seconds, 7.0)
        self.assertEqual(args.assignment_poll_seconds, 1.0)
        self.assertEqual(args.preview_fps, 3.0)
        self.assertEqual((args.preview_width, args.preview_height), (640, 480))
        self.assertEqual(args.preview_quality, 65)

    def test_assignment_identity_isolated_by_attempt(self):
        first = {"execution_id": "execution-1", "attempt_id": "attempt-1"}
        second = {"execution_id": "execution-1", "attempt_id": "attempt-2"}
        self.assertNotEqual(assignment_identity(first), assignment_identity(second))
        self.assertIsNone(assignment_identity(None))

    def test_monitor_epoch_forces_fresh_baseline_generation(self):
        first = {"execution_id": "execution-1", "attempt_id": "attempt-1", "monitor_epoch": 1}
        resumed = {"execution_id": "execution-1", "attempt_id": "attempt-1", "monitor_epoch": 2}
        self.assertNotEqual(assignment_identity(first), assignment_identity(resumed))

    def test_terminal_observation_pauses_uploads_only_while_awaiting_confirmation(self):
        self.assertTrue(uploads_paused({"monitor_state": "awaiting_confirmation"}))
        self.assertFalse(uploads_paused({"monitor_state": "inferencing"}))
        self.assertFalse(uploads_paused({"monitor_state": "observing"}))

    def test_preview_packet_is_binary_jpeg_with_timestamp_and_sequence(self):
        args = parser().parse_args(["run", "--server", "https://example.com"])
        sender = LivePreviewSender(args, "secret", "camera:one", Image)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
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

    def test_seven_second_window_is_downsampled_to_six_fps(self):
        frames = deque(
            (index / 30, index)
            for index in range(211)
        )
        selected = sample_window(frames, 0.0, 7.0, fps=6)
        self.assertEqual(len(selected), 42)

    def test_empty_window_returns_no_frames(self):
        self.assertEqual(sample_window(deque(), 0, 7, fps=6), [])
