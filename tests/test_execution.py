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
