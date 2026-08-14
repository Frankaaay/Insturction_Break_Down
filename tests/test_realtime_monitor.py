import tempfile
import unittest
from pathlib import Path

from realtime_monitor import (
    CHAIN_OUTPUT_SCHEMA, OUTPUT_SCHEMA, ModelResponseValidationError,
    VisualMonitorConfig, VisualMonitorService,
    normalize_model_json, output_schema_for_window,
    validate_chain_result, validate_result,
)
from visual_contracts import (
    build_chain_monitor_prompt, build_monitor_prompt, get_visual_contract,
    supports_visual_contract,
)


class RealtimeMonitorContractTests(unittest.IsolatedAsyncioTestCase):
    def test_realtime_schemas_expose_only_three_business_states(self):
        expected = ["in_progress", "succeeded", "failed"]
        self.assertEqual(OUTPUT_SCHEMA["properties"]["status"]["enum"], expected)
        self.assertEqual(CHAIN_OUTPUT_SCHEMA["properties"]["status"]["enum"], expected)
        self.assertEqual(
            CHAIN_OUTPUT_SCHEMA["properties"]["step_updates"]["items"]["properties"]["status"]["enum"],
            expected,
        )

    def test_chain_prompt_and_result_allow_fast_intermediate_steps(self):
        assignment = {
            "instruction": "拿起水壶并放到桌子上",
            "current_step_index": 0,
            "confirmed_steps": [],
            "unfinished_steps": [
                {"step_id": "pick", "status": "in_progress", "description_zh": "手正在靠近水壶"},
                {"step_id": "carry", "status": "in_progress", "description_zh": None},
                {"step_id": "place", "status": "in_progress", "description_zh": None},
            ],
            "steps": [
                {"step_id": "pick", "action_id": "A_001", "logic": 0, "slots": {"obj_a": "水壶"}, "zh": "拿起水壶"},
                {"step_id": "carry", "action_id": "A_003", "logic": 0, "slots": {"obj_a": "水壶"}, "zh": "搬运水壶"},
                {"step_id": "place", "action_id": "A_002", "logic": 1, "slots": {"obj_a": "水壶", "sur_a": "桌子"}, "zh": "放到桌子上"},
            ],
        }
        prompt = build_chain_monitor_prompt(assignment, 1)
        self.assertIn("中间步骤只需在窗口内真实发生过", prompt)
        self.assertIn("自行重新拆解", prompt)
        self.assertIn("原始指令：拿起水壶并放到桌子上", prompt)
        self.assertNotIn("手正在靠近水壶", prompt)
        self.assertIn("唯一的 CURRENT_WINDOW 视频", prompt)
        self.assertIn("不存在任何历史视频", prompt)
        self.assertIn("一张 HISTORY_STEP_KEYFRAME", prompt)
        self.assertNotIn("PREV_NOW", prompt)
        result = {
            "status": "succeeded", "description_zh": "三步均已完成",
            "task_completion_evidence_timestamp_s": 6.4,
            "step_updates": [
                {"step_id": "pick", "status": "succeeded", "description_zh": "拿起", "failure_reason": None, "evidence": [{"timestamp_s": 1.0, "observation": "水壶离开原支撑面"}], "completion_evidence_timestamp_s": 1.0},
                {"step_id": "carry", "status": "succeeded", "description_zh": "搬运", "failure_reason": None, "evidence": [{"timestamp_s": 2.0, "observation": "水壶被持续手持移动"}], "completion_evidence_timestamp_s": 2.0},
                {"step_id": "place", "status": "succeeded", "description_zh": "放置", "failure_reason": None, "evidence": [{"timestamp_s": 6.4, "observation": "水壶释放后留在桌面"}], "completion_evidence_timestamp_s": 6.4},
            ],
        }
        self.assertEqual(validate_chain_result(result, assignment)["status"], "succeeded")

    def test_chain_place_prompt_uses_authoritative_history_and_persistent_now(self):
        assignment = {
            "instruction": "拿起水壶并放到桌子上",
            "current_step_index": 2,
            "current_step_id": "place",
            "confirmed_steps": [
                {"step_id": "pick", "status": "succeeded"},
                {"step_id": "carry", "status": "succeeded"},
            ],
            "steps": [
                {"step_id": "pick", "action_id": "A_001", "logic": 0, "slots": {"obj_a": "水壶"}, "zh": "拿起水壶"},
                {"step_id": "carry", "action_id": "A_003", "logic": 0, "slots": {"obj_a": "水壶"}, "zh": "搬运水壶"},
                {"step_id": "place", "action_id": "A_002", "logic": 1, "slots": {"obj_a": "水壶", "sur_a": "桌子"}, "zh": "放到桌子上"},
            ],
        }
        prompt = build_chain_monitor_prompt(assignment, 3, 8.5)
        self.assertIn("当前执行 Step：step_id=place，步骤序号=3", prompt)
        self.assertIn("pick=succeeded（已附带唯一冻结成功关键帧）", prompt)
        self.assertIn("carry=succeeded（已附带唯一冻结成功关键帧）", prompt)
        self.assertIn("禁止回答“尚未拿起”“尚未搬运”", prompt)
        self.assertIn("只要 CURRENT_NOW 能确认指定目标物稳定留在指定 sur_a", prompt)
        self.assertIn("不得重新判断、否认或回退", prompt)

    async def test_history_steps_contribute_one_frozen_image_each_and_no_video(self):
        async def update(**kwargs):
            return kwargs

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pick.jpg").write_bytes(b"pick-image")
            (root / "carry.jpg").write_bytes(b"carry-image")
            service = VisualMonitorService(update, VisualMonitorConfig(root))
            assignment = {
                "monitor_scope": "chain",
                "steps": [
                    {"step_id": "pick", "zh": "拿起水壶"},
                    {"step_id": "carry", "zh": "搬运水壶"},
                ],
                "confirmed_steps": [
                    {
                        "step_id": "pick", "status": "succeeded",
                        "success_keyframe_url": "/api/visual-monitor/media/pick.jpg",
                        "success_evidence_observation": "水壶离开原支撑面",
                    },
                    {
                        "step_id": "carry", "status": "succeeded",
                        "success_keyframe_url": "/api/visual-monitor/media/carry.jpg",
                        "success_evidence_observation": "水壶相对拿起位置发生位移",
                    },
                ],
            }
            try:
                content, count = service._history_keyframe_content(assignment)
                self.assertEqual(count, 2)
                self.assertEqual(
                    [item["type"] for item in content],
                    ["text", "image_url", "text", "image_url"],
                )
                self.assertFalse(any(item["type"] == "video_url" for item in content))
                self.assertIn("冻结证据说明=水壶离开原支撑面", content[0]["text"])
                self.assertIn("冻结证据说明=水壶相对拿起位置发生位移", content[2]["text"])
                broken = {**assignment, "confirmed_steps": [{
                    "step_id": "pick", "status": "succeeded",
                    "success_keyframe_url": None,
                }]}
                with self.assertRaisesRegex(RuntimeError, "缺少成功关键帧"):
                    service._history_keyframe_content(broken)
            finally:
                await service.close()

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
        with self.assertRaisesRegex(ValueError, "连续前缀"):
            validate_chain_result(invalid, assignment)

    def test_chain_result_accepts_wrong_object_failure_without_future_placeholders(self):
        assignment = {
            "current_step_index": 0,
            "steps": [{"step_id": "pick"}, {"step_id": "carry"}, {"step_id": "place"}],
        }
        result = {
            "status": "failed", "description_zh": "第一步拿错了物体",
            "task_completion_evidence_timestamp_s": None,
            "step_updates": [{
                "step_id": "pick", "status": "failed", "description_zh": "拿起了蓝色塑料包",
                "failure_reason": "手拿起的是水壶旁边的蓝色塑料包，水壶仍留在桌面",
                "evidence": [{"timestamp_s": 4.5, "observation": "蓝色塑料包被手提离桌面，黑色水壶保持原位"}],
                "completion_evidence_timestamp_s": None,
            }],
        }
        self.assertEqual(validate_chain_result(result, assignment)["status"], "failed")

    def test_chain_result_stops_at_first_non_success(self):
        assignment = {
            "current_step_index": 0,
            "steps": [{"step_id": "pick"}, {"step_id": "carry"}, {"step_id": "place"}],
        }
        common = {
            "failure_reason": None,
            "evidence": [{"timestamp_s": 2.0, "observation": "直接可见事实"}],
            "completion_evidence_timestamp_s": None,
        }
        after_failure = {
            "status": "failed", "description_zh": "拿错物体",
            "task_completion_evidence_timestamp_s": None,
            "step_updates": [
                {**common, "step_id": "pick", "status": "failed", "failure_reason": "拿起了手机", "description_zh": "拿起手机"},
                {**common, "step_id": "carry", "status": "in_progress", "description_zh": "未执行"},
            ],
        }
        with self.assertRaisesRegex(ValueError, "第一个未成功步骤"):
            validate_chain_result(after_failure, assignment)

        omitted_next_step = {
            "status": "succeeded", "description_zh": "只报告拿起成功",
            "task_completion_evidence_timestamp_s": 5.5,
            "step_updates": [{
                **common, "step_id": "pick", "status": "succeeded", "description_zh": "已拿起",
                "completion_evidence_timestamp_s": 2.0,
            }],
        }
        with self.assertRaisesRegex(ValueError, "成功前缀不能遗漏"):
            validate_chain_result(omitted_next_step, assignment)

    def test_chain_prompt_treats_visible_no_action_as_in_progress(self):
        assignment = {
            "instruction": "拿起水壶并放到桌子上",
            "current_step_index": 0,
            "confirmed_steps": [],
            "unfinished_steps": [{"step_id": "pick", "status": "in_progress", "description_zh": None}],
            "steps": [{
                "step_id": "pick", "action_id": "A_001", "logic": 0,
                "slots": {"obj_a": "水壶"}, "zh": "拿起水壶",
            }],
        }
        prompt = build_chain_monitor_prompt(assignment, 2)
        self.assertIn("仍保持初始状态，返回 in_progress", prompt)
        self.assertIn("一旦遇到第一个 in_progress 或 failed 就停止输出", prompt)
        self.assertNotIn("unknown：", prompt)

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
        self.assertIn("随后 7 秒连续视频", prompt)
        self.assertIn("NOW 画面中已经放回", prompt)
        self.assertIn("窗口中途曾经满足、但结尾已不满足", prompt)
        self.assertIn("最后 1 秒", prompt)
        self.assertIn("无法确认物体身份时返回 in_progress", prompt)
        self.assertNotIn("unknown：", prompt)

    def test_model_json_normalization_is_finite_and_type_safe(self):
        normalized, applied = normalize_model_json([{
            "status": "unknown",
            "step_updates": [{"status": "unknown"}],
        }])
        self.assertEqual(normalized["status"], "in_progress")
        self.assertEqual(normalized["step_updates"][0]["status"], "in_progress")
        self.assertEqual(applied, [
            "unwrapped_singleton_array",
            "top_status_unknown_to_in_progress",
            "step_0_status_unknown_to_in_progress",
        ])
        for invalid in ([], [{}, {}], [[{}]], "text", 1, None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_model_json(invalid)

    def test_singleton_array_wrong_object_response_validates_after_unwrap(self):
        assignment = {
            "current_step_index": 0,
            "steps": [{"step_id": "pick"}, {"step_id": "carry"}, {"step_id": "place"}],
        }
        provider_value = [{
            "status": "failed",
            "description_zh": "拿起的是塑料袋而不是水壶",
            "step_updates": [{
                "step_id": "pick",
                "status": "failed",
                "description_zh": "实际拿起了蓝色塑料袋",
                "failure_reason": "操作对象与指定水壶不一致",
                "evidence": [{
                    "timestamp_s": 1.2,
                    "observation": "蓝色塑料袋被手提起离开桌面",
                }],
                "completion_evidence_timestamp_s": None,
            }],
            "task_completion_evidence_timestamp_s": None,
        }]
        normalized, applied = normalize_model_json(provider_value)
        result = validate_chain_result(normalized, assignment)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(applied, ["unwrapped_singleton_array"])

    def test_validators_reject_non_objects_without_python_type_errors(self):
        assignment = {"current_step_index": 0, "steps": [{"step_id": "pick"}]}
        for invalid in ([], ["bad"], "bad", 1, None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_result(invalid)
                with self.assertRaises(ValueError):
                    validate_chain_result(invalid, assignment)
        malformed_chain = {
            "status": "in_progress", "description_zh": "仍在执行",
            "task_completion_evidence_timestamp_s": None,
            "step_updates": ["not-an-object"],
        }
        with self.assertRaisesRegex(ValueError, "必须是 JSON 对象"):
            validate_chain_result(malformed_chain, assignment)

    def test_result_timestamps_must_stay_inside_default_seven_second_window(self):
        with self.assertRaises(ValueError):
            validate_result({
                "status": "succeeded", "description_zh": "已经完成",
                "failure_reason": None,
                "evidence": [{"timestamp_s": 7.3, "observation": "超出窗口"}],
                "completion_evidence_timestamp_s": 7.3,
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
            "evidence": [{"timestamp_s": 6.5, "observation": "NOW 中水壶仍离开桌面"}],
            "completion_evidence_timestamp_s": 6.5,
        })
        self.assertEqual(valid["status"], "succeeded")

    def test_dynamic_eleven_second_window_updates_schema_prompt_and_validation(self):
        schema = output_schema_for_window(11.0, chain_mode=True)
        self.assertEqual(
            schema["properties"]["task_completion_evidence_timestamp_s"]["maximum"],
            11.2,
        )
        prompt = build_monitor_prompt({
            "action_id": "A_001", "logic": 0, "slots": {"obj_a": "水壶"},
        }, 2, 11.0)
        self.assertIn("11 秒连续视频", prompt)
        self.assertIn("10 到 11 秒", prompt)
        valid = validate_result({
            "status": "succeeded", "description_zh": "结尾仍被拿起",
            "failure_reason": None,
            "evidence": [{"timestamp_s": 10.5, "observation": "水壶仍离开桌面"}],
            "completion_evidence_timestamp_s": 10.5,
        }, 11.0)
        self.assertEqual(valid["status"], "succeeded")
        with self.assertRaises(ValueError):
            validate_result({
                "status": "succeeded", "description_zh": "越界",
                "failure_reason": None,
                "evidence": [{"timestamp_s": 11.3, "observation": "超出窗口"}],
                "completion_evidence_timestamp_s": 11.3,
            }, 11.0)

    def test_action_specific_contracts_share_common_output_rules(self):
        carry = build_monitor_prompt({
            "action_id": "A_003", "logic": 0, "slots": {"obj_a": "水壶"}
        }, 1)
        self.assertIn("移动慢、暂时停止", carry)
        self.assertIn("不要求判断最终目标位置", carry)
        self.assertIn("仅在明确搬运了错误物体", carry)
        self.assertIn("当前阶段由人类用手执行操作", carry)
        self.assertIn("不得因为机械臂静止", carry)
        self.assertIn("由人手操作所以失败", carry)
        self.assertIn("transition_event", carry)

        place = build_monitor_prompt({
            "action_id": "A_002", "logic": 1,
            "slots": {"obj_a": "水壶", "sur_a": "桌子"},
        }, 1)
        self.assertIn("由该表面承托", place)
        self.assertIn("手已经释放", place)
        self.assertIn("failure_reason 必须为 null", place)
        self.assertIn("persistent_state", place)

        with self.assertRaises(ValueError):
            build_monitor_prompt({
                "action_id": "A_002", "logic": 0,
                "slots": {"obj_a": "水壶", "obj_b": "杯子"},
            }, 1)

    def test_new_desktop_action_contracts_are_registered_with_exact_slots(self):
        expected = {
            ("A_014", 0): (("obj_a",), "current_now"),
            ("A_009", 0): (("obj_a", "sur_a"), "event_in_window"),
            ("A_017", 0): (("obj_a",), "current_now"),
            ("A_016", 0): (("obj_a",), "current_now"),
            ("A_010", 0): (("obj_a", "obj_b"), "current_now"),
        }
        for key, (slots, policy) in expected.items():
            with self.subTest(action=key):
                self.assertTrue(supports_visual_contract(*key))
                contract = get_visual_contract(*key)
                self.assertEqual(contract.required_slots, slots)
                self.assertEqual(contract.terminal_evidence_policy, policy)

    def test_turn_open_close_insert_prompts_define_visible_persistent_results(self):
        turn = build_monitor_prompt({
            "action_id": "A_014", "logic": 0, "slots": {"obj_a": "笔记本"},
        }, 1)
        self.assertIn("不能把桌面内平移或水平旋转当成翻面", turn)
        self.assertIn("正反面相似", turn)
        self.assertIn("最后 1 秒", turn)

        opened = build_monitor_prompt({
            "action_id": "A_017", "logic": 0, "slots": {"obj_a": "笔记本电脑"},
        }, 1)
        self.assertIn("功能性打开状态", opened)
        self.assertIn("开口、内部空间或可访问区域明确暴露", opened)

        closed = build_monitor_prompt({
            "action_id": "A_016", "logic": 0, "slots": {"obj_a": "笔记本电脑"},
        }, 1)
        self.assertIn("功能性关闭状态", closed)
        self.assertIn("不要求推断不可见的机械锁扣", closed)

        inserted = build_monitor_prompt({
            "action_id": "A_010", "logic": 0,
            "slots": {"obj_a": "马克笔", "obj_b": "笔筒"},
        }, 1)
        self.assertIn("开口、插槽或受约束区域", inserted)
        self.assertIn("放入宽大开放区域不足以证明 Insert", inserted)
        self.assertIn("手已经释放", inserted)

    def test_wipe_prompt_requires_contact_motion_but_not_cleanliness(self):
        prompt = build_monitor_prompt({
            "action_id": "A_009", "logic": 0,
            "slots": {"obj_a": "白色纸巾", "sur_a": "桌子"},
        }, 1)
        self.assertIn("持续接触", prompt)
        self.assertIn("明确、连续的擦拭位移", prompt)
        self.assertIn("不要求判断表面已经完全擦干净", prompt)
        self.assertIn("不要求接触或运动一直保持到 CURRENT_NOW", prompt)
        self.assertIn("任意时刻（0 到 7 秒）", prompt)
        self.assertNotIn("completion_evidence_timestamp_s 必须在最后 1 秒", prompt)

    def test_wipe_event_can_complete_early_but_persistent_actions_cannot(self):
        early_success = {
            "status": "succeeded",
            "description_zh": "纸巾与桌面持续接触并完成擦拭",
            "failure_reason": None,
            "evidence": [{
                "timestamp_s": 2.4,
                "observation": "纸巾贴着桌面连续向左再向右移动",
            }],
            "completion_evidence_timestamp_s": 2.4,
        }
        self.assertEqual(
            validate_result(
                early_success, terminal_evidence_policy="event_in_window",
            )["status"],
            "succeeded",
        )
        with self.assertRaisesRegex(ValueError, "最后 1 秒"):
            validate_result(early_success, terminal_evidence_policy="current_now")

    def test_chain_wipe_final_accepts_early_event_evidence(self):
        assignment = {
            "instruction": "用纸巾擦桌子",
            "current_step_index": 0,
            "current_step_id": "wipe",
            "confirmed_steps": [],
            "steps": [{
                "step_id": "wipe", "action_id": "A_009", "logic": 0,
                "slots": {"obj_a": "纸巾", "sur_a": "桌子"}, "zh": "用纸巾擦桌子",
            }],
        }
        prompt = build_chain_monitor_prompt(assignment, 1)
        self.assertIn("最后一个步骤属于短时事件动作", prompt)
        self.assertIn("task_completion_evidence_timestamp_s；其值必须位于本次 WINDOW 内", prompt)
        result = {
            "status": "succeeded",
            "description_zh": "擦拭动作已完成",
            "task_completion_evidence_timestamp_s": 2.4,
            "step_updates": [{
                "step_id": "wipe", "status": "succeeded",
                "description_zh": "纸巾与桌面接触并连续擦过",
                "failure_reason": None,
                "evidence": [{
                    "timestamp_s": 2.4,
                    "observation": "纸巾贴着桌面完成连续往返移动",
                }],
                "completion_evidence_timestamp_s": 2.4,
            }],
        }
        self.assertEqual(validate_chain_result(result, assignment)["status"], "succeeded")

    def test_new_contracts_reject_missing_required_slots(self):
        for action_id, slots in (
            ("A_009", {"obj_a": "纸巾"}),
            ("A_010", {"obj_a": "马克笔"}),
        ):
            with self.subTest(action_id=action_id):
                with self.assertRaisesRegex(ValueError, "缺少 slots"):
                    build_monitor_prompt({
                        "action_id": action_id, "logic": 0, "slots": slots,
                    }, 1)

    def test_chain_prompt_treats_human_hand_as_valid_operator(self):
        prompt = build_chain_monitor_prompt({
            "instruction": "拿起水壶并放到桌子上",
            "current_step_index": 0,
            "confirmed_steps": [],
            "steps": [{
                "step_id": "pick", "action_id": "A_001", "logic": 0,
                "slots": {"obj_a": "水壶"}, "zh": "拿起水壶",
            }],
        }, 1)
        self.assertIn("人手是合法且唯一需要评估的执行者", prompt)
        self.assertIn("机械臂、夹爪暂时视为无关背景", prompt)
        self.assertIn("人手使指定目标物满足动作成功后置条件", prompt)

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
            self.assertEqual(
                successes[0]["latest"]["completion_evidence_timestamp_s"], 5.2,
            )
            self.assertTrue(
                successes[0]["latest"]["completion_evidence_url"].startswith(
                    "/api/visual-monitor/media/"
                )
            )
            self.assertEqual(
                successes[0]["latest"]["evidence"][0]["image_url"],
                successes[0]["latest"]["completion_evidence_url"],
            )
            await service.close()

    async def test_end_boundary_completion_uses_now_image(self):
        successes = []

        async def update(**kwargs):
            return {}

        async def succeed(**kwargs):
            successes.append(kwargs)
            return {}

        with tempfile.TemporaryDirectory() as directory:
            service = VisualMonitorService(
                update, VisualMonitorConfig(Path(directory)), success_callback=succeed,
            )

            async def terminal(*args):
                return ({
                    "status": "succeeded",
                    "description_zh": "水壶在窗口结尾仍位于目标桌面",
                    "failure_reason": None,
                    "evidence": [{"timestamp_s": 7.0, "observation": "水壶稳定留在桌面"}],
                    "completion_evidence_timestamp_s": 7.0,
                }, {"bailian_total": 10.0}, "qwen3.7-plus", "{}")

            service._call_bailian = terminal
            service._extract_frame = lambda *args: self.fail("7.0s 应直接使用 NOW")
            baseline = Path(directory) / "before.jpg"
            video = Path(directory) / "window.mp4"
            now = Path(directory) / "now.jpg"
            baseline.write_bytes(b"image")
            video.write_bytes(b"video")
            now.write_bytes(b"now")
            await service.submit(
                assignment={
                    "execution_id": "e", "attempt_id": "a", "action_id": "A_002",
                    "logic": 1, "slots": {"obj_a": "水壶", "sur_a": "桌子"},
                },
                camera_id="c", sequence=1, baseline_path=baseline,
                video_path=video, now_path=now, client_timings={},
            )
            for _ in range(50):
                if not service.in_flight:
                    break
                import asyncio
                await asyncio.sleep(0.01)
            self.assertEqual(
                successes[0]["latest"]["completion_evidence_url"],
                f"/api/visual-monitor/media/{now.name}",
            )
            self.assertEqual(
                successes[0]["latest"]["evidence"][0]["image_url"],
                f"/api/visual-monitor/media/{now.name}",
            )
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
            updates = chain_results[0]["latest"]["step_updates"]
            self.assertTrue(updates[0]["completion_evidence_url"].startswith("/api/visual-monitor/media/"))
            self.assertEqual(
                updates[0]["evidence"][0]["image_url"],
                updates[0]["completion_evidence_url"],
            )
            self.assertTrue(updates[1]["evidence"][0]["image_url"].startswith("/api/visual-monitor/media/"))
            await service.close()

    async def test_validation_failure_persists_raw_model_response(self):
        updates = []

        async def update(**kwargs):
            updates.append(kwargs)
            return {}

        with tempfile.TemporaryDirectory() as directory:
            service = VisualMonitorService(update, VisualMonitorConfig(Path(directory)))

            async def invalid(*args):
                raise ModelResponseValidationError(
                    "模型 evidence 非法", raw_body='{"raw":"response"}',
                    actual_model="qwen3.7-plus", timings_ms={"bailian_total": 8400.0},
                )

            service._call_bailian = invalid
            baseline = Path(directory) / "before.jpg"
            video = Path(directory) / "window.mp4"
            now = Path(directory) / "now.jpg"
            baseline.write_bytes(b"image")
            video.write_bytes(b"video")
            now.write_bytes(b"now")
            await service.submit(
                assignment={"execution_id": "e", "attempt_id": "a", "action_id": "A_001", "logic": 0, "slots": {"obj_a": "水壶"}},
                camera_id="c", sequence=1, baseline_path=baseline,
                video_path=video, now_path=now, client_timings={"encode": 100.0},
            )
            for _ in range(50):
                if not service.in_flight:
                    break
                import asyncio
                await asyncio.sleep(0.01)
            saved = __import__("json").loads(video.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(saved["raw"], '{"raw":"response"}')
            self.assertEqual(saved["validation_error"], "模型 evidence 非法")
            self.assertEqual(saved["latest"]["status"], "in_progress")
            self.assertEqual(updates[-1]["patch"]["pipeline"]["phase"], "error")
            await service.close()
