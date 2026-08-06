import tempfile
import unittest
from pathlib import Path

from realtime_monitor import VisualMonitorConfig, VisualMonitorService, build_pick_prompt, validate_result


class RealtimeMonitorContractTests(unittest.IsolatedAsyncioTestCase):
    def test_result_null_rules_and_pick_occlusion_prompt(self):
        valid = validate_result({
            "status": "failed",
            "description_zh": "拿起了纸巾包",
            "failure_reason": "拿起了旁边的纸巾包，水壶仍在桌面",
            "evidence": [{"timestamp_s": 2.0, "observation": "纸巾包离开桌面"}],
            "completion_evidence_timestamp_s": None,
        })
        self.assertEqual(valid["status"], "failed")
        with self.assertRaises(ValueError):
            validate_result({
                "status": "in_progress", "description_zh": "仍在执行",
                "failure_reason": "不应存在", "evidence": [{"timestamp_s": 1.0, "observation": "手靠近"}],
                "completion_evidence_timestamp_s": None,
            })
        prompt = build_pick_prompt({"slots": {"obj_a": "水壶"}}, 1)
        self.assertIn("底部—支撑面间隙", prompt)
        self.assertIn("unknown", prompt)

    async def test_concurrency_limit_rejects_third_job(self):
        updates = []

        async def update(**kwargs):
            updates.append(kwargs)
            return {}

        with tempfile.TemporaryDirectory() as directory:
            service = VisualMonitorService(update, VisualMonitorConfig(Path(directory), max_concurrency=2))

            async def slow(*args):
                import asyncio
                await asyncio.sleep(10)

            service._call_bailian = slow
            baseline = Path(directory) / "before.jpg"
            video = Path(directory) / "window.mp4"
            baseline.write_bytes(b"image")
            video.write_bytes(b"video")
            assignment = {"execution_id": "e", "attempt_id": "a", "slots": {"obj_a": "水壶"}}
            await service.submit(assignment=assignment, camera_id="c", sequence=1, baseline_path=baseline, video_path=video, client_timings={})
            await service.submit(assignment=assignment, camera_id="c", sequence=2, baseline_path=baseline, video_path=video, client_timings={})
            with self.assertRaises(RuntimeError):
                await service.submit(assignment=assignment, camera_id="c", sequence=3, baseline_path=baseline, video_path=video, client_timings={})
            await service.close()
