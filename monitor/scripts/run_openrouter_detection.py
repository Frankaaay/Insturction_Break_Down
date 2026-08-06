import argparse
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-2.5-pro"
DEFAULT_OPERATION = "pick"
SLIDING_WINDOW_FRAMES = 4
LANE_FOLDERS = {
    "head/right": "frames/head_right",
    "left_wrist/left": "frames/left_wrist_left",
    "right_wrist/right": "frames/right_wrist_right",
}


OPERATION_ALIASES = {
    "a001": "pick",
    "a_001": "pick",
    "pick": "pick",
    "pickup": "pick",
    "pick_up": "pick",
    "a002": "place",
    "a_002": "place",
    "place": "place",
    "drop": "place",
    "put_down": "place",
    "a003": "carry",
    "a_003": "carry",
    "carry": "carry",
    "transport": "carry",
    "a004": "pull",
    "a_004": "pull",
    "pull": "pull",
    "a005": "push",
    "a_005": "push",
    "push": "push",
    "a006": "hang",
    "a_006": "hang",
    "hang": "hang",
    "a007": "pour",
    "a_007": "pour",
    "pour": "pour",
    "pour_liquid": "pour",
    "a008": "press_button",
    "a_008": "press_button",
    "pressbutton": "press_button",
    "press_button": "press_button",
    "button_press": "press_button",
    "a009": "wipe",
    "a_009": "wipe",
    "wipe": "wipe",
    "a010": "insert",
    "a_010": "insert",
    "insert": "insert",
    "insertion": "insert",
    "a011": "rotate",
    "a_011": "rotate",
    "rotate": "rotate",
    "rotation": "rotate",
    "a012": "scoop",
    "a_012": "scoop",
    "scoop": "scoop",
    "a013": "stir",
    "a_013": "stir",
    "stir": "stir",
    "a014": "cut",
    "a_014": "cut",
    "cut": "cut",
    "a015": "turn",
    "a_015": "turn",
    "turn": "turn",
    "flip": "turn",
    "turn_over": "turn",
    "a016": "swipe",
    "a_016": "swipe",
    "swipe": "swipe",
    "swipe_card": "swipe",
    "card_swipe": "swipe",
    "a017": "stamp",
    "a_017": "stamp",
    "stamp": "stamp",
    "a018": "sweep",
    "a_018": "sweep",
    "sweep": "sweep",
    "a019": "close",
    "a_019": "close",
    "close": "close",
    "a020": "open",
    "a_020": "open",
    "open": "open",
}


