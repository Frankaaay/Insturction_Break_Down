import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from execution import ExecutionConflictError, ExecutionManager


STEPS = [
    {
        "action_id": "A_001",
        "action": "Pick",
        "logic": 0,
        "slots": {"obj_a": "水壶"},
        "zh": "拿起水壶",
        "en": "Pick up the kettle.",
    },
    {
        "action_id": "A_002",
        "action": "Place",
        "logic": 1,
        "slots": {"obj_a": "水壶", "sur_a": "桌子"},
        "zh": "把水壶放到桌子上",
        "en": "Place the kettle on the table.",
    },
]

CHAIN_STEPS = [
    STEPS[0],
    {
        "action_id": "A_003", "action": "Carry", "logic": 0,
        "slots": {"obj_a": "水壶"}, "zh": "搬运水壶", "en": "Carry the kettle.",
    },
    STEPS[1],
]

WORKFLOW = {
    "workflow_id": "grasparm.auto-pick",
    "version": "3",
    "digest": "a" * 64,
    "capability_id": "A_001",
    "label": "GraspArm Auto Pick",
    "nodes": [
        {
            "node_id": "controller",
            "label": "Controller",
            "type": "service",
            "host": "arm",
            "depends_on": [],
        },
        {
            "node_id": "reset",
            "label": "Reset",
            "type": "command",
            "host": "arm",
            "depends_on": ["controller"],
        },
        {
            "node_id": "auto_pick",
            "label": "Auto Pick",
            "type": "motion",
            "host": "arm",
            "depends_on": ["reset"],
        },
        {
            "node_id": "discover",
            "label": "YOLOE Discover",
            "type": "command",
            "host": "arm",
            "depends_on": ["reset"],
            "start_after": {
                "node_id": "auto_pick",
                "marker": "Waiting for MosaicGrasp candidate",
            },
        },
    ],
}


class ExecutionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ExecutionManager(timeout_seconds=1, db_path="")

    async def asyncTearDown(self):
        await self.manager.close()

    async def create_and_start(self):
        execution = await self.manager.create("把水壶放到桌子上", "test", None, STEPS)
        return await self.manager.start(execution["execution_id"])

    async def test_chain_visual_monitor_advances_multiple_steps_without_new_session(self):
        execution = await self.manager.create(
            "拿起水壶并放到桌子上", "test", None, CHAIN_STEPS,
            execution_mode="chain_visual_monitor",
        )
        started = await self.manager.start(execution["execution_id"])
        assignment = await self.manager.claim_visual_monitor("camera-1", "qwen3.7-plus")
        self.assertEqual(assignment["monitor_scope"], "chain")
        self.assertEqual(len(assignment["steps"]), 3)
        session_id = assignment["attempt_id"]
        await self.manager.update_visual_monitor(
            execution["execution_id"], attempt_id=session_id, camera_id="camera-1",
            patch={"state": "ready"}, event_type="visual_monitor.baseline.ready",
        )
        await self.manager.begin_visual_checkpoint(
            execution["execution_id"], attempt_id=session_id, camera_id="camera-1", sequence=1,
        )
        updates = [
            {
                "step_id": started["steps"][0]["step_id"], "status": "succeeded",
                "description_zh": "已拿起",
                "evidence": [{"timestamp_s": 1.1, "observation": "手接近水壶"}, {"timestamp_s": 2.0, "observation": "水壶离开支撑面"}],
                "completion_evidence_timestamp_s": 2.0,
                "completion_evidence_url": "/api/visual-monitor/media/pick.jpg",
            },
            {
                "step_id": started["steps"][1]["step_id"], "status": "succeeded",
                "description_zh": "已搬运",
                "evidence": [{"timestamp_s": 3.0, "observation": "水壶相对拿起位置发生位移"}],
                "completion_evidence_timestamp_s": 3.0,
                "completion_evidence_url": "/api/visual-monitor/media/carry.jpg",
            },
            {"step_id": started["steps"][2]["step_id"], "status": "in_progress", "description_zh": "尚未放稳"},
        ]
        snapshot = await self.manager.apply_chain_visual_result(
            execution["execution_id"], attempt_id=session_id, camera_id="camera-1",
            latest={"status": "in_progress", "description_zh": "前两步完成", "step_updates": updates, "sequence": 1},
        )
        self.assertEqual([step["status"] for step in snapshot["steps"]], ["succeeded", "succeeded", "active"])
        observation_event = next(
            event for event in snapshot["events"]
            if event["type"] == "chain_visual_monitor.observation"
        )
        self.assertEqual(observation_event["data"]["observation"]["description_zh"], "前两步完成")
        self.assertEqual(len(observation_event["data"]["observation"]["step_updates"]), 3)
        self.assertEqual(snapshot["current_step_index"], 2)
        reclaimed = await self.manager.claim_visual_monitor("camera-1", "qwen3.7-plus")
        self.assertEqual(reclaimed["attempt_id"], session_id)
        self.assertEqual(reclaimed["monitor_state"], "observing")
        self.assertEqual(reclaimed["current_step_index"], 2)
        self.assertEqual(reclaimed["current_step_id"], started["steps"][2]["step_id"])
        self.assertEqual(
            [step["monitor_status"] for step in reclaimed["steps"]],
            ["succeeded", "succeeded", "current"],
        )
        self.assertEqual(reclaimed["confirmed_steps"], [
            {
                "step_id": started["steps"][0]["step_id"], "status": "succeeded",
                "description_zh": "已拿起",
                "success_keyframe_url": "/api/visual-monitor/media/pick.jpg",
                "success_evidence_observation": "水壶离开支撑面",
                "completion_evidence_timestamp_s": 2.0,
            },
            {
                "step_id": started["steps"][1]["step_id"], "status": "succeeded",
                "description_zh": "已搬运",
                "success_keyframe_url": "/api/visual-monitor/media/carry.jpg",
                "success_evidence_observation": "水壶相对拿起位置发生位移",
                "completion_evidence_timestamp_s": 3.0,
            },
        ])
        self.assertEqual(reclaimed["unfinished_steps"], [{
            "step_id": started["steps"][2]["step_id"],
            "status": "in_progress",
            "description_zh": "尚未放稳",
        }])

    async def test_chain_visual_monitor_accumulates_success_across_three_windows(self):
        execution = await self.manager.create(
            "拿起水壶并放到桌子上", "test", None, CHAIN_STEPS,
            execution_mode="chain_visual_monitor",
        )
        started = await self.manager.start(execution["execution_id"])
        step_ids = [step["step_id"] for step in started["steps"]]
        assignment = await self.manager.claim_visual_monitor("camera-1", "qwen3.7-plus")
        session_id = assignment["attempt_id"]
        await self.manager.update_visual_monitor(
            execution["execution_id"], attempt_id=session_id, camera_id="camera-1",
            patch={"state": "ready"}, event_type="visual_monitor.baseline.ready",
        )

        windows = [
            [
                {"step_id": step_ids[0], "status": "succeeded", "description_zh": "水壶已离开桌面"},
                {"step_id": step_ids[1], "status": "in_progress", "description_zh": "正在搬运"},
            ],
            [
                {"step_id": step_ids[1], "status": "succeeded", "description_zh": "已搬到目标桌面附近"},
                {"step_id": step_ids[2], "status": "in_progress", "description_zh": "尚未释放"},
            ],
            [
                {"step_id": step_ids[2], "status": "succeeded", "description_zh": "释放后稳定留在桌面"},
            ],
        ]
        expected_indices = [1, 2, 2]
        for sequence, (updates, expected_index) in enumerate(zip(windows, expected_indices), start=1):
            await self.manager.begin_visual_checkpoint(
                execution["execution_id"], attempt_id=session_id,
                camera_id="camera-1", sequence=sequence,
            )
            terminal = sequence == len(windows)
            snapshot = await self.manager.apply_chain_visual_result(
                execution["execution_id"], attempt_id=session_id, camera_id="camera-1",
                latest={
                    "status": "succeeded" if terminal else "in_progress",
                    "description_zh": updates[-1]["description_zh"],
                    "step_updates": updates,
                    "sequence": sequence,
                },
            )
            self.assertEqual(snapshot["current_step_index"], expected_index)
            if not terminal:
                reclaimed = await self.manager.claim_visual_monitor("camera-1", "qwen3.7-plus")
                self.assertEqual(reclaimed["attempt_id"], session_id)
                self.assertEqual(reclaimed["current_step_index"], expected_index)

        self.assertEqual(snapshot["state"], "completed")
        self.assertEqual([step["status"] for step in snapshot["steps"]], ["succeeded"] * 3)
        self.assertEqual(snapshot["chain_visual_monitor"]["state"], "succeeded")

    async def test_chain_visual_monitor_failure_stays_on_first_wrong_step(self):
        execution = await self.manager.create(
            "拿起水壶并放到桌子上", "test", None, CHAIN_STEPS,
            execution_mode="chain_visual_monitor",
        )
        started = await self.manager.start(execution["execution_id"])
        assignment = await self.manager.claim_visual_monitor("camera-1")
        await self.manager.update_visual_monitor(
            execution["execution_id"], attempt_id=assignment["attempt_id"], camera_id="camera-1",
            patch={"state": "ready"}, event_type="visual_monitor.baseline.ready",
        )
        await self.manager.begin_visual_checkpoint(
            execution["execution_id"], attempt_id=assignment["attempt_id"], camera_id="camera-1", sequence=1,
        )
        updates = [
            {"step_id": started["steps"][0]["step_id"], "status": "failed", "description_zh": "拿起了手机", "failure_reason": "拿错物体"},
        ]
        snapshot = await self.manager.apply_chain_visual_result(
            execution["execution_id"], attempt_id=assignment["attempt_id"], camera_id="camera-1",
            latest={"status": "failed", "description_zh": "第一步拿错物体", "step_updates": updates, "sequence": 1},
        )
        self.assertEqual(snapshot["current_step_index"], 0)
        self.assertEqual(snapshot["steps"][0]["status"], "active")
        self.assertEqual(snapshot["chain_visual_monitor"]["state"], "awaiting_confirmation")

    async def test_chain_claim_maps_legacy_unknown_history_to_in_progress(self):
        execution = await self.manager.create(
            "拿起水壶并放到桌子上", "test", None, CHAIN_STEPS,
            execution_mode="chain_visual_monitor",
        )
        started = await self.manager.start(execution["execution_id"])
        assignment = await self.manager.claim_visual_monitor("camera-1")
        await self.manager.update_visual_monitor(
            execution["execution_id"], attempt_id=assignment["attempt_id"], camera_id="camera-1",
            patch={"state": "ready"}, event_type="visual_monitor.baseline.ready",
        )
        await self.manager.begin_visual_checkpoint(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", sequence=1,
        )
        await self.manager.apply_chain_visual_result(
            execution["execution_id"], attempt_id=assignment["attempt_id"], camera_id="camera-1",
            latest={
                "status": "unknown", "description_zh": "旧版本看不清",
                "step_updates": [{
                    "step_id": started["steps"][0]["step_id"],
                    "status": "unknown", "description_zh": "目标物被遮挡",
                }],
                "sequence": 1,
            },
        )
        reclaimed = await self.manager.claim_visual_monitor("camera-1")
        self.assertEqual(reclaimed["previous_status"], "in_progress")
        self.assertEqual(reclaimed["unfinished_steps"][0]["status"], "in_progress")
        self.assertEqual(reclaimed["unfinished_steps"][0]["description_zh"], "目标物被遮挡")

    async def test_chain_pipeline_telemetry_is_visible_and_rejects_stale_sequence(self):
        execution = await self.manager.create(
            "拿起水壶并放到桌子上", "test", None, CHAIN_STEPS,
            execution_mode="chain_visual_monitor",
        )
        await self.manager.start(execution["execution_id"])
        assignment = await self.manager.claim_visual_monitor("camera-1")
        snapshot = await self.manager.update_visual_pipeline(
            execution["execution_id"], attempt_id=assignment["attempt_id"], camera_id="camera-1",
            pipeline={
                "phase": "capturing", "sequence": 1,
                "phase_started_at": "2026-08-11T00:00:00Z",
                "window_started_at": "2026-08-11T00:00:00Z",
                "window_ended_at": "2026-08-11T00:00:06Z",
            },
        )
        self.assertEqual(snapshot["chain_visual_monitor"]["pipeline"]["phase"], "capturing")
        with self.assertRaises(ExecutionConflictError):
            await self.manager.update_visual_pipeline(
                execution["execution_id"], attempt_id=assignment["attempt_id"], camera_id="camera-1",
                pipeline={"phase": "uploading", "sequence": 0, "phase_started_at": "2026-08-11T00:00:07Z"},
            )

    async def test_visual_failure_observation_waits_for_confirmation(self):
        execution = await self.manager.create(
            "拿起水壶", "test", None, STEPS[:1], execution_mode="visual_monitor"
        )
        started = await self.manager.start(execution["execution_id"])
        self.assertEqual(started["execution_mode"], "visual_monitor")
        self.assertNotIn("workflow_preview", started["active_attempt"])
        assignment = await self.manager.claim_visual_monitor("camera-1")
        self.assertEqual(assignment["attempt_id"], started["active_attempt"]["attempt_id"])
        await self.manager.update_visual_monitor(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", patch={"state": "ready"},
            event_type="visual_monitor.baseline.ready",
        )
        await self.manager.begin_visual_checkpoint(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", sequence=1,
        )
        snapshot = await self.manager.update_visual_monitor(
            execution["execution_id"],
            attempt_id=assignment["attempt_id"],
            camera_id="camera-1",
            patch={"state": "awaiting_confirmation", "latest": {"status": "failed"}},
            event_type="visual_monitor.observation",
        )
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["steps"][0]["status"], "active")
        self.assertEqual(snapshot["active_attempt"]["visual_monitor"]["state"], "awaiting_confirmation")
        with self.assertRaises(ExecutionConflictError):
            await self.manager.begin_visual_checkpoint(
                execution["execution_id"], attempt_id=assignment["attempt_id"],
                camera_id="camera-1", sequence=2,
            )
        resumed = await self.manager.resume_visual_monitor(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
        )
        monitor = resumed["active_attempt"]["visual_monitor"]
        self.assertEqual(monitor["state"], "awaiting_baseline")
        self.assertEqual(monitor["monitor_epoch"], 2)
        self.assertIsNone(monitor["latest"])
        reclaimed = await self.manager.claim_visual_monitor("camera-1")
        self.assertEqual(reclaimed["monitor_epoch"], 2)

    async def test_visual_success_atomically_advances_and_rejects_stale_repeat(self):
        execution = await self.manager.create(
            "拿起并搬运水壶", "test", None, CHAIN_STEPS[:2],
            execution_mode="visual_monitor",
        )
        await self.manager.start(execution["execution_id"])
        assignment = await self.manager.claim_visual_monitor("camera-1")
        await self.manager.update_visual_monitor(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", patch={"state": "ready"},
            event_type="visual_monitor.baseline.ready",
        )
        await self.manager.begin_visual_checkpoint(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", sequence=1,
        )
        latest = {
            "status": "succeeded", "sequence": 1,
            "description_zh": "水壶在窗口结尾仍被拿起",
        }
        snapshot = await self.manager.complete_visual_success(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", latest=latest,
        )
        self.assertEqual(snapshot["steps"][0]["status"], "succeeded")
        self.assertEqual(snapshot["steps"][0]["attempts"][-1]["source"], "visual_monitor")
        self.assertEqual(
            snapshot["steps"][0]["attempts"][-1]["visual_monitor"]["latest"], latest,
        )
        self.assertEqual(snapshot["current_step_index"], 1)
        self.assertEqual(snapshot["active_attempt"]["status"], "waiting")
        with self.assertRaises(ExecutionConflictError):
            await self.manager.complete_visual_success(
                execution["execution_id"], attempt_id=assignment["attempt_id"],
                camera_id="camera-1", latest=latest,
            )
        next_assignment = await self.manager.claim_visual_monitor("camera-1")
        self.assertEqual(next_assignment["action_id"], "A_003")

    async def test_only_one_visual_inference_can_be_reserved(self):
        execution = await self.manager.create(
            "拿起水壶", "test", None, STEPS[:1], execution_mode="visual_monitor"
        )
        await self.manager.start(execution["execution_id"])
        assignment = await self.manager.claim_visual_monitor("camera-1")
        await self.manager.update_visual_monitor(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", patch={"state": "ready"},
            event_type="visual_monitor.baseline.ready",
        )
        await self.manager.begin_visual_checkpoint(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", sequence=1,
        )
        with self.assertRaises(ExecutionConflictError):
            await self.manager.begin_visual_checkpoint(
                execution["execution_id"], attempt_id=assignment["attempt_id"],
                camera_id="camera-1", sequence=2,
            )
        await self.manager.update_visual_monitor(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", patch={"state": "observing", "latest": {"status": "in_progress"}},
            event_type="visual_monitor.observation",
        )
        await self.manager.begin_visual_checkpoint(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", sequence=3,
        )

    async def test_restart_recovery_does_not_restart_confirmation_timeout(self):
        execution = await self.manager.create(
            "拿起水壶", "test", None, STEPS[:1], execution_mode="visual_monitor"
        )
        await self.manager.start(execution["execution_id"])
        assignment = await self.manager.claim_visual_monitor("camera-1")
        await self.manager.update_visual_monitor(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", patch={"state": "ready"},
            event_type="visual_monitor.baseline.ready",
        )
        await self.manager.begin_visual_checkpoint(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", sequence=1,
        )
        await self.manager.update_visual_monitor(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1",
            patch={"state": "awaiting_confirmation", "latest": {"status": "failed"}},
            event_type="visual_monitor.observation",
        )
        await self.manager.resume_timers()
        self.assertNotIn(execution["execution_id"], self.manager._timers)

    async def test_restart_recovery_releases_orphaned_inference(self):
        execution = await self.manager.create(
            "拿起水壶", "test", None, STEPS[:1], execution_mode="visual_monitor"
        )
        await self.manager.start(execution["execution_id"])
        assignment = await self.manager.claim_visual_monitor("camera-1")
        await self.manager.update_visual_monitor(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", patch={"state": "ready"},
            event_type="visual_monitor.baseline.ready",
        )
        await self.manager.begin_visual_checkpoint(
            execution["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="camera-1", sequence=1,
        )
        await self.manager.resume_timers()
        snapshot = await self.manager.get(execution["execution_id"])
        self.assertEqual(snapshot["active_attempt"]["visual_monitor"]["state"], "error")

    async def test_visual_monitor_claims_full_pick_carry_place_chain(self):
        execution = await self.manager.create(
            "拿起并搬运水壶到桌子", "test", None, CHAIN_STEPS,
            execution_mode="visual_monitor",
        )
        snapshot = await self.manager.start(execution["execution_id"])
        previous_attempt_id = None
        expected = [("A_001", 0), ("A_003", 0), ("A_002", 1)]
        for index, (action_id, logic) in enumerate(expected):
            assignment = await self.manager.claim_visual_monitor("camera-1")
            self.assertEqual((assignment["action_id"], assignment["logic"]), (action_id, logic))
            self.assertIn("contract_key", assignment)
            if previous_attempt_id:
                self.assertNotEqual(assignment["attempt_id"], previous_attempt_id)
            previous_attempt_id = assignment["attempt_id"]
            snapshot = (await self.manager.report(
                execution["execution_id"], report_id=f"chain-{index}",
                step_id=assignment["step_id"], attempt_id=assignment["attempt_id"],
                outcome="success", source="human",
            ))["execution"]
        self.assertEqual(snapshot["state"], "completed")

    async def test_stale_visual_result_cannot_overwrite_next_step(self):
        execution = await self.manager.create(
            "拿起并搬运水壶", "test", None, CHAIN_STEPS[:2],
            execution_mode="visual_monitor",
        )
        await self.manager.start(execution["execution_id"])
        old = await self.manager.claim_visual_monitor("camera-1")
        await self.manager.report(
            execution["execution_id"], report_id="advance", step_id=old["step_id"],
            attempt_id=old["attempt_id"], outcome="success", source="human",
        )
        new = await self.manager.claim_visual_monitor("camera-1")
        with self.assertRaises(ExecutionConflictError):
            await self.manager.update_visual_monitor(
                execution["execution_id"], attempt_id=old["attempt_id"], camera_id="camera-1",
                patch={"latest": {"status": "succeeded"}}, event_type="visual_monitor.observation",
            )
        self.assertEqual(new["action_id"], "A_003")

    async def test_mode_can_only_change_before_start(self):
        execution = await self.manager.create("拿起水壶", "test", None, STEPS[:1])
        changed = await self.manager.set_mode(execution["execution_id"], "visual_monitor")
        self.assertEqual(changed["execution_mode"], "visual_monitor")
        await self.manager.start(execution["execution_id"])
        with self.assertRaises(ExecutionConflictError):
            await self.manager.set_mode(execution["execution_id"], "robot_agent")

    async def resolve(self, execution, outcome, report_id=None):
        step = execution["steps"][execution["current_step_index"]]
        attempt = execution["active_attempt"]
        result = await self.manager.report(
            execution["execution_id"],
            report_id=report_id or str(uuid4()),
            step_id=step["step_id"],
            attempt_id=attempt["attempt_id"],
            outcome=outcome,
            source="human",
        )
        return result

    async def register(self, manager=None, workflow=None):
        manager = manager or self.manager
        return await manager.register_workflow(
            robot_id="x5-arm-grasp",
            agent_instance_id="agent-1",
            workflow=workflow or WORKFLOW,
        )

    async def test_success_advances_and_completes(self):
        execution = await self.create_and_start()
        execution = (await self.resolve(execution, "success"))["execution"]
        self.assertEqual(execution["current_step_index"], 1)
        self.assertEqual(execution["steps"][0]["status"], "succeeded")
        self.assertEqual(execution["active_attempt"]["attempt_no"], 1)

        execution = (await self.resolve(execution, "success"))["execution"]
        self.assertEqual(execution["state"], "completed")
        self.assertEqual(execution["progress"], {"succeeded": 2, "total": 2, "ratio": 1.0})

    async def test_three_failures_pause_then_manual_attempt_is_single(self):
        execution = await self.manager.create("把水壶放到桌子上", "test", None, STEPS[1:])
        execution = await self.manager.start(execution["execution_id"])
        for expected_attempt in (2, 3):
            execution = (await self.resolve(execution, "failure"))["execution"]
            self.assertEqual(execution["state"], "running")
            self.assertEqual(execution["active_attempt"]["attempt_no"], expected_attempt)

        execution = (await self.resolve(execution, "failure"))["execution"]
        self.assertEqual(execution["state"], "paused")
        self.assertEqual(execution["steps"][0]["status"], "blocked")

        execution = await self.manager.retry(execution["execution_id"])
        self.assertTrue(execution["active_attempt"]["manual_retry"])
        execution = (await self.resolve(execution, "failure"))["execution"]
        self.assertEqual(execution["state"], "paused")
        self.assertEqual(len(execution["steps"][0]["attempts"]), 4)

    async def test_timeout_uses_same_retry_policy(self):
        manager = ExecutionManager(timeout_seconds=0.015, db_path="")
        try:
            execution = await manager.create("测试超时", "test", None, STEPS[1:])
            execution = await manager.start(execution["execution_id"])
            for _ in range(50):
                await asyncio.sleep(0.01)
                execution = await manager.get(execution["execution_id"])
                if execution["state"] == "paused":
                    break
            self.assertEqual(execution["state"], "paused")
            self.assertEqual(
                [attempt["status"] for attempt in execution["steps"][0]["attempts"]],
                ["timeout", "timeout", "timeout"],
            )
        finally:
            await manager.close()

    async def test_pick_claim_uses_registered_dynamic_snapshot_and_no_auto_retry(self):
        execution = await self.manager.create("拿起水壶", "test", None, STEPS[:1])
        execution = await self.manager.start(execution["execution_id"])
        self.assertIsNone(
            await self.manager.claim_pick("x5-arm-grasp", "agent-1")
        )
        await self.register()
        preview = await self.manager.get(execution["execution_id"])
        self.assertEqual(
            preview["active_attempt"]["workflow_preview"]["state"],
            "registered",
        )
        self.assertEqual(
            len(preview["active_attempt"]["workflow_preview"]["nodes"]),
            4,
        )
        assignment = await self.manager.claim_pick("x5-arm-grasp", "agent-1")
        self.assertEqual(assignment["attempt_id"], execution["active_attempt"]["attempt_id"])
        self.assertEqual(assignment["workflow_digest"], "a" * 64)
        self.assertEqual(
            [node["node_id"] for node in assignment["nodes"]],
            ["controller", "reset", "auto_pick", "discover"],
        )

        common = {
            "execution_id": execution["execution_id"],
            "attempt_id": assignment["attempt_id"],
            "run_id": assignment["run_id"],
            "robot_id": "x5-arm-grasp",
        }
        result = await self.manager.workflow_event(
            **common,
            event_id="pass-1",
            sequence=1,
            node_id="controller",
            state="passed",
            marker="ARM_CONTROLLER_READY",
        )
        attempt = result["execution"]["active_attempt"]
        self.assertIsNone(attempt["deadline_at"])
        self.assertEqual(attempt["workflow"]["state"], "running")

        changed = {
            **WORKFLOW,
            "version": "4",
            "digest": "b" * 64,
            "nodes": WORKFLOW["nodes"][:2],
        }
        await self.register(workflow=changed)
        snapshot = await self.manager.get(execution["execution_id"])
        self.assertEqual(
            snapshot["active_attempt"]["workflow"]["version"],
            "3",
        )
        self.assertEqual(
            len(snapshot["active_attempt"]["workflow"]["nodes"]),
            4,
        )

        result = await self.manager.workflow_event(
            **common,
            event_id="fail-2",
            sequence=2,
            node_id="reset",
            state="failed",
            exit_code=1,
            message="validation failed",
        )
        self.assertEqual(
            result["execution"]["active_attempt"]["workflow"]["state"],
            "failed",
        )
        completed = await self.manager.complete_workflow(
            **common,
            completion_id="complete-1",
            outcome="failed",
            message="reset readiness check failed",
        )
        execution = completed["execution"]
        self.assertEqual(execution["state"], "paused")
        self.assertEqual(len(execution["steps"][0]["attempts"]), 1)
        duplicate = await self.manager.complete_workflow(
            **common,
            completion_id="complete-1",
            outcome="failed",
            message="duplicate",
        )
        self.assertTrue(duplicate["duplicate"])

    async def test_workflow_registration_rejects_bad_digest_and_cycle(self):
        bad_digest = copy.deepcopy(WORKFLOW)
        bad_digest["digest"] = "not-a-digest"
        with self.assertRaises(ExecutionConflictError):
            await self.register(workflow=bad_digest)

        cyclic = copy.deepcopy(WORKFLOW)
        cyclic["nodes"][0]["depends_on"] = ["auto_pick"]
        with self.assertRaises(ExecutionConflictError):
            await self.register(workflow=cyclic)

    async def test_agent_stale_marks_the_active_workflow_node_failed(self):
        manager = ExecutionManager(
            timeout_seconds=1,
            agent_stale_seconds=0.02,
            db_path="",
        )
        try:
            execution = await manager.create("拿起水壶", "test", None, STEPS[:1])
            execution = await manager.start(execution["execution_id"])
            await self.register(manager=manager)
            assignment = await manager.claim_pick("x5-arm-grasp", "agent-1")
            await manager.workflow_event(
                execution["execution_id"],
                event_id="wait-stale",
                attempt_id=assignment["attempt_id"],
                run_id=assignment["run_id"],
                robot_id="x5-arm-grasp",
                sequence=1,
                node_id="controller",
                state="running",
            )
            await asyncio.sleep(0.05)
            snapshot = await manager.get(execution["execution_id"])
            workflow = snapshot["steps"][0]["attempts"][0]["workflow"]
            wait_node = next(
                node for node in workflow["nodes"] if node["node_id"] == "controller"
            )
            self.assertEqual(snapshot["state"], "paused")
            self.assertEqual(wait_node["state"], "failed")
            self.assertIn("heartbeat", wait_node["message"])
        finally:
            await manager.close()

    async def test_duplicate_is_idempotent_and_stale_attempt_is_rejected(self):
        execution = await self.create_and_start()
        old_step = execution["steps"][0]
        old_attempt = execution["active_attempt"]
        accepted = await self.resolve(execution, "success", report_id="same-report")

        duplicate = await self.manager.report(
            execution["execution_id"],
            report_id="same-report",
            step_id=old_step["step_id"],
            attempt_id=old_attempt["attempt_id"],
            outcome="success",
            source="human",
        )
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["execution"]["version"], accepted["execution"]["version"])

        with self.assertRaises(ExecutionConflictError):
            await self.manager.report(
                execution["execution_id"],
                report_id="late-report",
                step_id=old_step["step_id"],
                attempt_id=old_attempt["attempt_id"],
                outcome="failure",
                source="robot",
            )
        snapshot = await self.manager.get(execution["execution_id"])
        self.assertEqual(snapshot["events"][-1]["type"], "report.rejected")
        self.assertEqual(snapshot["current_step_index"], 1)

    async def test_terminate_and_initial_subscription_snapshot(self):
        execution = await self.create_and_start()
        queue = await self.manager.subscribe(execution["execution_id"])
        initial = queue.get_nowait()
        self.assertEqual(initial["type"], "snapshot")
        self.assertEqual(initial["snapshot"]["state"], "running")
        self.manager.unsubscribe(execution["execution_id"], queue)

        execution = await self.manager.terminate(execution["execution_id"])
        self.assertEqual(execution["state"], "terminated")
        self.assertEqual(execution["steps"][0]["attempts"][0]["status"], "cancelled")

    async def test_sqlite_store_restores_execution_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "executions.db")
            first = ExecutionManager(timeout_seconds=1, db_path=path)
            execution = await first.create("拿起水壶", "test", None, STEPS[:1])
            await self.register(manager=first)
            await first.close()

            second = ExecutionManager(timeout_seconds=1, db_path=path)
            try:
                restored = await second.get(execution["execution_id"])
                self.assertEqual(restored["instruction"], "拿起水壶")
                self.assertEqual(restored["state"], "ready")
                registered = second.registered_workflow(
                    "x5-arm-grasp", "A_001"
                )
                self.assertEqual(registered["digest"], "a" * 64)
                mode = second._store.connection.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(mode.lower(), "wal")
            finally:
                await second.close()


if __name__ == "__main__":
    unittest.main()
