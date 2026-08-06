import argparse
import base64
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from extract_mcap_frames import write_h264_stream
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_MODEL = "qwen/qwen3.8-max"
HEAD_RIGHT_TOPIC = "/camera/coracam_head/right_h264/video"
DEFAULT_PROMPT = "prompts/umi_open_video_understanding_v15_zh.md"
DEFAULT_OUTPUT_ROOT = "artifacts/qwen38_open_video_15"

# Fixed independently of model output. Operation names remain local evaluation
# metadata and are never included in the OpenRouter request.
DEFAULT_SUCCESS_CASE_IDS = {
    "carry": "338da14d5e4acf78a463ca398040a2a7",
    "drop": "02e14afe9cf1e2bc3e995327dbf082f4",
    "hang": "4ce5354fd96cdd3fc964dc5bff926c89",
    "pickup": "0af3559fd1315ffc1d6a8eb0b4838a2a",
    "pour_liquid": "2bd8948922d714158eacf4b15fb25a6a",
    "press_button": "35091bf50de25a338b539f8fd89ffdd2",
    "pull": "00ea080b631de972ad072ea148c9d8e4",
    "push": "3d6e568222379c528893ab6d790671be",
    "stir": "8d3686750ff6c506bdd2526a7214a3f6",
    "wipe": "1241adb33e93dc961a2553eecef7cf43",
}


def get_api_key(project_root: Path) -> str:
    local_config = project_root / "local_config.py"
    if local_config.exists():
        namespace: dict[str, str] = {}
        exec(local_config.read_text(encoding="utf-8"), namespace)
        key = str(namespace.get("OPENROUTER_API_KEY", "")).strip()
        if key:
            return key
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def response_text(response: dict) -> str:
    content = response["choices"][0]["message"]["content"]
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_ffmpeg(explicit: str) -> str:
    if explicit:
        candidate = Path(explicit).resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"FFmpeg does not exist: {candidate}")
        return str(candidate)
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        found = shutil.which("ffmpeg")
        if found:
            return found
    raise RuntimeError("FFmpeg was not found. Pass --ffmpeg with an executable path.")


