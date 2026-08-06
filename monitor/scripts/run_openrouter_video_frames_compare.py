import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def get_api_key(project_root: Path) -> str:
    local_config = project_root / "local_config.py"
    if local_config.exists():
        namespace: dict[str, str] = {}
        exec(local_config.read_text(encoding="utf-8"), namespace)
        key = str(namespace.get("OPENROUTER_API_KEY", "")).strip()
        if key:
            return key
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def image_part(path: Path) -> dict:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
    }


def response_text(response: dict) -> str:
    content = response["choices"][0]["message"]["content"]
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content)


def call_openrouter(payload: dict, api_key: str, timeout_s: int) -> dict:
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/local/robot-failure-detector",
            "X-Title": "Robot Failure Detector Video Comparison",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--frames-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--model", default="qwen/qwen3-vl-32b-instruct")
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    api_key = get_api_key(project_root)
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is missing")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    template = args.prompt.read_text(encoding="utf-8")
    output_dirs = {
        name: args.output_root / name
        for name in ("qwen_outputs", "qwen_raw", "qwen_logs")
    }
    for path in output_dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    records = []
    for index, case_data in enumerate(manifest["cases"], start=1):
        case = case_data["case"]
        duration = float(case_data["duration_s"])
        frames = sorted((args.frames_root / case).glob("*.jpg"))
        if len(frames) != 64:
            raise RuntimeError(f"{case}: expected 64 frames, found {len(frames)}")

        prompt = template.format(video_duration_s=f"{duration:.2f}")
        content = [{"type": "text", "text": prompt}]
        content.extend(image_part(frame) for frame in frames)
        payload = {
            "model": args.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": args.max_tokens,
        }

        print(f"QWEN START {index}/10 {case}", flush=True)
        started = time.perf_counter()
        result = None
        error = ""
        for attempt in range(1, 4):
            try:
                result = call_openrouter(payload, api_key, args.timeout)
                break
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                error = f"HTTP {exc.code}: {details[:4000]}"
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    break
            except Exception as exc:
                error = repr(exc)
                if attempt == 3:
                    break
            time.sleep(5 * attempt)

        elapsed = round(time.perf_counter() - started, 2)
        if result is None:
            (output_dirs["qwen_logs"] / f"{case}.error.txt").write_text(
                error or "unknown error", encoding="utf-8"
            )
            records.append(
                {
                    **case_data,
                    "status": "error",
                    "elapsed_seconds": elapsed,
                    "error": error,
                }
            )
            print(f"QWEN ERROR {case}: {error}", flush=True)
            continue

        text = response_text(result)
        raw_path = output_dirs["qwen_raw"] / f"{case}.json"
        output_path = output_dirs["qwen_outputs"] / f"{case}.txt"
        raw_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        output_path.write_text(text, encoding="utf-8")
        record = {
            **case_data,
            "status": "ok",
            "frame_count": len(frames),
            "model_requested": args.model,
            "model_returned": result.get("model"),
            "finish_reason": result["choices"][0].get("finish_reason"),
            "elapsed_seconds": elapsed,
            "output_chars": len(text),
            "usage": result.get("usage", {}),
        }
        records.append(record)
        (output_dirs["qwen_logs"] / f"{case}.run.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        total_tokens = record["usage"].get("total_tokens")
        print(
            f"QWEN DONE {index}/10 {case} elapsed={elapsed}s "
            f"chars={len(text)} tokens={total_tokens}",
            flush=True,
        )

    batch_manifest = {
        "schema_version": 1,
        "input_adapter": (
            "64 uniformly sampled chronological JPEG frames from the same MP4; "
            "OpenRouter returned no video-capable endpoint for this model"
        ),
        "records": records,
    }
    (args.output_root / "qwen_batch_manifest.json").write_text(
        json.dumps(batch_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
