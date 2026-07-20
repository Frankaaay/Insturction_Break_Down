import asyncio
import unittest
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


class ExecutionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ExecutionManager(timeout_seconds=1)

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
        execution = await self.create_and_start()
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
        manager = ExecutionManager(timeout_seconds=0.015)
        try:
            execution = await manager.create("测试超时", "test", None, STEPS[:1])
            execution = await manager.start(execution["execution_id"])
            await asyncio.sleep(0.08)
            execution = await manager.get(execution["execution_id"])
            self.assertEqual(execution["state"], "paused")
            self.assertEqual(
                [attempt["status"] for attempt in execution["steps"][0]["attempts"]],
                ["timeout", "timeout", "timeout"],
            )
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


if __name__ == "__main__":
    unittest.main()
