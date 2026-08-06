import argparse
import json
import time
from collections import Counter
from pathlib import Path


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def infer_total(args: argparse.Namespace) -> int:
    if args.total:
        return args.total
    if args.data_dir:
        zip_count = len(list(Path(args.data_dir).rglob("*.zip")))
        return zip_count * args.models * args.strategies * args.indices
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="artifacts/qwen_all_ops")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--total", type=int, default=0)
    parser.add_argument("--models", type=int, default=1)
    parser.add_argument("--strategies", type=int, default=3)
    parser.add_argument("--indices", type=int, default=8)
    parser.add_argument("--strategy", default="", help="Only count files for one strategy, such as start_current.")
    parser.add_argument("--tail", type=int, default=5)
    args = parser.parse_args()

    output_dir = Path(args.dir)
    files = sorted(
        [path for path in output_dir.glob("*.json") if "summary" not in path.name],
        key=lambda path: path.stat().st_mtime,
    )
    if args.strategy:
        files = [path for path in files if f"__{args.strategy}__" in path.name]
    done = len(files)
    total = infer_total(args)
    now = time.time()

    operation_counts = Counter(path.name.split("__", 1)[0] for path in files)
    print(f"done: {done} / {total}" if total else f"done: {done}")
    if total:
        print(f"progress: {done / total * 100:.1f}%")

    if len(files) >= 2:
        first_time = files[0].stat().st_mtime
        last_time = files[-1].stat().st_mtime
        elapsed = max(1.0, last_time - first_time)
        rate = (len(files) - 1) / elapsed
        print(f"observed_elapsed: {format_duration(now - first_time)}")
        print(f"write_rate: {rate * 60:.2f} results/min")
        if total and rate > 0:
            remaining = max(0, total - done)
            print(f"eta: {format_duration(remaining / rate)}")
    else:
        print("eta: n/a until at least two result files exist")

    if operation_counts:
        print("by_operation:")
        for operation, count in sorted(operation_counts.items()):
            print(f"  {operation}: {count}")

    if args.tail and files:
        print("latest:")
        for path in files[-args.tail :]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result = data.get("result", {})
                status = result.get("status", "?")
                probability = result.get("success_probability", "?")
                elapsed = data.get("elapsed_seconds")
                elapsed_text = f"{elapsed:.2f}s" if isinstance(elapsed, (int, float)) else "n/a"
                print(f"  {path.name}: {status} p={probability} request={elapsed_text}")
            except Exception as exc:
                print(f"  {path.name}: unreadable ({exc})")


if __name__ == "__main__":
    main()
