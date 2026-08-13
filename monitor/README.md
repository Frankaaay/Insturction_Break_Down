# Visual Monitor Lab

This directory is the curated source import from
`Frankaaay/Robot-Failure-Detector` at commit
`51c28a7a7b1eb470861d9eec7c72516b89a0cc55`, including the source and prompt
work that was present in its local working tree at import time.

It is the experimental visual-monitoring area of the Planner Monitor
repository. Run the examples from this directory so paths such as `scripts/`,
`prompts/`, `data/`, and `artifacts/` resolve locally. Runtime data, model
outputs, logs, credentials, temporary files, and source PDFs are intentionally
not imported or tracked.

Install its optional dependencies separately from the web service:

```powershell
python -m pip install -r monitor/requirements.txt
Set-Location monitor
```

The robot uploader is `ros2_camera_client.py`; camera-independent Monitor
transport/state code lives in `camera_monitor_client.py`, while the ROS2 file
only adapts `sensor_msgs/msg/Image`. Its small dependency set is in
`requirements-ros2-camera.txt`. See the repository root README for `probe` and
continuous `run` commands.

The live client uses a gap-free coverage cursor. Its first window covers 7
seconds; later requests begin one second before the last accepted window ended
and extend to the newest camera frame. A request is capped at 15 seconds, so a
larger backlog is recovered as consecutive overlapping chunks instead of being
dropped. Only one checkpoint is uploaded for an assignment at a time. Each
checkpoint contains `CHAIN_BEFORE`, `PREV_NOW`, the dynamic H.264 window, and
`CURRENT_NOW`; `window_duration_s` drives the Prompt, JSON Schema, timestamp
validation, and evidence extraction.

Prototype for testing whether a vision-language model can monitor short robot
manipulation clips and decide whether an atomic operation is on track,
successful, risky, or failed.

## Recorded Video Monitor Benchmark

The current local-video benchmark is separate from the older MCAP experiments.
It simulates a causal monitor over ordinary MP4/MOV recordings: checkpoints are
created every 4 seconds plus at the exact end of the video, and no request can
see footage after its checkpoint.

Put local recordings in:

```text
data/recorded_samples/
```

That directory contains recording and naming guidance for the first kettle-pick
samples. Videos and the real `manifest.json` are ignored by Git. Copy
`data_manifest.example.json` to `data/recorded_samples/manifest.json`, then edit
the operation, object, success criteria, and optional checkpoint truth labels.

Run the offline preflight first. It performs actual FFmpeg extraction/transcoding,
checks payload sizes, and writes artifacts, but does not call a model API:

```powershell
python scripts/run_recorded_video_monitor.py --dry-run
```

The recorded-video benchmark defaults to Alibaba Cloud Model Studio's Beijing
OpenAI-compatible endpoint. Put the key in the repository root `.env`, which is
ignored by Git:

```dotenv
DASHSCOPE_API_KEY=sk-...
```

The current benchmark contract sends native video only. Its default model is
`qwen3.7-plus`, with thinking disabled and structured output:

```powershell
python scripts/run_recorded_video_monitor.py `
  --provider bailian `
  --model qwen3.7-plus `
  --strategy native_video `
  --reasoning-effort none
```

The default endpoint is
`https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`. Use
`--endpoint-url` only for a different region or a workspace-specific endpoint.

You can repeat `--model` and `--case` to select an explicit matrix.
`--all-models` also includes `qwen3.6-plus`, `stepfun/step-3.7-flash`, and
`qwen3.5-omni-plus`. Use a small canary before a paid sweep. The active strategy is:

- `native_video`: BEFORE plus a 4-second rolling H.264 window extended one
  second backwards so adjacent 4-second checks overlap.

`frame_packet` remains available only when passed explicitly, so earlier
artifacts can be reproduced; it is not part of the current experiment matrix.

Results are written under `artifacts/recorded_monitor/<timestamp>/`:

- `run_manifest.json`: exact prompt/config/model provenance;
- `records.json`: per-request preprocessing, first-byte, response, validation,
  truth, retry, size, token, and completion-image evidence;
- `summary.json`: P50/P95 latency, status metrics, and acceptance gates;
- `completion_evidence/`: frames extracted only for valid `succeeded` results.

The SLA metric is `evidence_to_result_seconds`: Base64 payload construction,
network/model time, JSON validation, and completion-frame extraction after the
checkpoint evidence is ready. FFmpeg preparation is reported separately and is
also included in `wall_total_seconds`. A result obtained after retry is always
marked ineligible for the real-time SLA.

The output contract intentionally contains only `status`, Chinese description,
free-text `failure_reason`, timestamped visual evidence, and an optional
completion-evidence timestamp. It contains no control decision, recoverability,
reason-code, or confidence field.

The active prompt is `prompts/recorded_video_monitor_v2_zh.md`. V2 makes two
status boundaries explicit: acting on a visibly different object while the
target remains untouched is `failed`, and an occluded success predicate is
`unknown`. V1 is retained for reproducible A/B comparison.

## Data

The current `data/` directory contains episode zip files. Each zip includes:

- `episode.mcap`
- `frames/manifest.json`
- `frames/head_right/*.jpg`
- `frames/left_wrist_left/*.jpg`
- `frames/right_wrist_right/*.jpg`

Each current episode has 3 camera lanes and 8 sampled frames per lane.

Supported operation folders can use either codes, English names, or local
aliases. Examples: `A_001`, `Pick`, and `pickup` are all normalized to `pick`.

