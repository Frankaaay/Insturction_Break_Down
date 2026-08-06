import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_recorded_video_monitor as monitor


def packet(strategy="native_video"):
    value = {
        "strategy": strategy,
        "visible_ranges_s": [[0.0, 0.0], [3.0, 8.0]],
        "frames": [
            {"timestamp_s": 0.0},
            {"timestamp_s": 3.0},
            {"timestamp_s": 8.0},
        ],
    }
    return value


def output(status="in_progress", failure_reason=None, completion=None, timestamp=8.0):
    return {
        "status": status,
        "description_zh": "水壶仍在被拿起",
        "failure_reason": failure_reason,
        "evidence": [{"timestamp_s": timestamp, "observation": "水壶仍与手接触"}],
        "completion_evidence_timestamp_s": completion,
    }


class OutputContractTests(unittest.TestCase):
    def test_schema_has_only_requested_public_fields(self):
        self.assertEqual(set(monitor.OUTPUT_SCHEMA["properties"]), monitor.REQUIRED_OUTPUT_KEYS)
        for removed in ("decision", "recoverability", "reason_code", "confidence"):
            self.assertNotIn(removed, monitor.OUTPUT_SCHEMA["properties"])

    def test_failed_accepts_free_text_reason(self):
        result = output(
            status="failed",
            failure_reason="手拿起了旁边的杯子，水壶仍留在桌面上",
            timestamp=7.5,
        )
        monitor.validate_model_output(result, packet())

    def test_non_failed_requires_null_failure_reason(self):
        result = output(failure_reason="发生过滑脱")
        with self.assertRaisesRegex(monitor.ContractError, "failure_reason must be null"):
            monitor.validate_model_output(result, packet())

    def test_succeeded_requires_visible_completion_timestamp(self):
        monitor.validate_model_output(output("succeeded", completion=7.5, timestamp=7.5), packet())
        with self.assertRaisesRegex(monitor.ContractError, "outside sent visual evidence"):
            monitor.validate_model_output(output("succeeded", completion=9.0), packet())

    def test_frame_packet_timestamps_must_match_sent_frames(self):
        with self.assertRaisesRegex(monitor.ContractError, "outside sent visual evidence"):
            monitor.validate_model_output(output(timestamp=6.0), packet("frame_packet"))

    def test_frame_packet_accepts_requested_final_timestamp_after_decode_fallback(self):
        value = packet("frame_packet")
        value["frames"][-1]["timestamp_s"] = 7.73
        value["frames"][-1]["requested_timestamp_s"] = 7.83
        monitor.validate_model_output(output(timestamp=7.83), value)

    def test_none_reasoning_is_explicitly_disabled(self):
        case = {
            "atomic_action": "拿起水壶",
            "object": "水壶",
            "target": "离开桌面",
            "initial_state": "水壶在桌面上",
            "success_criteria": ["水壶离开桌面"],
            "timeout_s": 15,
        }
        evidence = {
            "strategy": "frame_packet",
            "frames": [],
            "visible_ranges_s": [[0.0, 0.0]],
        }
        payload, _ = monitor.build_payload(
            "model", "prompt", case, {"time_s": 4}, evidence, "none", 300, "none", True
        )
        self.assertEqual(payload["reasoning"], {"enabled": False})

    def test_bailian_non_thinking_uses_native_switch_and_json_schema(self):
        case = {
            "atomic_action": "拿起水壶",
            "object": "水壶",
            "target": "离开桌面",
            "initial_state": "水壶在桌面上",
            "success_criteria": ["水壶离开桌面"],
            "timeout_s": 15,
        }
        evidence = {
            "strategy": "frame_packet",
            "frames": [],
            "visible_ranges_s": [[0.0, 0.0]],
        }
        payload, _ = monitor.build_payload(
            "qwen3.6-flash",
            "prompt",
            case,
            {"time_s": 4},
            evidence,
            "none",
            300,
            "none",
            True,
            "bailian",
        )
        self.assertIs(payload["enable_thinking"], False)
        self.assertNotIn("reasoning", payload)
        self.assertEqual(payload["response_format"]["type"], "json_schema")


class ProviderConfigTests(unittest.TestCase):
    def test_bailian_defaults_to_qwen37_plus(self):
        self.assertEqual(monitor.DEFAULT_PROVIDER, "bailian")
        self.assertEqual(monitor.DEFAULT_STRATEGIES, ["native_video"])
        self.assertEqual(
            monitor.DEFAULT_MODELS_BY_PROVIDER["bailian"],
            ["qwen3.7-plus"],
        )
        self.assertIn("dashscope.aliyuncs.com", monitor.BAILIAN_URL)

    def test_bailian_performance_candidates_are_available(self):
        candidates = monitor.ALL_CANDIDATE_MODELS_BY_PROVIDER["bailian"]
        self.assertIn("qwen3.6-plus", candidates)
        self.assertIn("stepfun/step-3.7-flash", candidates)
        self.assertIn("qwen3.5-omni-plus", candidates)


