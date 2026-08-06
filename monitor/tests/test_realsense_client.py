import unittest
from collections import deque

from monitor.realsense_client import sample_window


class RealSenseClientTests(unittest.TestCase):
    def test_seven_second_window_is_downsampled_to_six_fps(self):
        frames = deque(
            (index / 30, index)
            for index in range(211)
        )
        selected = sample_window(frames, 0.0, 7.0, fps=6)
        self.assertEqual(len(selected), 42)

    def test_empty_window_returns_no_frames(self):
        self.assertEqual(sample_window(deque(), 0, 7, fps=6), [])
