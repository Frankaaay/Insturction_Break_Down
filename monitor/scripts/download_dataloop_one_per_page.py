import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional, Set, Tuple


def run(cmd, *, dry_run: bool = False) -> subprocess.CompletedProcess:
    print("+ " + " ".join(cmd), flush=True)
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def add_global_args(cmd, args: argparse.Namespace):
    if args.base_url:
        cmd.extend(["--base-url", args.base_url])
    return cmd


def add_list_filters(cmd, args: argparse.Namespace):
    for project in args.project:
        cmd.extend(["-p", str(project)])
    if args.time_from:
        cmd.extend(["--time-from", args.time_from])
    if args.time_to:
        cmd.extend(["--time-to", args.time_to])
    for tag in args.tag:
        cmd.extend(["--tag", tag])
    if args.search:
        cmd.extend(["--search", args.search])
    if args.sort:
        cmd.extend(["--sort", args.sort])
    return cmd


def episode_id(item: object) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    for key in ("id", "episode_id", "episodeId", "sample_id", "sampleId"):
        value = item.get(key)
        if value:
            return str(value)
    return None


def list_page(args: argparse.Namespace, page: int) -> Tuple[Optional[str], object]:
    cmd = [args.dl_bin]
    add_global_args(cmd, args)
    cmd.extend(["episode", "list"])
    add_list_filters(cmd, args)
    cmd.extend(["--page", str(page), "--page-size", str(args.list_page_size), "--json"])

    proc = run(cmd, dry_run=args.dry_run)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise RuntimeError(f"list failed on page {page} with exit code {proc.returncode}")

    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"page {page} did not return valid JSON: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        return None, payload

    index = args.item_index - 1
    if index >= len(payload):
        return None, payload
    return episode_id(payload[index]), payload


def download_episode(args: argparse.Namespace, episode: str) -> int:
    cmd = [args.dl_bin]
    add_global_args(cmd, args)
    cmd.extend(["episode", "download", episode, "-o", str(args.output)])

    if args.download_mode == "all":
        cmd.append("--all")
    elif args.download_mode == "sub-dir":
        for sub_dir in args.sub_dir:
            cmd.extend(["--sub-dir", sub_dir])

    cmd.extend(["-j", str(args.concurrent), "--retries", str(args.retries)])
    if args.progress:
        cmd.append("--progress")
    if args.json:
        cmd.append("--json")

    proc = run(cmd, dry_run=args.dry_run)
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc.returncode


def load_seen(manifest_path: Path) -> Set[str]:
    if not manifest_path.exists():
        return set()
    seen: Set[str] = set()
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        episode = row.get("episode_id")
        if episode and row.get("download_exit_code") == 0:
            seen.add(str(episode))
    return seen


def append_manifest(manifest_path: Path, row: dict) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sample one episode from each dataloop list page and download it."
    )
    parser.add_argument("--dl-bin", default="dl", help="Path to dl executable.")
    parser.add_argument("--base-url", help="Dataloop base URL, e.g. http://192.168.215.158.")
    parser.add_argument("-p", "--project", action="append", required=True, help="Project ID. Can repeat.")
    parser.add_argument("--pages", type=int, default=390, help="Number of pages to scan.")
    parser.add_argument("--page-from", type=int, default=1, help="First page number, inclusive.")
    parser.add_argument("--list-page-size", type=int, default=10, help="Use the same page size as the website.")
    parser.add_argument("--item-index", type=int, default=1, help="1-based item index to pick from each page.")
    parser.add_argument("--time-from")
    parser.add_argument("--time-to")
    parser.add_argument("--tag", action="append", default=[], help="Tag filter KEY=VAL. Can repeat.")
    parser.add_argument("--search")
    parser.add_argument("--sort", default="-creation_time")
    parser.add_argument("-o", "--output", required=True, type=Path, help="Existing writable output directory.")
    parser.add_argument(
        "--download-mode",
        choices=["root-mcap", "all", "sub-dir"],
        default="all",
        help="root-mcap downloads only root *.mcap; all recursively downloads everything.",
    )
    parser.add_argument("--sub-dir", action="append", default=[], help="Subdirectory to download. Can repeat.")
    parser.add_argument("-j", "--concurrent", type=int, default=4)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--json", action="store_true", help="Ask download command to print JSON.")
    parser.add_argument("--manifest", type=Path, help="JSONL log path. Defaults to output/download_manifest.jsonl.")
    parser.add_argument("--skip-seen", action="store_true", help="Skip episodes already logged as successful.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()

    if args.item_index < 1:
        parser.error("--item-index must be >= 1")
    if args.download_mode == "sub-dir" and not args.sub_dir:
        parser.error("--download-mode sub-dir requires at least one --sub-dir")
    if not args.dry_run:
        args.output.mkdir(parents=True, exist_ok=True)
    if args.manifest is None:
        args.manifest = args.output / "download_manifest.jsonl"
    return args


def main() -> None:
    args = parse_args()
    page_to = args.page_from + args.pages - 1
    seen = load_seen(args.manifest) if args.skip_seen else set()

    ok = 0
    failed = 0
    skipped = 0

    for page in range(args.page_from, page_to + 1):
        print(f"\n[{page - args.page_from + 1}/{args.pages}] list page {page}", flush=True)
        try:
            episode, payload = list_page(args, page)
            if not episode:
                skipped += 1
                append_manifest(
                    args.manifest,
                    {"page": page, "episode_id": None, "status": "no_item", "raw": payload},
                )
                print(f"page {page}: no item at index {args.item_index}")
                continue

            if episode in seen:
                skipped += 1
                print(f"page {page}: skip seen episode {episode}")
                continue

            exit_code = download_episode(args, episode)
            row = {"page": page, "episode_id": episode, "download_exit_code": exit_code}
            append_manifest(args.manifest, row)
            if exit_code == 0:
                ok += 1
            else:
                failed += 1
                if not args.continue_on_error:
                    raise RuntimeError(f"download failed for {episode} with exit code {exit_code}")
        except Exception as exc:
            failed += 1
            append_manifest(args.manifest, {"page": page, "error": str(exc)})
            print(f"ERROR: {exc}", file=sys.stderr)
            if not args.continue_on_error:
                raise

    print(f"\nDone. ok={ok} failed={failed} skipped={skipped} manifest={args.manifest}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
