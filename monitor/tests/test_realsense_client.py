import unittest
from collections import deque

from monitor.realsense_client import parser, sample_window


class RealSenseClientTests(unittest.TestCase):
    def test_default_window_and_submission_cycle_are_both_seven_seconds(self):
        args = parser().parse_args(["probe"])
        self.assertEqual(args.window_seconds, 7.0)
        self.assertEqual(args.cycle_seconds, 7.0)

    def test_seven_second_window_is_downsampled_to_six_fps(self):
        frames = deque(
            (index / 30, index)
            for index in range(211)
        )
        selected = sample_window(frames, 0.0, 7.0, fps=6)
        self.assertEqual(len(selected), 42)

    def test_empty_window_returns_no_frames(self):
        self.assertEqual(sample_window(deque(), 0, 7, fps=6), [])