def zip_contains_mcap(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return any(name.lower().endswith("episode.mcap") for name in archive.namelist())
    except zipfile.BadZipFile:
        return False


def resolve_case_source(data_root: Path, operation: str, case_id: str | None) -> Path | None:
    operation_dir = data_root / operation
    if case_id:
        raw = operation_dir / case_id / "episode.mcap"
        if raw.is_file():
            return raw.resolve()
        archive = operation_dir / f"{case_id}.zip"
        if archive.is_file() and zip_contains_mcap(archive):
            return archive.resolve()
        return None

    raw_candidates = sorted(operation_dir.glob("*/episode.mcap"))
    if raw_candidates:
        return raw_candidates[0].resolve()
    for archive in sorted(operation_dir.glob("*.zip")):
        if zip_contains_mcap(archive):
            return archive.resolve()
    return None


def select_default_cases(data_root: Path) -> tuple[list[dict], list[str]]:
    cases = []
    missing = []
    for operation, case_id in DEFAULT_SUCCESS_CASE_IDS.items():
        source = resolve_case_source(data_root, operation, case_id)
        if source is None:
            missing.append(
                f"{operation}: no episode.mcap found"
                + (f" for fixed case {case_id}" if case_id else "")
            )
            continue
        resolved_case_id = case_id or (source.parent.name if source.suffix == ".mcap" else source.stem)
        cases.append(
            {
                "case": f"{operation}__{resolved_case_id}",
                "evaluation_group": operation,
                "source": source,
            }
        )

    for name in ("fail1", "fail2", "fail3", "fail4"):
        source = (data_root / "fail" / name / "episode.mcap").resolve()
        if not source.is_file():
            missing.append(f"fail/{name}: episode.mcap is missing")
            continue
        cases.append(
            {
                "case": f"fail__{name}",
                "evaluation_group": "fail",
                "source": source,
            }
        )
    return cases, missing


@contextlib.contextmanager
def materialized_mcap(source: Path):
    if source.suffix.lower() == ".mcap":
        yield source
        return

    temp_path: Path | None = None
    try:
        with zipfile.ZipFile(source) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith("episode.mcap")]
            if len(members) != 1:
                raise RuntimeError(f"{source}: expected one episode.mcap, found {len(members)}")
            handle = tempfile.NamedTemporaryFile(prefix="qwen38_case_", suffix=".mcap", delete=False)
            temp_path = Path(handle.name)
            with handle, archive.open(members[0]) as source_handle:
                shutil.copyfileobj(source_handle, handle)
        yield temp_path
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def extract_head_right_video(
    source: Path,
    output_path: Path,
    ffmpeg: str,
    overwrite: bool,
    output_fps: float,
    crf: int,
) -> dict:
    sidecar = output_path.with_suffix(".video.json")
    if output_path.exists() and sidecar.exists() and not overwrite:
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        metadata["reused"] = True
        return metadata
    if output_path.exists() and not overwrite:
        raise RuntimeError(
            f"Refusing to reuse video without provenance sidecar: {output_path}. "
            "Pass --overwrite to regenerate it."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_handle = tempfile.NamedTemporaryFile(prefix="qwen38_head_right_", suffix=".h264", delete=False)
    raw_path = Path(raw_handle.name)
    raw_handle.close()
    try:
        with materialized_mcap(source) as mcap_path:
            times_ns = write_h264_stream(mcap_path, HEAD_RIGHT_TOPIC, raw_path)
        if not times_ns or not raw_path.exists() or raw_path.stat().st_size == 0:
            raise RuntimeError(f"No head_right H.264 payloads found in {source}")
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "+genpts",
                "-r",
                "30",
                "-i",
                str(raw_path),
                "-an",
                "-vf",
                f"fps={output_fps}",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                str(crf),
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            check=True,
        )
    finally:
        if raw_path.exists():
            raw_path.unlink()

    duration_s = (times_ns[-1] - times_ns[0]) / 1_000_000_000 if len(times_ns) > 1 else 0.0
    metadata = {
        "source": str(source),
        "topic": HEAD_RIGHT_TOPIC,
        "packet_count": len(times_ns),
        "duration_s": round(duration_s, 6),
        "source_fps": 30.0,
        "output_fps": output_fps,
        "crf": crf,
        "codec": "H.264/libx264",
        "pixel_format": "yuv420p",
        "video": str(output_path),
        "video_bytes": output_path.stat().st_size,
        "video_sha256": sha256_file(output_path),
        "reused": False,
    }
    sidecar.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def estimated_base64_bytes(raw_bytes: int) -> int:
    return 4 * ((raw_bytes + 2) // 3)


def build_payload(
    model: str,
    prompt: str,
    video_path: Path,
    max_tokens: int,
    reasoning_effort: str,
) -> dict:
    encoded = base64.b64encode(video_path.read_bytes()).decode("ascii")
    video_data_url = f"data:video/mp4;base64,{encoded}"
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "请完整观看这个连续视频，并严格按照系统要求给出纯视觉描述。",
                    },
                    {
                        "type": "video_url",
                        "video_url": {"url": video_data_url},
                    },
                ],
            },
        ],
        "reasoning": {"effort": reasoning_effort, "exclude": False},
        "temperature": 0,
        "max_tokens": max_tokens,
    }