OPERATION_SPECS = {
    "pick": {
        "code": "A_001",
        "display": "Pick / 拿起",
        "action": "pick up the intended object",
        "success": "the latest/current frame shows the intended object securely grasped and lifted, or stably held after lifting",
    },
    "place": {
        "code": "A_002",
        "display": "Place / 放下",
        "action": "place or put down the intended object",
        "success": "the latest/current frame shows the object released from the tool or gripper and resting at the intended placement area",
    },
    "carry": {
        "code": "A_003",
        "display": "Carry / 搬运",
        "action": "carry or transport the intended object",
        "success": "the latest/current frame shows the intended object still secured while being moved toward or held at the carry target",
    },
    "pull": {
        "code": "A_004",
        "display": "Pull / 拉",
        "action": "pull the intended object or handle",
        "success": "the latest/current frame shows the intended object or handle displaced in the pull direction with the gripper maintaining useful contact",
    },
    "push": {
        "code": "A_005",
        "display": "Push / 推",
        "action": "push the intended object or surface",
        "success": "the latest/current frame shows the intended object or control displaced in the push direction with useful contact",
    },
    "hang": {
        "code": "A_006",
        "display": "Hang / 挂",
        "action": "hang the intended object",
        "success": "the latest/current frame shows the object supported by the hook, rack, or hanging target without needing the gripper to hold it",
    },
    "pour": {
        "code": "A_007",
        "display": "Pour / 倒液体",
        "action": "pour liquid from the source container into the target",
        "success": "the latest/current frame shows the source container tilted toward the target with liquid transfer completed or clearly happening into the intended target",
    },
    "press_button": {
        "code": "A_008",
        "display": "PressButton / 按按钮",
        "action": "press the intended button",
        "success": "the latest/current frame shows the button contacted and depressed, or the press action clearly completed",
    },
    "wipe": {
        "code": "A_009",
        "display": "Wipe / 擦拭",
        "action": "wipe the intended surface",
        "success": "the latest/current frame shows the wiping tool in contact with the target surface after a wiping motion across the intended area",
    },
    "insert": {
        "code": "A_010",
        "display": "Insert / 插入",
        "action": "insert the intended object into the target slot, hole, or container",
        "success": "the latest/current frame shows the object aligned with and inserted into the intended target",
    },
    "rotate": {
        "code": "A_011",
        "display": "Rotate / 旋转",
        "action": "rotate the intended object, handle, knob, or tool",
        "success": "the latest/current frame shows the intended target rotated in the desired direction or held after a completed rotation",
    },
    "scoop": {
        "code": "A_012",
        "display": "Scoop / 舀",
        "action": "scoop the intended material or object with the tool",
        "success": "the latest/current frame shows the material or object collected in or on the scooping tool",
    },
    "stir": {
        "code": "A_013",
        "display": "Stir / 搅拌",
        "action": "stir the contents of the target container",
        "success": "the latest/current frame shows the tool inside the container and the stirring motion underway or completed",
    },
    "cut": {
        "code": "A_014",
        "display": "Cut / 切",
        "action": "cut the intended object or material",
        "success": "the latest/current frame shows the cutting tool contacting and separating, slicing, or visibly cutting the intended material",
    },
    "turn": {
        "code": "A_015",
        "display": "Turn / 翻转",
        "action": "turn over or flip the intended object",
        "success": "the latest/current frame shows the intended object turned over, flipped, or clearly reoriented as required",
    },
    "swipe": {
        "code": "A_016",
        "display": "Swipe / 刷卡",
        "action": "swipe the intended card or object through the target reader or contact area",
        "success": "the latest/current frame shows the card or object moved through the intended reader/contact path",
    },
    "stamp": {
        "code": "A_017",
        "display": "Stamp / 盖章",
        "action": "stamp the intended target surface",
        "success": "the latest/current frame shows the stamp contacted and pressed onto the intended surface, or the stamping action clearly completed",
    },
    "sweep": {
        "code": "A_018",
        "display": "Sweep / 清扫",
        "action": "sweep the intended surface or debris",
        "success": "the latest/current frame shows the sweeping tool moving debris or covering the intended surface area",
    },
    "close": {
        "code": "A_019",
        "display": "Close / 关闭",
        "action": "close the intended door, lid, drawer, or cover",
        "success": "the latest/current frame shows the intended door, lid, drawer, or cover fully closed and staying closed",
    },
    "open": {
        "code": "A_020",
        "display": "Open / 打开",
        "action": "open the intended door, lid, drawer, or container",
        "success": "the latest/current frame shows the intended door, lid, drawer, or container opened to expose its interior or opening",
    },
}


JSON_SCHEMA = {
    "name": "atomic_operation_detection",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task": {"type": "string"},
            "status": {
                "type": "string",
                "enum": [
                    "needs_more_observation",
                    "in_progress",
                    "at_risk",
                    "failed",
                    "succeeded",
                ],
            },
            "success_probability": {"type": "number", "minimum": 0, "maximum": 1},
            "failure_probability": {"type": "number", "minimum": 0, "maximum": 1},
            "progress": {"type": "number", "minimum": 0, "maximum": 1},
            "should_intervene": {"type": "boolean"},
            "intervention_level": {
                "type": "string",
                "enum": ["none", "observe", "low_level_retry", "high_level_agent"],
            },
            "evidence": {"type": "array", "items": {"type": "string"}},
            "risk_factors": {"type": "array", "items": {"type": "string"}},
            "next_check": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "recommended_delay_frames": {"type": "integer", "minimum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["recommended_delay_frames", "reason"],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "task",
            "status",
            "success_probability",
            "failure_probability",
            "progress",
            "should_intervene",
            "intervention_level",
            "evidence",
            "risk_factors",
            "next_check",
            "confidence",
        ],
    },
}


SYSTEM_PROMPT = """You are a robotics task monitor for short-horizon manipulation episodes.
Your job is to judge whether the robot/human-operated tool or gripper is still on track
to complete the requested atomic action.

Use the latest/current frame as the decision frame. Earlier frames are context
for motion and object identity, not proof that the action is currently complete.
Use only visible evidence. Be conservative about declaring irreversible failure:
if the operator can still recover, report needs_more_observation, in_progress,
or at_risk.

Status definitions:
- succeeded: the latest/current frame satisfies the operation-specific success criteria.
- in_progress: the agent is approaching, aligning, contacting, moving, or plausibly progressing.
- needs_more_observation: the scene is occluded, ambiguous, or too early to judge.
- at_risk: visible evidence suggests high chance of failure without correction, but recovery is possible.
- failed: the action is unlikely to succeed without higher-level intervention because there is visible hard-to-recover evidence.

For process-heavy operations such as pour, stir, wipe, sweep, cut, and scoop,
do not mark failed merely because the latest frame lacks motion or the target is
temporarily occluded. Use recent history to decide whether the task is plausibly
underway. Set should_intervene only when immediate intervention is useful, and
for failed only with irreversible or hard-to-recover evidence.

Because this dataset currently contains successful demonstrations, avoid inventing failures.
Return exactly one JSON object matching the provided schema."""


