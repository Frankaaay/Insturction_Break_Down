import tempfile
import unittest
from pathlib import Path

from realtime_monitor import VisualMonitorConfig, VisualMonitorService, validate_result
from visual_contracts import build_monitor_prompt


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
        prompt = build_monitor_prompt({
            "action_id": "A_001", "logic": 0, "slots": {"obj_a": "水壶"}
        }, 1)
        self.assertIn("目标物底部", prompt)
        self.assertIn("unknown", prompt)

    def test_action_specific_contracts_share_common_output_rules(self):
        carry = build_monitor_prompt({
            "action_id": "A_003", "logic": 0, "slots": {"obj_a": "水壶"}
        }, 1)
        self.assertIn("宽松成功、极窄失败", carry)
        self.assertIn("不要求判断最终目标位置", carry)
        self.assertIn("仅在明确搬运了错误物体", carry)

        place = build_monitor_prompt({
            "action_id": "A_002", "logic": 1,
            "slots": {"obj_a": "水壶", "sur_a": "桌子"},
        }, 1)
        self.assertIn("指定表面承托", place)
        self.assertIn("手已经释放", place)
        self.assertIn("failure_reason 必须为 null", place)

        with self.assertRaises(ValueError):
            build_monitor_prompt({
                "action_id": "A_002", "logic": 0,
                "slots": {"obj_a": "水壶", "obj_b": "杯子"},
            }, 1)

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
            assignment = {"execution_id": "e", "attempt_id": "a", "action_id": "A_001", "logic": 0, "slots": {"obj_a": "水壶"}}
            await service.submit(assignment=assignment, camera_id="c", sequence=1, baseline_path=baseline, video_path=video, client_timings={})
            await service.submit(assignment=assignment, camera_id="c", sequence=2, baseline_path=baseline, video_path=video, client_timings={})
            with self.assertRaises(RuntimeError):
                await service.submit(assignment=assignment, camera_id="c", sequence=3, baseline_path=baseline, video_path=video, client_timings={})
            await service.close()
