import unittest
from unittest.mock import patch

import httpx

import server
from execution import ExecutionManager


PLANNER_RESULT = {
    "status": "ok",
    "steps": [
        {
            "action_id": "A_001",
            "action": "Pick",
            "logic": 0,
            "slots": {"obj_a": "杯子"},
            "zh": "拿起杯子",
            "en": "Pick up the cup.",
        }
    ],
}


class ExecutionApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_manager = server.execution_manager
        server.execution_manager = ExecutionManager(timeout_seconds=1)
        transport = httpx.ASGITransport(app=server.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await server.execution_manager.close()
        server.execution_manager = self.previous_manager

    async def test_create_start_report_and_get(self):
        with patch("server.decompose", return_value=PLANNER_RESULT):
            response = await self.client.post("/api/executions", json={
                "instruction": "拿起杯子",
                "provider": "deepseek",
            })
        self.assertEqual(response.status_code, 200)
        execution = response.json()["execution"]
        self.assertEqual(execution["state"], "ready")

        response = await self.client.post(f"/api/executions/{execution['execution_id']}/start")
        execution = response.json()["execution"]
        self.assertEqual(execution["state"], "running")

        response = await self.client.post(
            f"/api/executions/{execution['execution_id']}/reports",
            json={
                "report_id": "api-report-1",
                "step_id": execution["steps"][0]["step_id"],
                "attempt_id": execution["active_attempt"]["attempt_id"],
                "outcome": "success",
                "source": "robot",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["execution"]["state"], "completed")

        response = await self.client.get(f"/api/executions/{execution['execution_id']}")
        self.assertEqual(response.json()["execution"]["progress"]["succeeded"], 1)

    async def test_stale_report_returns_409_and_missing_session_returns_404(self):
        with patch("server.decompose", return_value=PLANNER_RESULT):
            execution = (await self.client.post("/api/executions", json={
                "instruction": "拿起杯子",
                "provider": "deepseek",
            })).json()["execution"]
        execution = (await self.client.post(
            f"/api/executions/{execution['execution_id']}/start"
        )).json()["execution"]

        response = await self.client.post(
            f"/api/executions/{execution['execution_id']}/reports",
            json={
                "report_id": "stale",
                "step_id": execution["steps"][0]["step_id"],
                "attempt_id": "wrong-attempt",
                "outcome": "failure",
                "source": "human",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("已过期", response.json()["detail"])

        response = await self.client.get("/api/executions/not-found")
        self.assertEqual(response.status_code, 404)

    async def test_ambiguous_result_does_not_create_session_and_old_api_remains(self):
        ambiguous = {"status": "ambiguous", "reason": "缺少目标位置"}
        with patch("server.decompose", return_value=ambiguous):
            response = await self.client.post("/api/executions", json={
                "instruction": "把杯子放那里",
                "provider": "deepseek",
            })
            legacy = await self.client.post("/api/decompose", json={
                "instruction": "把杯子放那里",
                "provider": "deepseek",
            })
        self.assertEqual(response.json()["status"], "ambiguous")
        self.assertNotIn("execution", response.json())
        self.assertEqual(legacy.json()["status"], "ambiguous")


if __name__ == "__main__":
    unittest.main()