def get_api_key() -> str:
    local_config_path = Path("local_config.py")
    if local_config_path.exists():
        namespace: dict[str, str] = {}
        exec(local_config_path.read_text(encoding="utf-8"), namespace)
        key = namespace.get("OPENROUTER_API_KEY")
        if key:
            return str(key).strip()

    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    env_path = Path(".env")
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == "OPENROUTER_API_KEY":
            return value.strip().strip('"').strip("'")
    return ""


def data_url(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def load_manifest(zf: zipfile.ZipFile) -> dict:
    return json.loads(zf.read("frames/manifest.json"))


def selected_indices(frames_per_lane: int, current_index: int, strategy: str) -> list[int]:
    current_index = max(0, min(current_index, frames_per_lane - 1))
    if strategy == "current":
        return [current_index]
    if strategy == "start_current":
        return sorted(set([0, current_index]))
    if strategy == "start_sliding_window":
        start = max(0, current_index - SLIDING_WINDOW_FRAMES + 1)
        return sorted(set([0, *range(start, current_index + 1)]))
    if strategy in {"history_sparse", "timeline_sheet"}:
        raw = [0, current_index // 3, (2 * current_index) // 3, current_index]
        return sorted(set(raw))
    raise ValueError(f"Unknown strategy: {strategy}")


def normalize_operation_name(operation: str) -> str:
    key = operation.strip().lower().replace("-", "_").replace(" ", "_")
    if key.startswith("a_") and len(key) == 5:
        return OPERATION_ALIASES.get(key, key)
    if key.startswith("a") and len(key) == 4 and key[1:].isdigit():
        return OPERATION_ALIASES.get(f"a_{key[1:]}", OPERATION_ALIASES.get(key, key))
    return OPERATION_ALIASES.get(key, key)


def infer_operation(zip_path: Path, explicit_operation: str = "") -> str:
    if explicit_operation:
        return normalize_operation_name(explicit_operation)
    parent_name = zip_path.parent.name
    if parent_name and parent_name.lower() != "data":
        return normalize_operation_name(parent_name)
    return DEFAULT_OPERATION


def operation_spec(operation: str) -> dict:
    return OPERATION_SPECS.get(
        operation,
        {
            "action": operation.replace("_", " "),
            "success": "the latest/current frame visibly satisfies the named atomic operation",
        },
    )


def frame_times(manifest: dict, lane_key: str) -> dict[int, int]:
    for lane in manifest["lanes"]:
        if lane["laneKey"] == lane_key:
            return {idx: frame["timeMs"] for idx, frame in enumerate(lane["frames"])}
    return {}


def build_timeline_sheet(
    zf: zipfile.ZipFile,
    manifest: dict,
    indices: list[int],
    width: int = 320,
    height: int = 220,
) -> bytes:
    label_h = 28
    cell_h = height + label_h
    lane_items = list(LANE_FOLDERS.items())
    sheet = Image.new("RGB", (width * len(indices), cell_h * len(lane_items)), "white")
    draw = ImageDraw.Draw(sheet)

    for row, (lane_key, folder) in enumerate(lane_items):
        times = frame_times(manifest, lane_key)
        for col, idx in enumerate(indices):
            img = Image.open(io.BytesIO(zf.read(f"{folder}/{idx:02d}.jpg"))).convert("RGB")
            img.thumbnail((width, height))
            x = col * width
            y = row * cell_h
            sheet.paste(img, (x + (width - img.width) // 2, y + label_h))
            draw.text((x + 6, y + 6), f"{lane_key} f{idx} t={times.get(idx, '?')}ms", fill=(0, 0, 0))

    out = io.BytesIO()
    sheet.save(out, format="JPEG", quality=88)
    return out.getvalue()


def build_image_parts(
    zip_path: Path,
    current_index: int,
    strategy: str,
    operation: str = "",
    layout: str = "auto",
    task_text: str = "",
) -> tuple[list[dict], dict]:
    with zipfile.ZipFile(zip_path) as zf:
        manifest = load_manifest(zf)
        frames_per_lane = int(manifest["framesPerLane"])
        indices = selected_indices(frames_per_lane, current_index, strategy)
        inferred_operation = infer_operation(zip_path, operation)
        spec = operation_spec(inferred_operation)
        metadata = {
            "episode": zip_path.name,
            "task_instruction": task_text,
            "operation": inferred_operation,
            "operation_code": spec.get("code", ""),
            "operation_display": spec.get("display", inferred_operation),
            "action": spec["action"],
            "success_criteria": spec["success"],
            "strategy": strategy,
            "layout": layout,
            "selected_indices": indices,
            "lanes": list(LANE_FOLDERS.keys()),
        }

        resolved_layout = layout
        if resolved_layout == "auto":
            resolved_layout = "sheet" if strategy == "timeline_sheet" else "separate"
            metadata["layout"] = resolved_layout

        if resolved_layout == "sheet":
            image_bytes = build_timeline_sheet(zf, manifest, indices)
            return [
                {
                    "type": "image_url",
                    "image_url": {"url": data_url(image_bytes)},
                }
            ], metadata

        parts = []
        for idx in indices:
            for lane_key, folder in LANE_FOLDERS.items():
                image_bytes = zf.read(f"{folder}/{idx:02d}.jpg")
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url(image_bytes)},
                    }
                )
                metadata.setdefault("images", []).append({"lane": lane_key, "frame": idx})
        return parts, metadata


def build_payload(
    model: str,
    prompt_path: Path,
    image_parts: list[dict],
    metadata: dict,
    structured_output: bool = True,
) -> dict:
    if prompt_path.exists():
        prompt_text = prompt_path.read_text(encoding="utf-8")
        system_prompt = prompt_text.split("## User Prompt Template")[0]
    else:
        system_prompt = SYSTEM_PROMPT

    action = metadata.get("action", metadata.get("operation", "the requested atomic operation"))
    success_criteria = metadata.get("success_criteria", "the latest/current frame satisfies the requested operation")
    task_instruction = metadata.get("task_instruction", "")
    task_line = ""
    if task_instruction:
        task_line = (
            f"Task instruction: {task_instruction}\n"
            "The success criteria apply to the SPECIFIC object named in the task instruction. "
            "First identify that object in the scene. If the tool or gripper is manipulating a "
            "different object than the one named, that is evidence of failure (wrong object), "
            "not success — say so in the evidence and lower success_probability accordingly.\n"
        )
    user_text = (
        "Evaluate the current state of this atomic robot manipulation action.\n\n"
        f"Operation: {metadata.get('operation', 'unknown')}.\n"
        f"{task_line}"
        f"Action: {action}.\n"
        f"Success criteria: {success_criteria}.\n"
        f"Sampling metadata: {json.dumps(metadata, ensure_ascii=False)}\n\n"
        "The images are ordered and labeled by camera lane and frame index/time. "
        "Use the latest/current frame as the decision frame; earlier frames are context only. "
        "Determine whether the operation is on track, already successful in the latest/current frame, "
        "at risk, or failed. "
        "Return only the JSON object."
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [{"type": "text", "text": user_text}, *image_parts],
            },
        ],
        "temperature": 0,
    }
    if structured_output:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": JSON_SCHEMA,
        }
    return payload