class CheckpointTests(unittest.TestCase):
    def test_short_video_gets_final_checkpoint(self):
        self.assertEqual(monitor.make_checkpoints(2.5, 4.0), [{"time_s": 2.5}])

    def test_non_multiple_duration_gets_periodic_and_final_checkpoints(self):
        checkpoints = monitor.make_checkpoints(10.5, 4.0)
        self.assertEqual([item["time_s"] for item in checkpoints], [4.0, 8.0, 10.5])

    def test_annotations_merge_with_generated_checkpoints(self):
        checkpoints = monitor.make_checkpoints(
            8.0,
            4.0,
            [{"time_s": 4, "expected_status": "in_progress"}],
        )
        self.assertEqual(checkpoints[0]["expected_status"], "in_progress")
        self.assertEqual(checkpoints[-1]["time_s"], 8.0)


class FfmpegAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg = monitor.resolve_ffmpeg("")
        except RuntimeError as exc:
            raise unittest.SkipTest(str(exc))

    def test_both_strategies_prepare_causal_evidence_at_video_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "sample.mp4"
            subprocess.run(
                [
                    self.ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=320x240:r=10:d=2.5",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(video),
                ],
                check=True,
            )
            duration = monitor.probe_video_duration(self.ffmpeg, video)
            frames = monitor.prepare_frame_packet(
                self.ffmpeg, video, duration, root / "frames", 4.0, 8, 640, 4
            )
            native = monitor.prepare_native_video(
                self.ffmpeg,
                video,
                duration,
                root / "native",
                4.0,
                1.0,
                6.0,
                28,
                640,
                4,
                1.5,
            )
            self.assertGreaterEqual(len(frames["frames"]), 2)
            self.assertLessEqual(max(item["timestamp_s"] for item in frames["frames"]), duration)
            self.assertTrue(Path(native["video"]["path"]).is_file())
            self.assertLessEqual(native["video"]["bytes"], 1.5 * 1024 * 1024)

            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cases": [
                            {
                                "id": "synthetic_pick",
                                "video": video.name,
                                "atomic_action": "拿起水壶",
                                "object": "水壶",
                                "target": "水壶离开桌面",
                                "initial_state": "水壶在桌面上",
                                "success_criteria": ["水壶明显离开桌面"],
                                "timeout_s": 20,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output_root = root / "cli_output"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "run_recorded_video_monitor.py"),
                    "--manifest",
                    str(manifest_path),
                    "--output-root",
                    str(output_root),
                    "--dry-run",
                    "--max-checkpoints",
                    "2",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            records = json.loads((output_root / "records.json").read_text(encoding="utf-8"))["records"]
            self.assertEqual(len(records), 1)
            self.assertEqual({item["strategy"] for item in records}, {"native_video"})
            self.assertEqual(
                {item["model_requested"] for item in records},
                {"qwen3.7-plus"},
            )
            self.assertTrue(all(item["provider_requested"] == "bailian" for item in records))
            self.assertTrue(all(item["request_status"] == "dry_run" for item in records))


class ManifestTests(unittest.TestCase):
    def test_relative_video_resolves_from_manifest_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "pick_kettle.mp4"
            video.write_bytes(b"not decoded in this unit test")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cases": [
                            {
                                "id": "pick_kettle",
                                "video": video.name,
                                "atomic_action": "拿起水壶",
                                "object": "水壶",
                                "target": "离开桌面",
                                "initial_state": "水壶在桌面上",
                                "success_criteria": ["水壶离开桌面"],
                                "timeout_s": 20,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest = monitor.load_manifest(manifest_path)
            self.assertEqual(manifest["cases"][0]["video_path"], video.resolve())

    def test_failed_truth_label_requires_human_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "failure.mp4"
            video.write_bytes(b"placeholder")
            raw = {
                "id": "failure",
                "video": video.name,
                "atomic_action": "拿起水壶",
                "object": "水壶",
                "target": "离开桌面",
                "initial_state": "水壶在桌面上",
                "success_criteria": ["水壶离开桌面"],
                "timeout_s": 20,
                "checkpoints": [{"time_s": 4, "expected_status": "failed"}],
            }
            with self.assertRaisesRegex(monitor.ContractError, "expected_failure_reason"):
                monitor.validate_manifest_case(raw, root, None)


class SummaryTests(unittest.TestCase):
    def test_small_pilot_is_marked_insufficient_data(self):
        record = {
            "model_requested": "model",
            "strategy": "frame_packet",
            "request_status": "ok",
            "raw_response_path": "raw.json",
            "contract_valid": True,
            "evidence_to_result_seconds": 2.0,
            "wall_total_seconds": 2.5,
            "expected_status": "in_progress",
            "recoverable_event": True,
            "model_output": output(),
        }
        summary = monitor.build_summary([record], sla_seconds=5.0, min_requests=30)
        group = summary["groups"][0]
        self.assertEqual(group["gate_status"], "insufficient_data")
        self.assertEqual(group["recoverable_false_failure_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