def call_openrouter(payload: dict, api_key: str, timeout_s: int) -> dict:
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/local/robot-failure-detector",
            "X-Title": "Robot Failure Detector Qwen3.8 Native Video",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def verify_model_video_support(model: str, timeout_s: int) -> dict:
    with urllib.request.urlopen(OPENROUTER_MODELS_URL, timeout=timeout_s) as response:
        data = json.loads(response.read().decode("utf-8"))["data"]
    record = next((item for item in data if item.get("id") == model), None)
    if record is None:
        raise RuntimeError(f"OpenRouter model was not found: {model}")
    modalities = record.get("architecture", {}).get("input_modalities", [])
    if "video" not in modalities:
        raise RuntimeError(f"OpenRouter model does not advertise video input: {model}: {modalities}")
    return {"id": record.get("id"), "name": record.get("name"), "input_modalities": modalities}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a blind, open-ended Qwen3.8-Max test on native head_right MCAP video."
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--prompt", type=Path, default=Path(DEFAULT_PROMPT))
    parser.add_argument("--output-root", type=Path, default=Path(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--case", action="append", default=[], help="Run only matching case id(s).")
    parser.add_argument("--video-fps", type=float, default=15.0)
    parser.add_argument("--video-crf", type=int, default=26)
    parser.add_argument("--max-video-mb", type=float, default=5.5)
    parser.add_argument("--max-request-mb", type=float, default=8.0)
    parser.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high", "xhigh"],
        default="low",
    )
    parser.add_argument("--max-tokens", type=int, default=5000)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=5.0)
    parser.add_argument("--ffmpeg", default="")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--skip-model-check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    data_root = (project_root / args.data_root).resolve() if not args.data_root.is_absolute() else args.data_root.resolve()
    prompt_path = (project_root / args.prompt).resolve() if not args.prompt.is_absolute() else args.prompt.resolve()
    output_root = (project_root / args.output_root).resolve() if not args.output_root.is_absolute() else args.output_root.resolve()

    cases, missing = select_default_cases(data_root)
    if args.case:
        wanted = set(args.case)
        cases = [item for item in cases if item["case"] in wanted]
        unknown = wanted - {item["case"] for item in cases}
        if unknown:
            raise SystemExit(f"Unknown or unavailable --case values: {sorted(unknown)}")
        missing = []
    if missing and not args.allow_incomplete:
        details = "\n".join(f"- {item}" for item in missing)
        raise SystemExit(
            "The exact 14-case native-video suite is incomplete:\n"
            f"{details}\n"
            "Add the missing MCAP case or pass --allow-incomplete for a non-final dry run."
        )
    if not cases:
        raise SystemExit("No runnable MCAP cases were selected.")

    model_info = None
    if not args.skip_model_check:
        model_info = verify_model_video_support(args.model, min(args.timeout, 30))
    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    prompt_template = prompt_path.read_text(encoding="utf-8")
    prompt_sha256 = hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()

    api_key = ""
    if not args.dry_run:
        api_key = get_api_key(project_root)
        if not api_key:
            raise SystemExit("OPENROUTER_API_KEY is missing")

    videos_dir = output_root / "videos"
    outputs_dir = output_root / "outputs"
    raw_dir = output_root / "raw"
    logs_dir = output_root / "logs"
    for path in (videos_dir, outputs_dir, raw_dir, logs_dir):
        path.mkdir(parents=True, exist_ok=True)

    records = []
    for index, case_data in enumerate(cases, start=1):
        case = case_data["case"]
        output_path = outputs_dir / f"{case}.txt"
        if args.skip_existing and output_path.exists() and not args.dry_run:
            log_path = logs_dir / f"{case}.run.json"
            if not log_path.exists():
                raise RuntimeError(f"Existing output has no run provenance log: {output_path}")
            record = json.loads(log_path.read_text(encoding="utf-8"))
            record["reused_existing_output"] = True
            records.append(record)
            print(f"SKIP {index}/{len(cases)} {case}", flush=True)
            continue

        fps_label = str(args.video_fps).replace(".", "p")
        video_path = videos_dir / f"{case}__head_right_{fps_label}fps_crf{args.video_crf}.mp4"
        video_metadata = extract_head_right_video(
            case_data["source"],
            video_path,
            ffmpeg,
            args.overwrite,
            args.video_fps,
            args.video_crf,
        )
        raw_bytes = video_path.stat().st_size
        b64_bytes = estimated_base64_bytes(raw_bytes)
        prompt = prompt_template.format(video_duration_s=f"{video_metadata['duration_s']:.2f}")
        estimated_request_bytes = b64_bytes + len(prompt.encode("utf-8")) + 4096
        size_record = {
            "video_mb": round(raw_bytes / 1024 / 1024, 3),
            "estimated_base64_mb": round(b64_bytes / 1024 / 1024, 3),
            "estimated_request_mb": round(estimated_request_bytes / 1024 / 1024, 3),
        }
        if size_record["video_mb"] > args.max_video_mb or size_record["estimated_request_mb"] > args.max_request_mb:
            raise SystemExit(
                f"{case}: Base64 request exceeds the configured safety limit: {size_record}. "
                "No API request was sent. Confirm a compression or URL strategy before continuing."
            )

        record = {
            "case": case,
            "evaluation_group_local_only": case_data["evaluation_group"],
            "source": str(case_data["source"]),
            "input_adapter": (
                f"MCAP head_right continuous H.264 transcoded to {args.video_fps:g} FPS "
                f"CRF {args.video_crf} MP4; Base64 video_url"
            ),
            "video": video_metadata,
            "size": size_record,
            "prompt_sha256": prompt_sha256,
            "model_requested": args.model,
            "status": "dry_run" if args.dry_run else "pending",
        }

        if args.dry_run:
            records.append(record)
            print(
                f"DRY {index}/{len(cases)} {case} duration={video_metadata['duration_s']:.2f}s "
                f"video={size_record['video_mb']:.2f}MB request~={size_record['estimated_request_mb']:.2f}MB",
                flush=True,
            )
            continue

        payload = build_payload(
            args.model,
            prompt,
            video_path,
            args.max_tokens,
            args.reasoning_effort,
        )
        started = time.perf_counter()
        response = None
        error = ""
        for attempt in range(1, args.retries + 2):
            try:
                response = call_openrouter(payload, api_key, args.timeout)
                record["attempts"] = attempt
                break
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                error = f"HTTP {exc.code}: {details[:4000]}"
                retryable = exc.code in {408, 409, 429, 500, 502, 503, 504}
                if not retryable or attempt > args.retries:
                    break
            except Exception as exc:
                error = repr(exc)
                if attempt > args.retries:
                    break
            time.sleep(args.retry_delay * attempt)

        record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        if response is None:
            record["status"] = "error"
            record["error"] = error
            (logs_dir / f"{case}.error.txt").write_text(error, encoding="utf-8")
            records.append(record)
            print(f"ERROR {index}/{len(cases)} {case}: {error}", flush=True)
            continue

        text = response_text(response)
        if not text.strip():
            record.update(
                {
                    "status": "error_no_content",
                    "model_returned": response.get("model"),
                    "finish_reason": response["choices"][0].get("finish_reason"),
                    "usage": response.get("usage", {}),
                    "error": "Model returned no final answer content.",
                }
            )
            (raw_dir / f"{case}.json").write_text(
                json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (logs_dir / f"{case}.run.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            records.append(record)
            print(
                f"ERROR {index}/{len(cases)} {case}: no final content "
                f"finish_reason={record['finish_reason']}",
                flush=True,
            )
            continue
        record.update(
            {
                "status": "ok",
                "model_returned": response.get("model"),
                "finish_reason": response["choices"][0].get("finish_reason"),
                "usage": response.get("usage", {}),
                "output_chars": len(text),
            }
        )
        output_path.write_text(text, encoding="utf-8")
        (raw_dir / f"{case}.json").write_text(
            json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (logs_dir / f"{case}.run.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        records.append(record)
        print(
            f"DONE {index}/{len(cases)} {case} elapsed={record['elapsed_seconds']:.2f}s "
            f"chars={len(text)}",
            flush=True,
        )

    manifest = {
        "schema_version": 1,
        "experiment": "qwen38_open_ended_head_right_native_video_14",
        "dry_run": args.dry_run,
        "model": args.model,
        "model_info": model_info,
        "reasoning_effort": args.reasoning_effort,
        "max_tokens": args.max_tokens,
        "prompt": str(prompt_path),
        "prompt_sha256": prompt_sha256,
        "input_contract": {
            "camera": "head_right only",
            "source": "episode.mcap continuous H.264 stream",
            "video_encoding": {
                "resolution": "source 640x480 preserved",
                "fps": args.video_fps,
                "codec": "H.264/libx264",
                "crf": args.video_crf,
                "pixel_format": "yuv420p",
            },
            "transport": "Base64 data:video/mp4 in video_url",
            "task_text_sent": False,
            "operation_list_sent": False,
            "frames_folder_used": False,
        },
        "missing_cases": missing,
        "records": records,
    }
    manifest_name = "run_manifest_dry_run.json" if args.dry_run else "run_manifest.json"
    manifest_path = output_root / manifest_name
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE {manifest_path}", flush=True)


if __name__ == "__main__":
    main()