def call_openrouter(payload: dict, api_key: str) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/local/robot-failure-detector",
            "X-Title": "Robot Failure Detector",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {details}") from exc


def parse_model_json(response: dict) -> dict:
    content = response["choices"][0]["message"]["content"]
    if isinstance(content, list):
        text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    else:
        text = content
    text = text.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True, help="Path to one episode zip.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--operation", default="", help="Override operation name; otherwise inferred from parent folder.")
    parser.add_argument(
        "--strategy",
        choices=["current", "start_current", "start_sliding_window", "history_sparse", "timeline_sheet"],
        default="timeline_sheet",
    )
    parser.add_argument("--current-index", type=int, default=7)
    parser.add_argument("--prompt", default="prompts/atomic_pick_detector.md")
    parser.add_argument("--output", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-json-schema", action="store_true")
    args = parser.parse_args()

    image_parts, metadata = build_image_parts(Path(args.zip), args.current_index, args.strategy, args.operation)
    payload = build_payload(
        args.model,
        Path(args.prompt),
        image_parts,
        metadata,
        structured_output=not args.no_json_schema,
    )

    if args.dry_run:
        safe_payload = json.loads(json.dumps(payload))
        for message in safe_payload["messages"]:
            if isinstance(message.get("content"), list):
                for part in message["content"]:
                    if part.get("type") == "image_url":
                        part["image_url"]["url"] = part["image_url"]["url"][:80] + "...truncated"
        print(json.dumps(safe_payload, indent=2, ensure_ascii=False))
        return

    api_key = get_api_key()
    if not api_key:
        print("Set OPENROUTER_API_KEY before calling OpenRouter.", file=sys.stderr)
        sys.exit(2)

    started = time.perf_counter()
    response = call_openrouter(payload, api_key)
    elapsed_seconds = time.perf_counter() - started
    result = parse_model_json(response)
    output_text = json.dumps(result, indent=2, ensure_ascii=False)
    print(output_text)
    print(f"elapsed_seconds: {elapsed_seconds:.3f}", file=sys.stderr)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(output_text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
