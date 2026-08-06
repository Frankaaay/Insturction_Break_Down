import argparse
import subprocess
import tempfile
from pathlib import Path

from extract_mcap_frames import find_ffmpeg, write_h264_stream


CAMERAS = {
    "head_left": "/camera/coracam_head/left_h264/video",
    "head_right": "/camera/coracam_head/right_h264/video",
    "left_wrist_left": "/camera/coracam_lefthand/left_h264/video",
    "left_wrist_right": "/camera/coracam_lefthand/right_h264/video",
    "right_wrist_left": "/camera/coracam_righthand/left_h264/video",
    "right_wrist_right": "/camera/coracam_righthand/right_h264/video",
}


def remux_h264_to_mp4(
    ffmpeg: str,
    stream_path: Path,
    output_path: Path,
    fps: float,
    overwrite: bool,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg,
            "-y" if overwrite else "-n",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts",
            "-r",
            str(fps),
            "-i",
            str(stream_path),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract H.264 camera streams from a UMI MCAP and remux them to MP4 without re-encoding."
    )
    parser.add_argument("mcap", type=Path, help="Path to episode.mcap")
    parser.add_argument(
        "--camera",
        action="append",
        choices=sorted(CAMERAS),
        help="Camera to extract. Repeat for multiple cameras; defaults to head_right.",
    )
    parser.add_argument("--all-cameras", action="store_true")
    parser.add_argument("-o", "--output-dir", type=Path, default=None)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--ffmpeg", default="")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    mcap_path = args.mcap.resolve()
    output_dir = (args.output_dir or (mcap_path.parent / "videos")).resolve()
    ffmpeg = find_ffmpeg(args.ffmpeg)
    selected = list(CAMERAS) if args.all_cameras else (args.camera or ["head_right"])

    with tempfile.TemporaryDirectory(prefix="mcap_videos_") as tmp_name:
        tmp_dir = Path(tmp_name)
        for camera in selected:
            raw_path = tmp_dir / f"{camera}.h264"
            times_ns = write_h264_stream(mcap_path, CAMERAS[camera], raw_path)
            if not times_ns or not raw_path.exists() or raw_path.stat().st_size == 0:
                raise RuntimeError(f"No H.264 payloads found for {camera}: {CAMERAS[camera]}")

            output_path = output_dir / f"{camera}.mp4"
            remux_h264_to_mp4(ffmpeg, raw_path, output_path, args.fps, args.overwrite)
            duration_s = (times_ns[-1] - times_ns[0]) / 1_000_000_000
            print(
                f"{camera}: {len(times_ns)} packets, {duration_s:.3f}s -> "
                f"{output_path} ({output_path.stat().st_size} bytes)"
            )


if __name__ == "__main__":
    main()
