import os
import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import server
from execution import ExecutionManager
from realtime_monitor import VisualMonitorConfig, VisualMonitorService


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

WORKFLOW = {
    "workflow_id": "grasparm.auto-pick",
    "version": "3",
    "digest": "c" * 64,
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


class ExecutionApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {
            "OPERATOR_TOKEN": "",
            "GRASPARM_AGENT_TOKEN": "",
            "VISUAL_MONITOR_TOKEN": "",
            "EXECUTION_DB_PATH": "",
        })
        self.environment.start()
        self.previous_manager = server.execution_manager
        server.execution_manager = ExecutionManager(
            timeout_seconds=1,
            db_path="",
        )
        transport = httpx.ASGITransport(app=server.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await server.execution_manager.close()
        server.execution_manager = self.previous_manager
        self.environment.stop()

    async def test_create_start_report_and_get(self):
        with patch("server.decompose", return_value=PLANNER_RESULT):
            response = await self.client.post("/api/executions", json={
                "instruction": "拿起杯子",
                "provider": "deepseek",
            })
        self.assertEqual(response.status_code, 200)
        execution = response.json()["execution"]
        self.assertEqual(execution["state"], "ready")

        response = await self.client.post(
            f"/api/executions/{execution['execution_id']}/mode",
            json={"mode": "visual_monitor"},
        )
        self.assertEqual(response.status_code, 200)
        execution = response.json()["execution"]
        self.assertEqual(execution["execution_mode"], "visual_monitor")

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

    async def test_app_shell_and_script_disable_stale_browser_cache(self):
        for path in ("/", "/index.html", "/app.js"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("cache-control"), "no-store")
        index = (await self.client.get("/")).text
        self.assertIn("/app.js?v=monitor-result-log-20260811", index)
        script = (await self.client.get("/app.js")).text
        self.assertNotIn("人工确认成功", script)
        self.assertIn("VLM 判定成功后将自动进入下一步骤", script)
        self.assertIn("mode-chain_visual_monitor", script)
        self.assertIn("live-preview-model", script)
        self.assertIn("pipeline-panel", script)
        self.assertIn("function patchExecution()", script)
        self.assertIn("oldPreview", script)
        self.assertIn('["running", "completed"].includes(execution.state)', script)
        self.assertIn("function visualEvents()", script)
        self.assertIn("VLM 判断日志", script)
        self.assertIn("成功证据", script)
        self.assertNotIn('class="timing-grid"', script)

    async def test_chain_visual_monitor_mode_is_selectable_before_start(self):
        with patch("server.decompose", return_value=PLANNER_RESULT):
            execution = (await self.client.post("/api/executions", json={
                "instruction": "拿起杯子", "provider": "deepseek",
            })).json()["execution"]
        response = await self.client.post(
            f"/api/executions/{execution['execution_id']}/mode",
            json={"mode": "chain_visual_monitor"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["execution"]["execution_mode"], "chain_visual_monitor")

    async def test_visual_pipeline_telemetry_reaches_execution_snapshot(self):
        with patch("server.decompose", return_value=PLANNER_RESULT):
            execution = (await self.client.post("/api/executions", json={
                "instruction": "拿起杯子", "provider": "deepseek",
            })).json()["execution"]
        await self.client.post(
            f"/api/executions/{execution['execution_id']}/mode",
            json={"mode": "chain_visual_monitor"},
        )
        await self.client.post(f"/api/executions/{execution['execution_id']}/start")
        assignment = (await self.client.post(
            "/api/visual-monitor/claim", json={"camera_id": "cam-1"},
        )).json()["assignment"]
        response = await self.client.post("/api/visual-monitor/telemetry", json={
            "execution_id": execution["execution_id"],
            "attempt_id": assignment["attempt_id"], "camera_id": "cam-1",
            "sequence": 1, "phase": "capturing",
            "phase_started_at": "2026-08-11T00:00:00Z",
            "window_started_at": "2026-08-11T00:00:00Z",
            "window_ended_at": "2026-08-11T00:00:06Z",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["execution"]["chain_visual_monitor"]["pipeline"]["phase"],
            "capturing",
        )

    async def test_visual_monitor_upload_updates_snapshot_without_advancing(self):
        with patch("server.decompose", return_value=PLANNER_RESULT):
            created = (await self.client.post("/api/executions", json={
                "instruction": "拿起杯子", "provider": "deepseek",
            })).json()["execution"]
        await self.client.post(f"/api/executions/{created['execution_id']}/mode", json={"mode": "visual_monitor"})
        started = (await self.client.post(f"/api/executions/{created['execution_id']}/start")).json()["execution"]
        attempt_id = started["active_attempt"]["attempt_id"]
        temporary = tempfile.TemporaryDirectory()
        previous_service = server.visual_monitor
        previous_baselines = server.visual_baselines
        service = VisualMonitorService(
            server._update_visual_execution,
            VisualMonitorConfig(Path(temporary.name), max_concurrency=2),
        )

        received_call = []

        async def fake_call(*args):
            received_call.append(args)
            return ({
                "status": "in_progress", "description_zh": "手正在接近杯子",
                "failure_reason": None,
                "evidence": [{"timestamp_s": 1.0, "observation": "手靠近杯子"}],
                "completion_evidence_timestamp_s": None,
            }, {"bailian_total": 12.0}, "qwen3.7-plus", "{}")

        service._call_bailian = fake_call
        server.visual_monitor = service
        server.visual_baselines = {}
        try:
            claim = await self.client.post("/api/visual-monitor/claim", json={"camera_id": "cam-1"})
            self.assertEqual(claim.status_code, 200)
            baseline = await self.client.post("/api/visual-monitor/baseline", data={
                "execution_id": created["execution_id"], "attempt_id": attempt_id,
                "camera_id": "cam-1", "captured_at": "2026-08-06T00:00:00Z",
            }, files={"image": ("before.jpg", b"jpeg", "image/jpeg")})
            self.assertEqual(baseline.status_code, 200)
            checkpoint = await self.client.post("/api/visual-monitor/checkpoints", data={
                "execution_id": created["execution_id"], "attempt_id": attempt_id,
                "camera_id": "cam-1", "sequence": "1",
                "window_started_at": "2026-08-06T00:00:00Z",
                "window_ended_at": "2026-08-06T00:00:06Z",
                "capture_ms": "6000", "encode_ms": "80",
                "visual_input_format": "rgb_depth_side_by_side",
                "depth_min_m": "0.25", "depth_max_m": "2.0",
            }, files={
                "video": ("window.mp4", b"mp4", "video/mp4"),
                "now_image": ("now.jpg", b"jpeg-now", "image/jpeg"),
                "now_depth_image": ("now-depth.jpg", b"jpeg-depth", "image/jpeg"),
            })
            self.assertEqual(checkpoint.status_code, 202)
            for _ in range(20):
                if not service.in_flight:
                    break
                await asyncio.sleep(0.01)
            snapshot = (await self.client.get(f"/api/executions/{created['execution_id']}")).json()["execution"]
            self.assertEqual(snapshot["state"], "running")
            self.assertEqual(snapshot["active_attempt"]["visual_monitor"]["latest"]["status"], "in_progress")
            latest = snapshot["active_attempt"]["visual_monitor"]["latest"]
            self.assertEqual(latest["visual_input_format"], "rgb_depth_side_by_side")
            self.assertTrue(latest["now_depth_url"].endswith(".jpg"))
            self.assertEqual(received_call[0][6], "rgb_depth_side_by_side")
            self.assertEqual(received_call[0][7:9], (0.25, 2.0))
        finally:
            await service.close()
            server.visual_monitor = previous_service
            server.visual_baselines = previous_baselines
            temporary.cleanup()

    async def test_visual_monitor_resume_starts_new_epoch_without_advancing(self):
        with patch("server.decompose", return_value=PLANNER_RESULT):
            created = (await self.client.post("/api/executions", json={
                "instruction": "拿起杯子", "provider": "deepseek",
            })).json()["execution"]
        await self.client.post(
            f"/api/executions/{created['execution_id']}/mode",
            json={"mode": "visual_monitor"},
        )
        await self.client.post(f"/api/executions/{created['execution_id']}/start")
        assignment = (await self.client.post(
            "/api/visual-monitor/claim", json={"camera_id": "cam-resume"},
        )).json()["assignment"]
        await server.execution_manager.update_visual_monitor(
            created["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="cam-resume", patch={"state": "ready"},
            event_type="visual_monitor.baseline.ready",
        )
        await server.execution_manager.begin_visual_checkpoint(
            created["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="cam-resume", sequence=1,
        )
        await server.execution_manager.update_visual_monitor(
            created["execution_id"], attempt_id=assignment["attempt_id"],
            camera_id="cam-resume",
            patch={"state": "awaiting_confirmation", "latest": {"status": "failed"}},
            event_type="visual_monitor.observation",
        )
        response = await self.client.post(
            f"/api/executions/{created['execution_id']}/visual-monitor/resume",
            json={"attempt_id": assignment["attempt_id"]},
        )
        self.assertEqual(response.status_code, 200)
        execution = response.json()["execution"]
        self.assertEqual(execution["current_step_index"], 0)
        self.assertEqual(execution["active_attempt"]["attempt_id"], assignment["attempt_id"])
        self.assertEqual(execution["active_attempt"]["visual_monitor"]["monitor_epoch"], 2)
        self.assertEqual(execution["active_attempt"]["visual_monitor"]["state"], "awaiting_baseline")

    async def test_checkpoint_api_rejects_second_request_while_inferencing(self):
        with patch("server.decompose", return_value=PLANNER_RESULT):
            created = (await self.client.post("/api/executions", json={
                "instruction": "拿起杯子", "provider": "deepseek",
            })).json()["execution"]
        await self.client.post(
            f"/api/executions/{created['execution_id']}/mode", json={"mode": "visual_monitor"},
        )
        started = (await self.client.post(
            f"/api/executions/{created['execution_id']}/start",
        )).json()["execution"]
        temporary = tempfile.TemporaryDirectory()
        previous_service = server.visual_monitor
        previous_baselines = server.visual_baselines
        service = VisualMonitorService(
            server._update_visual_execution,
            VisualMonitorConfig(Path(temporary.name), max_concurrency=2),
        )
        release = asyncio.Event()

        async def slow_call(*args):
            await release.wait()
            return ({
                "status": "in_progress", "description_zh": "仍在执行",
                "failure_reason": None,
                "evidence": [{"timestamp_s": 1.0, "observation": "手正在接近杯子"}],
                "completion_evidence_timestamp_s": None,
            }, {"bailian_total": 10.0}, "qwen3.7-plus", "{}")

        service._call_bailian = slow_call
        server.visual_monitor = service
        server.visual_baselines = {}
        attempt_id = started["active_attempt"]["attempt_id"]
        try:
            await self.client.post("/api/visual-monitor/claim", json={"camera_id": "cam-one"})
            baseline = await self.client.post("/api/visual-monitor/baseline", data={
                "execution_id": created["execution_id"], "attempt_id": attempt_id,
                "camera_id": "cam-one", "captured_at": "2026-08-10T00:00:00Z",
            }, files={"image": ("before.jpg", b"jpeg", "image/jpeg")})
            self.assertEqual(baseline.status_code, 200)
            form = {
                "execution_id": created["execution_id"], "attempt_id": attempt_id,
                "camera_id": "cam-one", "window_started_at": "2026-08-10T00:00:00Z",
                "window_ended_at": "2026-08-10T00:00:06Z", "capture_ms": "6000",
                "encode_ms": "80",
            }
            first = await self.client.post(
                "/api/visual-monitor/checkpoints", data={**form, "sequence": "1"},
                files={
                    "video": ("one.mp4", b"mp4", "video/mp4"),
                    "now_image": ("now-one.jpg", b"jpeg-now", "image/jpeg"),
                },
            )
            second = await self.client.post(
                "/api/visual-monitor/checkpoints", data={**form, "sequence": "2"},
                files={
                    "video": ("two.mp4", b"mp4", "video/mp4"),
                    "now_image": ("now-two.jpg", b"jpeg-now", "image/jpeg"),
                },
            )
            self.assertEqual(first.status_code, 202)
            self.assertEqual(second.status_code, 409)
            self.assertIn("已有推理请求", second.json()["detail"])
        finally:
            release.set()
            for _ in range(50):
                if not service.in_flight:
                    break
                await asyncio.sleep(0.01)
            await service.close()
            server.visual_monitor = previous_service
            server.visual_baselines = previous_baselines
            temporary.cleanup()

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

    async def register_agent(self, headers=None):
        return await self.client.post(
            "/api/agent/workflows/register",
            json={
                "robot_id": "x5-arm-grasp",
                "agent_instance_id": "agent-1",
                "workflow": WORKFLOW,
            },
            headers=headers,
        )

    async def test_agent_register_claim_complete_and_failure_are_visible(self):
        registered = await self.register_agent()
        self.assertEqual(registered.status_code, 200)
        self.assertNotIn(
            "command",
            registered.json()["workflow"]["nodes"][0],
        )
        discover = next(
            node
            for node in registered.json()["workflow"]["nodes"]
            if node["node_id"] == "discover"
        )
        self.assertEqual(
            discover["start_after"]["node_id"],
            "auto_pick",
        )
        with patch("server.decompose", return_value=PLANNER_RESULT):
            execution = (await self.client.post("/api/executions", json={
                "instruction": "拿起杯子",
                "provider": "deepseek",
            })).json()["execution"]
        execution = (await self.client.post(
            f"/api/executions/{execution['execution_id']}/start"
        )).json()["execution"]

        assignment = (await self.client.post(
            "/api/agent/claim",
            json={
                "robot_id": "x5-arm-grasp",
                "agent_instance_id": "agent-1",
            },
        )).json()["assignment"]
        self.assertEqual(assignment["attempt_id"], execution["active_attempt"]["attempt_id"])
        self.assertEqual(assignment["workflow_digest"], "c" * 64)

        base = {
            "execution_id": execution["execution_id"],
            "attempt_id": assignment["attempt_id"],
            "run_id": assignment["run_id"],
            "robot_id": "x5-arm-grasp",
        }
        response = await self.client.post("/api/agent/events", json={
            **base,
            "event_id": "event-1",
            "sequence": 1,
            "node_id": "controller",
            "state": "passed",
            "marker": "ARM_CONTROLLER_READY",
        })
        workflow = response.json()["execution"]["active_attempt"]["workflow"]
        self.assertEqual(workflow["state"], "running")

        response = await self.client.post("/api/agent/events", json={
            **base,
            "event_id": "event-2",
            "sequence": 2,
            "node_id": "reset",
            "state": "failed",
            "exit_code": 1,
            "message": "ARM_READY_CHECK_FAILED",
            "log_tail": "reset readiness timeout",
        })
        self.assertEqual(
            response.json()["execution"]["active_attempt"]["workflow"]["state"],
            "failed",
        )
        response = await self.client.post("/api/agent/complete", json={
            **base,
            "completion_id": "complete-1",
            "outcome": "needs_operator",
            "message": "physical state needs inspection",
        })
        self.assertEqual(response.json()["execution"]["state"], "paused")
        node = next(
            item
            for item in response.json()["execution"]["steps"][0]["attempts"][0]["workflow"]["nodes"]
            if item["node_id"] == "reset"
        )
        self.assertEqual(node["state"], "failed")
        self.assertEqual(node["log_tail"], "reset readiness timeout")
        self.assertEqual(
            response.json()["execution"]["steps"][0]["attempts"][0]["workflow"]["state"],
            "needs_operator",
        )

    async def test_dynamic_four_node_agent_success_completes_pick(self):
        await self.register_agent()
        with patch("server.decompose", return_value=PLANNER_RESULT):
            execution = (await self.client.post("/api/executions", json={
                "instruction": "拿起杯子",
                "provider": "deepseek",
            })).json()["execution"]
        execution = (await self.client.post(
            f"/api/executions/{execution['execution_id']}/start"
        )).json()["execution"]
        assignment = (await self.client.post("/api/agent/claim", json={
            "robot_id": "x5-arm-grasp",
            "agent_instance_id": "agent-1",
        })).json()["assignment"]
        base = {
            "execution_id": execution["execution_id"],
            "attempt_id": assignment["attempt_id"],
            "run_id": assignment["run_id"],
            "robot_id": "x5-arm-grasp",
        }
        for sequence, (node_id, marker) in enumerate((
            ("controller", "ARM_CONTROLLER_READY"),
            ("reset", "RESET_COMPLETE"),
            ("discover", "YOLOE_DISCOVER_COMPLETE"),
            ("auto_pick", "AUTO_PICK_COMPLETE"),
        ), start=1):
            response = await self.client.post("/api/agent/events", json={
                **base,
                "event_id": f"pass-{sequence}",
                "sequence": sequence,
                "node_id": node_id,
                "state": "passed",
                "marker": marker,
            })
            self.assertEqual(response.status_code, 200)
        response = await self.client.post("/api/agent/complete", json={
            **base,
            "completion_id": "success-complete",
            "outcome": "succeeded",
            "message": "all markers observed",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["execution"]["state"], "completed")

    async def test_operator_and_agent_tokens_are_independent(self):
        with patch("server.decompose", return_value=PLANNER_RESULT):
            execution = (await self.client.post("/api/executions", json={
                "instruction": "拿起杯子",
                "provider": "deepseek",
            })).json()["execution"]
        with patch.dict(
            "os.environ",
            {"OPERATOR_TOKEN": "operator-secret", "GRASPARM_AGENT_TOKEN": "agent-secret"},
        ):
            denied = await self.client.post(
                f"/api/executions/{execution['execution_id']}/start"
            )
            self.assertEqual(denied.status_code, 401)
            started = await self.client.post(
                f"/api/executions/{execution['execution_id']}/start",
                headers={"X-Operator-Token": "operator-secret"},
            )
            self.assertEqual(started.status_code, 200)

            denied_claim = await self.client.post(
                "/api/agent/claim",
                json={
                    "robot_id": "x5-arm-grasp",
                    "agent_instance_id": "agent-1",
                },
                headers={"Authorization": "Bearer operator-secret"},
            )
            self.assertEqual(denied_claim.status_code, 401)
            registered = await self.register_agent(
                headers={"Authorization": "Bearer agent-secret"}
            )
            self.assertEqual(registered.status_code, 200)
            claim = await self.client.post(
                "/api/agent/claim",
                json={
                    "robot_id": "x5-arm-grasp",
                    "agent_instance_id": "agent-1",
                },
                headers={"Authorization": "Bearer agent-secret"},
            )
            self.assertIsNotNone(claim.json()["assignment"])


if __name__ == "__main__":
    unittest.main()
