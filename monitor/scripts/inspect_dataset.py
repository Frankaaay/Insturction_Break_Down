import argparse
import io
import json
import os
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw


LANES = [
    ("head/right", "frames/head_right"),
    ("left_wrist/left", "frames/left_wrist_left"),
    ("right_wrist/right", "frames/right_wrist_right"),
]


def load_manifest(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("frames/manifest.json"))


def summarize(data_dir: Path) -> None:
    zips = sorted(data_dir.glob("*.zip"))
    print(f"zip_count: {len(zips)}")
    for zip_path in zips:
        manifest = load_manifest(zip_path)
        lanes = [lane["laneKey"] for lane in manifest["lanes"]]
        last_times = [lane["frames"][-1]["timeMs"] for lane in manifest["lanes"]]
        print(
            f"{zip_path.name}: framesPerLane={manifest.get('framesPerLane')} "
            f"lanes={lanes} lastMs={last_times}"
        )


def make_contact_sheet(zip_path: Path, output_path: Path, max_width: int = 220) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        manifest = json.loads(zf.read("frames/manifest.json"))
        frames_per_lane = int(manifest["framesPerLane"])
        cell_w = max_width
        image_h = 160
        label_h = 24
        cell_h = image_h + label_h

        rows = []
        for lane_key, folder in LANES:
            row = []
            for idx in range(frames_per_lane):
                frame_name = f"{folder}/{idx:02d}.jpg"
                img = Image.open(io.BytesIO(zf.read(frame_name))).convert("RGB")
                img.thumbnail((cell_w, image_h))
                canvas = Image.new("RGB", (cell_w, cell_h), "white")
                canvas.paste(img, ((cell_w - img.width) // 2, label_h))
                draw = ImageDraw.Draw(canvas)
                draw.text((6, 5), f"{lane_key} {idx}", fill=(0, 0, 0))
                row.append(canvas)
            rows.append(row)

    sheet = Image.new("RGB", (cell_w * frames_per_lane, cell_h * len(rows)), "white")
    for row_idx, row in enumerate(rows):
        for col_idx, img in enumerate(row):
            sheet.paste(img, (col_idx * cell_w, row_idx * cell_h))
    sheet.save(output_path, quality=90)
    print(output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--zip", dest="zip_name")
    parser.add_argument("--contact-sheet", action="store_true")
    parser.add_argument("--output", default="artifacts/sample_contact_sheet.jpg")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if args.contact_sheet:
        zip_path = Path(args.zip_name) if args.zip_name else sorted(data_dir.glob("*.zip"))[0]
        make_contact_sheet(zip_path, Path(args.output))
    else:
        summarize(data_dir)


if __name__ == "__main__":
    main()
