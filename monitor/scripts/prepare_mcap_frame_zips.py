import argparse
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from extract_mcap_frames import DEFAULT_CAMERAS, ALL_CAMERAS, extract_frames, find_ffmpeg


def zip_directory(source_dir: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(source_dir).as_posix())


def prepare_one(
    mcap_path: Path,
    output_dir: Path,
    samples: int,
    cameras: dict[str, tuple[str, str]],
    jpeg_quality: int,
    ffmpeg: str,
    skip_existing: bool,
) -> Path:
    case_id = mcap_path.parent.name
    zip_path = output_dir / f"{case_id}.zip"
    if skip_existing and zip_path.exists():
        return zip_path

    with tempfile.TemporaryDirectory(prefix="mcap_frame_zip_") as tmp_name:
        tmp = Path(tmp_name)
        extract_frames(mcap_path, tmp, cameras, samples, jpeg_quality, ffmpeg)
        info = {
            "case_id": case_id,
            "source_mcap": str(mcap_path),
            "samples": samples,
            "camera_count": len(cameras),
        }
        (tmp / "source_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        zip_directory(tmp, zip_path)
    return zip_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert downloaded episode.mcap files into frame zips for OpenRouter sweeps.")
    parser.add_argument("--input-dir", type=Path, required=True, help="Directory containing case subdirectories with episode.mcap.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory where frame zips will be written.")
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--all-cameras", action="store_true")
    parser.add_argument("--jpeg-quality", type=int, default=3)
    parser.add_argument("--ffmpeg", default="")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of mcap files to convert.")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--clean-output", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    mcap_paths = sorted(args.input_dir.rglob("episode.mcap"))
    if args.limit > 0:
        mcap_paths = mcap_paths[: args.limit]
    if not mcap_paths:
        raise SystemExit(f"No episode.mcap files found under {args.input_dir}")

    if args.clean_output and args.output_dir.exists():
        shutil.rmtree(args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    cameras = ALL_CAMERAS if args.all_cameras else DEFAULT_CAMERAS
    ffmpeg = find_ffmpeg(args.ffmpeg)
    total = len(mcap_paths)
    for idx, mcap_path in enumerate(mcap_paths, 1):
        zip_path = prepare_one(
            mcap_path,
            args.output_dir,
            args.samples,
            cameras,
            args.jpeg_quality,
            ffmpeg,
            args.skip_existing,
        )
        print(f"[{idx}/{total}] {mcap_path} -> {zip_path}", flush=True)


if __name__ == "__main__":
    main()
