import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from flatbuffers import encode
from flatbuffers.number_types import UOffsetTFlags
from flatbuffers.table import Table
from mcap.reader import make_reader


DEFAULT_CAMERAS = {
    "/camera/coracam_head/right_h264/video": ("head/right", "frames/head_right"),
    "/camera/coracam_lefthand/left_h264/video": ("left_wrist/left", "frames/left_wrist_left"),
    "/camera/coracam_righthand/right_h264/video": ("right_wrist/right", "frames/right_wrist_right"),
}

ALL_CAMERAS = {
    "/camera/coracam_head/left_h264/video": ("head/left", "frames/head_left"),
    "/camera/coracam_head/right_h264/video": ("head/right", "frames/head_right"),
    "/camera/coracam_lefthand/left_h264/video": ("left_wrist/left", "frames/left_wrist_left"),
    "/camera/coracam_lefthand/right_h264/video": ("left_wrist/right", "frames/left_wrist_right"),
    "/camera/coracam_righthand/left_h264/video": ("right_wrist/left", "frames/right_wrist_left"),
    "/camera/coracam_righthand/right_h264/video": ("right_wrist/right", "frames/right_wrist_right"),
}


def compressed_video_data(message_data: bytes) -> bytes:
    """Return foxglove.CompressedVideo.data from its flatbuffer payload."""
    buf = bytearray(message_data)
    root = encode.Get(UOffsetTFlags.packer_type, buf, 0)
    table = Table(buf, root)
    data_field = table.Offset(4 + 2 * 2)
    if not data_field:
        return b""
    start = table.Vector(data_field)
    length = table.VectorLen(data_field)
    return bytes(buf[start : start + length])


def find_ffmpeg(explicit: str = "") -> str:
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(
            "ffmpeg was not found. Install ffmpeg or `pip install imageio-ffmpeg`."
        ) from exc


def write_h264_stream(mcap_path: Path, topic: str, out_path: Path) -> list[int]:
    times_ns = []
    with mcap_path.open("rb") as f, out_path.open("wb") as out:
        reader = make_reader(f)
        for schema, channel, message in reader.iter_messages(topics=[topic]):
            if not schema or schema.name != "foxglove.CompressedVideo":
                continue
            payload = compressed_video_data(message.data)
            if payload:
                out.write(payload)
                times_ns.append(message.log_time)
    return times_ns


def decode_all_frames(ffmpeg: str, stream_path: Path, out_dir: Path, quality: int) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "%06d.jpg"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(stream_path),
            "-q:v",
            str(quality),
            str(pattern),
        ],
        check=True,
    )
    return sorted(out_dir.glob("*.jpg"))


def sampled_indices(total: int, samples: int) -> list[int]:
    if total <= 0:
        return []
    if samples <= 1:
        return [0]
    if total <= samples:
        return list(range(total))
    return [round(i * (total - 1) / (samples - 1)) for i in range(samples)]


def copy_sampled_frames(decoded: list[Path], dst_dir: Path, samples: int) -> list[dict]:
    dst_dir.mkdir(parents=True, exist_ok=True)
    selected = sampled_indices(len(decoded), samples)
    frames = []
    for out_idx, src_idx in enumerate(selected):
        dst = dst_dir / f"{out_idx:02d}.jpg"
        shutil.copyfile(decoded[src_idx], dst)
        frames.append({"index": out_idx, "sourceFrame": src_idx})
    return frames


def extract_frames(
    mcap_path: Path,
    output_dir: Path,
    cameras: dict[str, tuple[str, str]],
    samples: int,
    jpeg_quality: int,
    ffmpeg: str,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source": str(mcap_path),
        "framesPerLane": samples,
        "lanes": [],
    }

    with tempfile.TemporaryDirectory(prefix="mcap_frames_") as tmp_name:
        tmp = Path(tmp_name)
        for topic, (lane_key, folder) in cameras.items():
            raw_path = tmp / f"{lane_key.replace('/', '_')}.h264"
            times_ns = write_h264_stream(mcap_path, topic, raw_path)
            if not times_ns or raw_path.stat().st_size == 0:
                print(f"skip missing/empty topic: {topic}")
                continue

            decoded_dir = tmp / f"{lane_key.replace('/', '_')}_decoded"
            decoded = decode_all_frames(ffmpeg, raw_path, decoded_dir, jpeg_quality)
            frames = copy_sampled_frames(decoded, output_dir / folder, samples)

            selected = sampled_indices(len(decoded), len(frames))
            for frame, src_idx in zip(frames, selected):
                if times_ns:
                    msg_idx = min(src_idx, len(times_ns) - 1)
                    frame["timeMs"] = round((times_ns[msg_idx] - times_ns[0]) / 1_000_000)

            manifest["lanes"].append(
                {
                    "laneKey": lane_key,
                    "topic": topic,
                    "folder": folder,
                    "decodedFrameCount": len(decoded),
                    "frames": frames,
                }
            )

    manifest_path = output_dir / "frames" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract sampled jpg frames from Discover/UMI MCAP H.264 topics.")
    parser.add_argument("mcap", type=Path, help="Path to episode.mcap.")
    parser.add_argument("-o", "--output-dir", type=Path, default=None, help="Episode output directory. Defaults to MCAP parent.")
    parser.add_argument("--samples", type=int, default=8, help="Number of evenly sampled frames per camera lane.")
    parser.add_argument("--all-cameras", action="store_true", help="Extract all 6 stereo camera streams instead of the 3 lanes used by the detector.")
    parser.add_argument("--ffmpeg", default="", help="Path to ffmpeg executable. Defaults to PATH or imageio-ffmpeg.")
    parser.add_argument("--jpeg-quality", type=int, default=3, help="ffmpeg q:v value; lower is higher quality.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    mcap_path = args.mcap.resolve()
    output_dir = (args.output_dir or mcap_path.parent).resolve()
    cameras = ALL_CAMERAS if args.all_cameras else DEFAULT_CAMERAS
    ffmpeg = find_ffmpeg(args.ffmpeg)
    manifest = extract_frames(mcap_path, output_dir, cameras, args.samples, args.jpeg_quality, ffmpeg)
    print(f"wrote {output_dir / 'frames' / 'manifest.json'}")
    for lane in manifest["lanes"]:
        print(f"{lane['laneKey']}: {len(lane['frames'])} sampled from {lane['decodedFrameCount']} decoded frames")


if __name__ == "__main__":
    main()
