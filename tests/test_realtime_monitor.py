import tempfile
import unittest
from pathlib import Path

from realtime_monitor import (
    VisualMonitorConfig, VisualMonitorService, validate_chain_result, validate_result,
)
from visual_contracts import build_chain_monitor_prompt, build_monitor_prompt


class RealtimeMonitorContractTests(unittest.IsolatedAsyncioTestCase):
    def test_chain_prompt_and_result_allow_fast_intermediate_steps(self):
        assignment = {
            "instruction": "拿起水壶并放到桌子上",
            "current_step_index": 0,
            "confirmed_steps": [],
            "unfinished_steps": [
                {"step_id": "pick", "status": "in_progress", "description_zh": "手正在靠近水壶"},
                {"step_id": "carry", "status": "unknown", "description_zh": None},
                {"step_id": "place", "status": "unknown", "description_zh": None},
            ],
            "steps": [
                {"step_id": "pick", "action_id": "A_001", "logic": 0, "slots": {"obj_a": "水壶"}, "zh": "拿起水壶"},
                {"step_id": "carry", "action_id": "A_003", "logic": 0, "slots": {"obj_a": "水壶"}, "zh": "搬运水壶"},
                {"step_id": "place", "action_id": "A_002", "logic": 1, "slots": {"obj_a": "水壶", "sur_a": "桌子"}, "zh": "放到桌子上"},
            ],
        }
        prompt = build_chain_monitor_prompt(assignment, 1)
        self.assertIn("中间步骤只需在窗口内真实发生过", prompt)
        self.assertIn("不得自行重新拆解", prompt)
        self.assertIn("原始指令：拿起水壶并放到桌子上", prompt)
        self.assertIn("手正在靠近水壶", prompt)
        self.assertIn("上一窗口摘要不是本窗口的视觉证据", prompt)
        result = {
            "status": "succeeded", "description_zh": "三步均已完成",
            "task_completion_evidence_timestamp_s": 5.4,
            "step_updates": [
                {"step_id": "pick", "status": "succeeded", "description_zh": "拿起", "failure_reason": None, "evidence": [{"timestamp_s": 1.0, "observation": "水壶离开原支撑面"}], "completion_evidence_timestamp_s": 1.0},
                {"step_id": "carry", "status": "succeeded", "description_zh": "搬运", "failure_reason": None, "evidence": [{"timestamp_s": 2.0, "observation": "水壶被持续手持移动"}], "completion_evidence_timestamp_s": 2.0},
                {"step_id": "place", "status": "succeeded", "description_zh": "放置", "failure_reason": None, "evidence": [{"timestamp_s": 5.4, "observation": "水壶释放后留在桌面"}], "completion_evidence_timestamp_s": 5.4},
            ],
        }
        self.assertEqual(validate_chain_result(result, assignment)["status"], "succeeded")

    def test_chain_result_only_accepts_unconfirmed_suffix(self):
        assignment = {
            "current_step_index": 1,
            "steps": [
                {"step_id": "pick"}, {"step_id": "place"},
            ],
        }
        valid = {
            "status": "in_progress", "description_zh": "仍在执行",
            "task_completion_evidence_timestamp_s": None,
            "step_updates": [
                {"step_id": "place", "status": "in_progress", "description_zh": "未完成", "failure_reason": None, "evidence": [{"timestamp_s": 5.0, "observation": "桌面为空"}], "completion_evidence_timestamp_s": None},
            ],
        }
        self.assertEqual(validate_chain_result(valid, assignment)["status"], "in_progress")
        invalid = {**valid, "step_updates": [
            {"step_id": "pick", "status": "succeeded", "description_zh": "历史步骤", "failure_reason": None, "evidence": [{"timestamp_s": 1.0, "observation": "当前窗口无关证据"}], "completion_evidence_timestamp_s": 1.0},
            *valid["step_updates"],
        ]}
        with self.assertRaisesRegex(ValueError, "一一对应"):
            validate_chain_result(invalid, assignment)
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
        self.assertIn("随后 6 秒视频", prompt)
        self.assertIn("NOW 画面中已经放回", prompt)
        self.assertIn("窗口中途曾经满足、但结尾已不满足", prompt)
        self.assertIn("最后 1 秒", prompt)
        self.assertIn("unknown", prompt)

    def test_result_timestamps_must_stay_inside_six_second_window(self):
        with self.assertRaises(ValueError):
            validate_result({
                "status": "succeeded", "description_zh": "已经完成",
                "failure_reason": None,
                "evidence": [{"timestamp_s": 6.3, "observation": "超出窗口"}],
                "completion_evidence_timestamp_s": 6.3,
            })
        with self.assertRaisesRegex(ValueError, "最后 1 秒"):
            validate_result({
                "status": "succeeded", "description_zh": "中途拿起",
                "failure_reason": None,
                "evidence": [{"timestamp_s": 4.9, "observation": "水壶曾离开桌面"}],
                "completion_evidence_timestamp_s": 4.9,
            })
        valid = validate_result({
            "status": "succeeded", "description_zh": "结尾仍保持拿起",
            "failure_reason": None,
            "evidence": [{"timestamp_s": 5.5, "observation": "NOW 中水壶仍离开桌面"}],
            "completion_evidence_timestamp_s": 5.5,
        })
        self.assertEqual(valid["status"], "succeeded")

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
            now = Path(directory) / "now.jpg"
            baseline.write_bytes(b"image")
            video.write_bytes(b"video")
            now.write_bytes(b"now")
            assignment = {"execution_id": "e", "attempt_id": "a", "action_id": "A_001", "logic": 0, "slots": {"obj_a": "水壶"}}
            await service.submit(assignment=assignment, camera_id="c", sequence=1, baseline_path=baseline, video_path=video, now_path=now, client_timings={})
            await service.submit(assignment=assignment, camera_id="c", sequence=2, baseline_path=baseline, video_path=video, now_path=now, client_timings={})
            with self.assertRaises(RuntimeError):
                await service.submit(assignment=assignment, camera_id="c", sequence=3, baseline_path=baseline, video_path=video, now_path=now, client_timings={})
            await service.close()

    async def test_terminal_result_enters_awaiting_confirmation(self):
        updates = []

        async def update(**kwargs):
            updates.append(kwargs)
            return {}

        with tempfile.TemporaryDirectory() as directory:
            service = VisualMonitorService(update, VisualMonitorConfig(Path(directory)))

            async def terminal(*args):
                return ({
                    "status": "failed",
                    "description_zh": "拿错了物体",
                    "failure_reason": "手中拿起的是纸巾包，不是水壶",
                    "evidence": [{"timestamp_s": 2.0, "observation": "纸巾包被拿起"}],
                    "completion_evidence_timestamp_s": None,
                }, {"bailian_total": 10.0}, "qwen3.7-plus", "{}")

            service._call_bailian = terminal
            baseline = Path(directory) / "before.jpg"
            video = Path(directory) / "window.mp4"
            now = Path(directory) / "now.jpg"
            baseline.write_bytes(b"image")
            video.write_bytes(b"video")
            now.write_bytes(b"now")
            assignment = {
                "execution_id": "e", "attempt_id": "a", "action_id": "A_001",
                "logic": 0, "slots": {"obj_a": "水壶"},
            }
            await service.submit(
                assignment=assignment, camera_id="c", sequence=1,
                baseline_path=baseline, video_path=video, now_path=now, client_timings={},
            )
            for _ in range(50):
                if not service.in_flight:
                    break
                import asyncio
                await asyncio.sleep(0.01)
            self.assertEqual(updates[-1]["patch"]["state"], "awaiting_confirmation")
            self.assertEqual(updates[-1]["patch"]["latest"]["status"], "failed")
            await service.close()

    async def test_succeeded_result_uses_auto_advance_callback(self):
        updates = []
        successes = []

        async def update(**kwargs):
            updates.append(kwargs)
            return {}

        async def succeed(**kwargs):
            successes.append(kwargs)
            return {}

        with tempfile.TemporaryDirectory() as directory:
            service = VisualMonitorService(
                update,
                VisualMonitorConfig(Path(directory)),
                success_callback=succeed,
            )

            async def terminal(*args):
                return ({
                    "status": "succeeded",
                    "description_zh": "水壶在窗口结尾仍被拿起",
                    "failure_reason": None,
                    "evidence": [{"timestamp_s": 5.2, "observation": "水壶底部离开桌面"}],
                    "completion_evidence_timestamp_s": 5.2,
                }, {"bailian_total": 10.0}, "qwen3.7-plus", "{}")

            service._call_bailian = terminal
            service._extract_frame = lambda video, output, timestamp: output.write_bytes(b"jpeg")
            baseline = Path(directory) / "before.jpg"
            video = Path(directory) / "window.mp4"
            now = Path(directory) / "now.jpg"
            baseline.write_bytes(b"image")
            video.write_bytes(b"video")
            now.write_bytes(b"now")
            assignment = {
                "execution_id": "e", "attempt_id": "a", "action_id": "A_001",
                "logic": 0, "slots": {"obj_a": "水壶"},
            }
            await service.submit(
                assignment=assignment, camera_id="c", sequence=1,
                baseline_path=baseline, video_path=video, now_path=now,
                client_timings={},
            )
            for _ in range(50):
                if not service.in_flight:
                    break
                import asyncio
                await asyncio.sleep(0.01)
            self.assertEqual(updates, [])
            self.assertEqual(successes[0]["latest"]["status"], "succeeded")
            self.assertEqual(successes[0]["latest"]["sequence"], 1)
            await service.close()

    async def test_chain_result_uses_chain_callback(self):
        chain_results = []

        async def update(**kwargs):
            return {}

        async def apply_chain(**kwargs):
            chain_results.append(kwargs)
            return {}

        with tempfile.TemporaryDirectory() as directory:
            service = VisualMonitorService(
                update, VisualMonitorConfig(Path(directory)),
                chain_result_callback=apply_chain,
            )
            result = {
                "status": "in_progress", "description_zh": "已经拿起，尚未放置",
                "task_completion_evidence_timestamp_s": None,
                "step_updates": [{
                    "step_id": "pick", "status": "succeeded", "description_zh": "水壶已拿起",
                    "failure_reason": None,
                    "evidence": [{"timestamp_s": 2.0, "observation": "水壶离开桌面"}],
                    "completion_evidence_timestamp_s": 2.0,
                }, {
                    "step_id": "place", "status": "in_progress", "description_zh": "仍被手持",
                    "failure_reason": None,
                    "evidence": [{"timestamp_s": 5.0, "observation": "水壶仍在手中"}],
                    "completion_evidence_timestamp_s": None,
                }],
            }

            async def fake_call(*args):
                return result, {"bailian_total": 10.0}, "qwen3.7-plus", "{}"

            service._call_bailian = fake_call
            service._extract_frame = lambda video, output, timestamp: output.write_bytes(b"jpeg")
            baseline = Path(directory) / "before.jpg"
            video = Path(directory) / "window.mp4"
            now = Path(directory) / "now.jpg"
            baseline.write_bytes(b"image")
            video.write_bytes(b"video")
            now.write_bytes(b"now")
            assignment = {
                "execution_id": "e", "attempt_id": "chain-session", "monitor_scope": "chain",
                "current_step_index": 0,
                "steps": [{"step_id": "pick"}, {"step_id": "place"}],
            }
            await service.submit(
                assignment=assignment, camera_id="c", sequence=1,
                baseline_path=baseline, video_path=video, now_path=now, client_timings={},
            )
            for _ in range(50):
                if not service.in_flight:
                    break
                import asyncio
                await asyncio.sleep(0.01)
            self.assertEqual(chain_results[0]["attempt_id"], "chain-session")
            self.assertEqual(chain_results[0]["latest"]["step_updates"][0]["completion_evidence_url"].startswith("/api/visual-monitor/media/"), True)
            await service.close()
