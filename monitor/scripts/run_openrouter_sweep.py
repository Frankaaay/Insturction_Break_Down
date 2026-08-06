import argparse
import json
import sys
import time
from pathlib import Path

from run_openrouter_detection import (
    build_image_parts,
    build_payload,
    call_openrouter,
    get_api_key,
    infer_operation,
    parse_model_json,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


DEFAULT_MODELS = [
    "qwen/qwen3-vl-32b-instruct",
]


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_indices(value: str) -> list[int]:
    if value == "all":
        return list(range(8))
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def limit_per_operation(zip_paths: list[Path], max_per_operation: int) -> list[Path]:
    if max_per_operation <= 0:
        return zip_paths
    counts: dict[str, int] = {}
    selected = []
    for path in zip_paths:
        operation = infer_operation(path)
        count = counts.get(operation, 0)
        if count >= max_per_operation:
            continue
        selected.append(path)
        counts[operation] = count + 1
    return selected


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def progress_line(done: int, total: int, started_at: float) -> str:
    width = 28
    ratio = done / total if total else 1.0
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    elapsed = time.perf_counter() - started_at
    if done:
        eta = elapsed * (total - done) / done
    else:
        eta = 0
    return (
        f"[{done:>4}/{total:<4} {ratio * 100:5.1f}%] "
        f"[{bar}] elapsed {format_duration(elapsed)} eta {format_duration(eta)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", help="Path to one episode zip.")
    parser.add_argument("--data-dir", default="", help="Run every *.zip in this directory.")
    parser.add_argument("--operations", default="", help="Optional comma-separated inferred operations to include.")
    parser.add_argument(
        "--max-per-operation",
        type=int,
        default=0,
        help="Optional cap on zips per inferred operation. 0 means no cap.",
    )
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument(
        "--strategies",
        default="current,start_current,start_sliding_window,timeline_sheet",
        help="Comma-separated strategies.",
    )
    parser.add_argument(
        "--layouts",
        default="auto",
        help="Comma-separated image layouts: auto,separate,sheet. auto preserves legacy behavior.",
    )
    parser.add_argument("--indices", default="all", help="'all' or comma-separated frame indices.")
    parser.add_argument(
        "--task-texts",
        default="",
        help="Optional JSON file mapping zip stem -> episode-specific task instruction text.",
    )
    parser.add_argument("--prompt", default="prompts/atomic_pick_detector.md")
    parser.add_argument("--output-dir", default="artifacts/sweep")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--no-json-schema", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=3.0)
    args = parser.parse_args()

    api_key = get_api_key()
    if not api_key:
        print("Set OPENROUTER_API_KEY before calling OpenRouter.", file=sys.stderr)
        sys.exit(2)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.data_dir:
        zip_paths = sorted(Path(args.data_dir).rglob("*.zip"))
    elif args.zip:
        zip_paths = [Path(args.zip)]
    else:
        parser.error("Provide --zip or --data-dir.")
    if args.operations:
        wanted_operations = set(parse_csv(args.operations))
        zip_paths = [path for path in zip_paths if infer_operation(path) in wanted_operations]
        if not zip_paths:
            parser.error(f"No zips matched --operations {args.operations!r}.")
    zip_paths = limit_per_operation(zip_paths, args.max_per_operation)

    task_texts: dict[str, str] = {}
    if args.task_texts:
        task_texts = json.loads(Path(args.task_texts).read_text(encoding="utf-8"))

    models = parse_csv(args.models)
    strategies = parse_csv(args.strategies)
    layouts = parse_csv(args.layouts)
    indices = parse_indices(args.indices)
    total_requests = len(zip_paths) * len(models) * len(strategies) * len(layouts) * len(indices)
    completed_requests = 0
    sweep_started_at = time.perf_counter()

    all_records = []
    for zip_path in zip_paths:
        records = []
        for model in models:
            safe_model = model.replace("/", "__").replace(":", "_")
            for strategy in strategies:
                for layout in layouts:
                    for idx in indices:
                        operation = infer_operation(zip_path)
                        prefix = f"{operation}__" if operation else ""
                        layout_suffix = "" if layout == "auto" else f"__{layout}"
                        name = f"{prefix}{zip_path.stem}__{safe_model}__{strategy}{layout_suffix}__f{idx}.json"
                        output_path = output_dir / name
                        if args.skip_existing and output_path.exists():
                            wrapped = json.loads(output_path.read_text(encoding="utf-8"))
                            records.append(wrapped)
                            all_records.append(wrapped)
                            completed_requests += 1
                            print(
                                f"{progress_line(completed_requests, total_requests, sweep_started_at)} "
                                f"skip episode={zip_path.name} model={model} strategy={strategy} "
                                f"layout={layout} frame={idx}",
                                flush=True,
                            )
                            continue

                        print(
                            f"{progress_line(completed_requests, total_requests, sweep_started_at)} "
                            f"running operation={operation} episode={zip_path.name} model={model} "
                            f"strategy={strategy} layout={layout} frame={idx}",
                            flush=True,
                        )
                        request_started = time.perf_counter()
                        for attempt in range(args.retries + 1):
                            try:
                                image_parts, metadata = build_image_parts(
                                    zip_path,
                                    idx,
                                    strategy,
                                    layout=layout,
                                    task_text=task_texts.get(zip_path.stem, ""),
                                )
                                payload = build_payload(
                                    model,
                                    Path(args.prompt),
                                    image_parts,
                                    metadata,
                                    structured_output=not args.no_json_schema,
                                )
                                started = time.perf_counter()
                                response = call_openrouter(payload, api_key)
                                elapsed_seconds = time.perf_counter() - started
                                total_elapsed_seconds = time.perf_counter() - request_started
                                result = parse_model_json(response)
                                wrapped = {
                                    "model": model,
                                    "strategy": strategy,
                                    "layout": metadata.get("layout", layout),
                                    "frame": idx,
                                    "episode": zip_path.name,
                                    "operation": metadata.get("operation", ""),
                                    "elapsed_seconds": elapsed_seconds,
                                    "total_elapsed_seconds": total_elapsed_seconds,
                                    "attempts": attempt + 1,
                                    "result": result,
                                }
                                output_path.write_text(
                                    json.dumps(wrapped, indent=2, ensure_ascii=False) + "\n",
                                    encoding="utf-8",
                                )
                                records.append(wrapped)
                                all_records.append(wrapped)
                                completed_requests += 1
                                print(
                                    f"{progress_line(completed_requests, total_requests, sweep_started_at)} -> "
                                    f"{result.get('status')} "
                                    f"p={result.get('success_probability')} "
                                    f"progress={result.get('progress')} "
                                    f"request={elapsed_seconds:.2f}s total={total_elapsed_seconds:.2f}s",
                                    flush=True,
                                )
                                break
                            except Exception as exc:
                                print(f"  !! attempt {attempt + 1}: {exc}", file=sys.stderr)
                                if attempt >= args.retries:
                                    completed_requests += 1
                                    print(
                                        f"{progress_line(completed_requests, total_requests, sweep_started_at)} "
                                        f"failed operation={operation} episode={zip_path.name} "
                                        f"strategy={strategy} layout={layout} frame={idx}",
                                        flush=True,
                                    )
                                    if not args.continue_on_error:
                                        raise
                                else:
                                    time.sleep(args.retry_delay * (attempt + 1))

        operation = infer_operation(zip_path)
        prefix = f"{operation}__" if operation else ""
        summary_path = output_dir / f"{prefix}{zip_path.stem}__summary.json"
        summary_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"summary: {summary_path}")

    all_summary_path = output_dir / "all_episodes__summary.json"
    all_summary_path.write_text(json.dumps(all_records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"all summary: {all_summary_path}")


if __name__ == "__main__":
    main()