| Code | Operation | Chinese |
| --- | --- | --- |
| `A_001` | `pick` | 拿起 |
| `A_002` | `place` | 放下 |
| `A_003` | `carry` | 搬运 |
| `A_004` | `pull` | 拉 |
| `A_005` | `push` | 推 |
| `A_006` | `hang` | 挂 |
| `A_007` | `pour` | 倒液体 |
| `A_008` | `press_button` | 按按钮 |
| `A_009` | `wipe` | 擦拭 |
| `A_010` | `insert` | 插入 |
| `A_011` | `rotate` | 旋转 |
| `A_012` | `scoop` | 舀 |
| `A_013` | `stir` | 搅拌 |
| `A_014` | `cut` | 切 |
| `A_015` | `turn` | 翻转 |
| `A_016` | `swipe` | 刷卡 |
| `A_017` | `stamp` | 盖章 |
| `A_018` | `sweep` | 清扫 |

Inspect the dataset:

```powershell
python scripts/inspect_dataset.py --data-dir data
```

Generate a contact sheet for visual inspection:

```powershell
python scripts/inspect_dataset.py --contact-sheet --zip data/18715953aecf4ce50e14b44bd7f913a5.zip --output artifacts/sample_contact_sheet.jpg
```

## Detector Prompt

The first prompt and output contract live in:

```text
prompts/atomic_pick_detector.md
```

The detector returns one JSON object with:

- `status`: `needs_more_observation`, `in_progress`, `at_risk`, `failed`, or `succeeded`
- `success_probability`
- `failure_probability`
- `progress`
- `should_intervene`
- `intervention_level`
- visual `evidence`
- `risk_factors`
- `next_check`
- `confidence`

## OpenRouter Test

Set your key outside the repo:

```powershell
$env:OPENROUTER_API_KEY="sk-or-..."
```

Or paste it into local `local_config.py`:

```python
OPENROUTER_API_KEY = "sk-or-..."
```

The scripts also read `OPENROUTER_API_KEY=...` from a local `.env` file, which is
ignored by git.

Dry run the payload without sending:

```powershell
python scripts/run_openrouter_detection.py --zip data/18715953aecf4ce50e14b44bd7f913a5.zip --strategy timeline_sheet --current-index 7 --dry-run
```

Call OpenRouter:

```powershell
python scripts/run_openrouter_detection.py --zip data/18715953aecf4ce50e14b44bd7f913a5.zip --strategy timeline_sheet --current-index 7 --output artifacts/result.json
```

Run a per-frame/model sweep:

```powershell
python scripts/run_openrouter_sweep.py --data-dir data --models qwen/qwen3-vl-32b-instruct --indices all --strategies current,start_current,timeline_sheet --output-dir artifacts/qwen_all_ops
```

The sweep prints a progress bar with elapsed time, ETA, and per-request latency.

If a model does not support strict JSON schema output, add:

```powershell
--no-json-schema
```

Sampling strategies:

- `current`: current frame from all camera lanes
- `start_current`: first frame plus current frame from all camera lanes
- `history_sparse`: sparse history frames from all camera lanes
- `timeline_sheet`: sparse history compressed into one labeled image

Analyze a sweep:

```powershell
python scripts/analyze_sweep.py --input-dir artifacts/qwen_all_ops
```

The report includes `overall_score`, `final_success_rate`, final
`success_probability`, final `progress`, `no_false_intervention_rate`,
`smoothness_score`, and `avg_elapsed_seconds`. Request latency is reported as an
average but is not included in `overall_score`.

## Qwen3.8-Max Blind Native-Video Test

The open-ended native-video test is intentionally separate from the detector
sweep above. It sends only the continuous `head_right` video extracted from each
`episode.mcap`; it does not send task text, an operation list, success/failure
labels, or files from a `frames/` directory.

The prompt is:

```text
prompts/umi_open_video_understanding_v15_zh.md
```

Run the no-cost preflight first. It extracts/remuxes the MCAP H.264 stream,
checks that OpenRouter advertises video input for the requested model, and
calculates the Base64 request size without calling the model:

```powershell
python scripts/run_openrouter_qwen38_native_video.py --dry-run
```

The default suite is fail-closed and contains 14 cases: one fixed MCAP case for
each of 10 operation folders (`carry`, `drop`, `hang`, `pickup`, `pour_liquid`,
`press_button`, `pull`, `push`, `stir`, and `wipe`) plus `fail1` through
`fail4`. The `place` folder is intentionally excluded from this experiment.
For local adapter testing while one of those 14 cases is missing, use
`--allow-incomplete`; do not treat that as the complete experiment.

After preflight succeeds, run the fixed suite with:

```powershell
python scripts/run_openrouter_qwen38_native_video.py
```

To make one paid canary call before the full batch, select the exact local case
identifier printed by the dry run:

```powershell
python scripts/run_openrouter_qwen38_native_video.py --case stir__8d3686750ff6c506bdd2526a7214a3f6
```

The runner transcodes every head-right stream with one uniform video contract:
source 640x480 resolution, 15 FPS, H.264 CRF 26, and no audio. It stops before
an API request if the MP4 exceeds 5.5 MB or the estimated Base64 request exceeds
8 MB. It does not silently fall back to sampled frames or change compression
again when a case exceeds those limits.

Qwen3.8-Max defaults to very high reasoning effort. This runner explicitly uses
`--reasoning-effort low` and `--max-tokens 5000` so a short visual-description
request retains enough budget for the final answer. Both values can be
overridden from the command line.
