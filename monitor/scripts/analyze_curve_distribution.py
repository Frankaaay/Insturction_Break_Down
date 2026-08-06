import argparse
import csv
import html
import json
import math
from collections import defaultdict
from pathlib import Path


METRICS = ["success_probability", "progress", "online_success_score"]


def as_float(value: str) -> float | None:
    if value == "" or value is None:
        return None
    return float(value)


def percentile(values: list[float], q: float) -> float | None:
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    pos = (len(clean) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return clean[lo]
    return clean[lo] * (hi - pos) + clean[hi] * (pos - lo)


def mean(values: list[float]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def std(values: list[float]) -> float | None:
    clean = [value for value in values if value is not None]
    if len(clean) < 2:
        return 0.0 if clean else None
    m = mean(clean)
    return math.sqrt(sum((value - m) ** 2 for value in clean) / (len(clean) - 1))


def read_records(path: Path, strategy: str, layout: str) -> list[dict]:
    records = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("strategy") != strategy or row.get("layout") != layout:
                continue
            record = {
                "episode": row["episode"],
                "frame": int(row["frame"]),
                "status": row["status"],
                "should_intervene": row.get("should_intervene", "").lower() == "true",
            }
            for key in METRICS:
                record[key] = as_float(row.get(key, ""))
            records.append(record)
    return records


def frame_distribution(records: list[dict]) -> list[dict]:
    rows = []
    max_frame = max((record["frame"] for record in records), default=-1)
    for frame in range(max_frame + 1):
        part = [record for record in records if record["frame"] == frame]
        row = {"frame": frame, "n": len(part)}
        for metric in METRICS:
            values = [record[metric] for record in part]
            row[f"{metric}_mean"] = mean(values)
            row[f"{metric}_std"] = std(values)
            row[f"{metric}_p10"] = percentile(values, 0.10)
            row[f"{metric}_p50"] = percentile(values, 0.50)
            row[f"{metric}_p90"] = percentile(values, 0.90)
        rows.append(row)
    return rows


def episode_curves(records: list[dict]) -> dict[str, list[dict]]:
    grouped = defaultdict(list)
    for record in records:
        grouped[record["episode"]].append(record)
    return {episode: sorted(items, key=lambda row: row["frame"]) for episode, items in sorted(grouped.items())}


def svg_distribution(dist: list[dict], metric: str, width: int = 720, height: int = 220) -> str:
    if not dist:
        return ""
    frames = [row["frame"] for row in dist]
    max_frame = max(frames) if frames else 1

    def xy(frame: int, value: float) -> tuple[float, float]:
        x = 36 + frame * (width - 56) / max(1, max_frame)
        y = 12 + (1 - max(0.0, min(1.0, value))) * (height - 34)
        return x, y

    def points(suffix: str) -> str:
        pts = []
        for row in dist:
            value = row.get(f"{metric}_{suffix}")
            if value is not None:
                pts.append(xy(row["frame"], value))
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    p90 = [xy(row["frame"], row[f"{metric}_p90"]) for row in dist if row.get(f"{metric}_p90") is not None]
    p10 = [xy(row["frame"], row[f"{metric}_p10"]) for row in reversed(dist) if row.get(f"{metric}_p10") is not None]
    band_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in [*p90, *p10])
    ticks = "".join(
        f'<text x="{xy(frame, 0)[0]:.1f}" y="{height - 4}" text-anchor="middle">f{frame}</text>'
        for frame in frames
    )
    return f"""
    <svg viewBox="0 0 {width} {height}" width="{width}" height="{height}">
      <rect x="0" y="0" width="{width}" height="{height}" fill="#fff"/>
      <line x1="36" y1="12" x2="36" y2="{height-22}" stroke="#d1d5db"/>
      <line x1="36" y1="{height-22}" x2="{width-20}" y2="{height-22}" stroke="#d1d5db"/>
      <polygon points="{band_points}" fill="#bfdbfe" opacity="0.55"/>
      <polyline points="{points('mean')}" fill="none" stroke="#2563eb" stroke-width="3"/>
      <polyline points="{points('p50')}" fill="none" stroke="#111827" stroke-width="1.5" stroke-dasharray="4 4"/>
      <text x="4" y="18">1.0</text><text x="4" y="{height-24}">0.0</text>{ticks}
    </svg>
    """


def write_outputs(records: list[dict], output_dir: Path, strategy: str, layout: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    dist = frame_distribution(records)
    curves = episode_curves(records)

    with (output_dir / "curve_distribution.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = list(dist[0].keys()) if dist else ["frame", "n"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(dist)

    payload = {"strategy": strategy, "layout": layout, "distribution": dist, "episodes": curves}
    (output_dir / "curve_distribution.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    rows = []
    for row in dist:
        cells = []
        for key, value in row.items():
            if isinstance(value, float):
                cells.append(f"<td>{value:.3f}</td>")
            else:
                cells.append(f"<td>{html.escape(str(value))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")

    episode_rows = []
    for episode, items in curves.items():
        score = [row["online_success_score"] for row in items]
        success = [row["success_probability"] for row in items]
        statuses = " ".join(f"f{row['frame']}:{row['status']}" for row in items)
        episode_rows.append(
            "<tr>"
            f"<td>{html.escape(episode)}</td>"
            f"<td>{' '.join(f'{v:.2f}' for v in success if v is not None)}</td>"
            f"<td>{' '.join(f'{v:.2f}' for v in score if v is not None)}</td>"
            f"<td>{html.escape(statuses)}</td>"
            "</tr>"
        )

    output_html = output_dir / "curve_distribution.html"
    output_html.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Curve Distribution</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #111827; }}
    h1 {{ font-size: 24px; }}
    h2 {{ margin-top: 28px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 6px 8px; font-size: 12px; text-align: left; }}
    th {{ background: #f3f4f6; }}
    .chart {{ margin: 14px 0 24px; }}
    .note {{ color: #4b5563; max-width: 900px; }}
  </style>
</head>
<body>
  <h1>{html.escape(strategy)} | {html.escape(layout)}</h1>
  <p class="note">Blue band is p10-p90 across episodes, blue line is mean, dashed black is median.</p>
  <h2>success_probability</h2><div class="chart">{svg_distribution(dist, "success_probability")}</div>
  <h2>progress</h2><div class="chart">{svg_distribution(dist, "progress")}</div>
  <h2>online_success_score</h2><div class="chart">{svg_distribution(dist, "online_success_score")}</div>
  <h2>Frame Distribution</h2>
  <table><thead><tr>{''.join(f'<th>{html.escape(k)}</th>' for k in (dist[0].keys() if dist else []))}</tr></thead><tbody>{''.join(rows)}</tbody></table>
  <h2>Episode Curves</h2>
  <table><thead><tr><th>episode</th><th>success_probability</th><th>online_success_score</th><th>statuses</th></tr></thead><tbody>{''.join(episode_rows)}</tbody></table>
</body>
</html>
""",
        encoding="utf-8",
    )
    print(f"records={len(records)} episodes={len(curves)}")
    print(f"report: {output_html}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--strategy", default="start_sliding_window")
    parser.add_argument("--layout", default="separate")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    records = read_records(args.records, args.strategy, args.layout)
    if not records:
        raise SystemExit("No matching records found.")
    write_outputs(records, args.output_dir, args.strategy, args.layout)


if __name__ == "__main__":
    main()
