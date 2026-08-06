import argparse
import csv
import html
import json
from collections import defaultdict
from pathlib import Path


ONLINE_SUCCESS_THRESHOLD = 0.75
INTERVENTION_PENALTY = 0.75
CONFIRMATION_FRAMES = 2


def online_success_score(record: dict) -> float:
    penalty = INTERVENTION_PENALTY if record["should_intervene"] else 0.0
    return clamp(
        0.60 * record["success_probability"]
        + 0.40 * record["progress"]
        - penalty
    )


def read_records(input_dir: Path) -> list[dict]:
    records = []
    for path in sorted(input_dir.glob("*.json")):
        if path.name.endswith("__summary.json") or path.name == "all_episodes__summary.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if "result" not in data:
            continue
        result = data["result"]
        record = {
            "episode": data["episode"],
            "operation": data.get("operation") or "unknown",
            "model": data["model"],
            "strategy": data["strategy"],
            "layout": data.get("layout", "auto"),
            "frame": int(data["frame"]),
            "elapsed_seconds": data.get("elapsed_seconds"),
            "total_elapsed_seconds": data.get("total_elapsed_seconds"),
            "attempts": data.get("attempts"),
            "status": result.get("status", ""),
            "success_probability": float(result.get("success_probability", 0)),
            "failure_probability": float(result.get("failure_probability", 0)),
            "progress": float(result.get("progress", 0)),
            "should_intervene": bool(result.get("should_intervene", False)),
            "confidence": float(result.get("confidence", 0)),
        }
        record["online_success_score"] = online_success_score(record)
        record["online_success"] = record["online_success_score"] >= ONLINE_SUCCESS_THRESHOLD
        records.append(record)
    return records


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def mean(values: list[float]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def first_confirmed_frame(items: list[dict], predicate) -> int | None:
    streak = 0
    for row in items:
        if predicate(row):
            streak += 1
            if streak >= CONFIRMATION_FRAMES:
                return row["frame"]
        else:
            streak = 0
    return None


def strategy_metrics(records: list[dict]) -> list[dict]:
    by_strategy_episode = defaultdict(list)
    for record in records:
        operation = record["operation"]
        model = record["model"]
        strategy_key = f"{record['strategy']}|{record.get('layout', 'auto')}"
        by_strategy_episode[(operation, model, strategy_key, record["episode"])].append(record)
        by_strategy_episode[("ALL", model, strategy_key, f"{operation}/{record['episode']}")].append(record)

    grouped = defaultdict(list)
    for (operation, model, strategy, episode), items in by_strategy_episode.items():
        items = sorted(items, key=lambda row: row["frame"])
        if not items:
            continue
        final = items[-1]
        first_success = next((row["frame"] for row in items if row["status"] == "succeeded"), None)
        first_confirmed_success = first_confirmed_frame(
            items,
            lambda row: row["online_success"] and not row["should_intervene"],
        )
        first_confirmed_intervention = first_confirmed_frame(
            items,
            lambda row: row["should_intervene"] and row["failure_probability"] >= 0.80,
        )
        intervention = any(
            row["should_intervene"] or row["status"] in {"at_risk", "failed"} for row in items
        )
        progress_drops = [
            max(0.0, items[i - 1]["progress"] - items[i]["progress"])
            for i in range(1, len(items))
        ]
        prob_drops = [
            max(0.0, items[i - 1]["success_probability"] - items[i]["success_probability"])
            for i in range(1, len(items))
        ]
        smoothness = 1.0 - clamp((sum(progress_drops) + sum(prob_drops)) / max(1, len(items) - 1))
        elapsed_mean = mean([row["elapsed_seconds"] for row in items])
        total_elapsed_mean = mean([row["total_elapsed_seconds"] for row in items])
        attempts_mean = mean([row["attempts"] for row in items])

        grouped[(operation, model, strategy)].append(
            {
                "episode": episode,
                "final_success": final["status"] == "succeeded",
                "final_online_success": final["online_success"],
                "final_online_success_score": final["online_success_score"],
                "confirmed_online_success": first_confirmed_success is not None,
                "confirmed_false_intervention": first_confirmed_intervention is not None,
                "final_progress": final["progress"],
                "final_success_probability": final["success_probability"],
                "first_success_frame": first_success,
                "first_confirmed_success_frame": first_confirmed_success,
                "first_confirmed_intervention_frame": first_confirmed_intervention,
                "false_intervention": intervention,
                "smoothness": smoothness,
                "avg_elapsed_seconds": elapsed_mean,
                "avg_total_elapsed_seconds": total_elapsed_mean,
                "avg_attempts": attempts_mean,
            }
        )

    metrics = []
    for (operation, model, strategy), items in sorted(grouped.items()):
        n = len(items)
        final_success_rate = sum(item["final_success"] for item in items) / n
        final_online_success_rate = sum(item["final_online_success"] for item in items) / n
        confirmed_online_success_rate = sum(item["confirmed_online_success"] for item in items) / n
        no_confirmed_false_intervention_rate = (
            1 - sum(item["confirmed_false_intervention"] for item in items) / n
        )
        final_online_success_score = sum(item["final_online_success_score"] for item in items) / n
        no_false_intervention_rate = 1 - sum(item["false_intervention"] for item in items) / n
        smoothness_score = sum(item["smoothness"] for item in items) / n
        final_progress = sum(item["final_progress"] for item in items) / n
        final_success_probability = sum(item["final_success_probability"] for item in items) / n
        avg_elapsed_seconds = mean([item["avg_elapsed_seconds"] for item in items])
        avg_total_elapsed_seconds = mean([item["avg_total_elapsed_seconds"] for item in items])
        avg_attempts = mean([item["avg_attempts"] for item in items])
        overall_score = (
            0.30 * final_online_success_rate
            + 0.15 * confirmed_online_success_rate
            + 0.25 * smoothness_score
            + 0.15 * no_confirmed_false_intervention_rate
            + 0.10 * final_online_success_score
            + 0.05 * final_success_rate
        )
        metrics.append(
            {
                "operation": operation,
                "model": model,
                "strategy": strategy,
                "episodes": n,
                "overall_score": overall_score,
                "final_success_rate": final_success_rate,
                "final_online_success_rate": final_online_success_rate,
                "confirmed_online_success_rate": confirmed_online_success_rate,
                "final_online_success_score": final_online_success_score,
                "final_success_probability": final_success_probability,
                "final_progress": final_progress,
                "no_false_intervention_rate": no_false_intervention_rate,
                "no_confirmed_false_intervention_rate": no_confirmed_false_intervention_rate,
                "smoothness_score": smoothness_score,
                "avg_elapsed_seconds": avg_elapsed_seconds,
                "avg_total_elapsed_seconds": avg_total_elapsed_seconds,
                "avg_attempts": avg_attempts,
            }
        )
    return sorted(metrics, key=lambda row: (row["operation"], -row["overall_score"]))


def write_csv(records: list[dict], metrics: list[dict], output_dir: Path) -> None:
    with (output_dir / "records.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)

    with (output_dir / "strategy_scores.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(metrics[0].keys()))
        writer.writeheader()
        writer.writerows(metrics)


def sparkline(values: list[float], width: int = 280, height: int = 72, color: str = "#2563eb") -> str:
    if not values:
        return ""
    if len(values) == 1:
        points = [(width / 2, height * (1 - values[0]))]
    else:
        points = [
            (i * width / (len(values) - 1), height * (1 - clamp(value)))
            for i, value in enumerate(values)
        ]
    point_text = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"><title>{i}: {values[i]:.2f}</title></circle>'
        for i, (x, y) in enumerate(points)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img"><polyline points="{point_text}" fill="none" stroke="{color}" '
        f'stroke-width="3"/>{dots}</svg>'
    )


def write_html(records: list[dict], metrics: list[dict], output_path: Path) -> None:
    by_episode_strategy = defaultdict(list)
    for record in records:
        strategy_key = f"{record['strategy']}|{record.get('layout', 'auto')}"
        by_episode_strategy[(record["operation"], record["episode"], record["model"], strategy_key)].append(record)

    rows = []
    for metric in metrics:
        cells = []
        for key in metric.keys():
            value = metric[key]
            if isinstance(value, float):
                cells.append(f"<td>{value:.3f}</td>")
            elif value is None:
                cells.append("<td></td>")
            else:
                cells.append(f"<td>{html.escape(str(value))}</td>")
        rows.append(
            "<tr>"
            + "".join(cells)
            + "</tr>"
        )

    cards = []
    for (operation, episode, model, strategy), items in sorted(by_episode_strategy.items()):
        items = sorted(items, key=lambda row: row["frame"])
        success_values = [row["success_probability"] for row in items]
        progress_values = [row["progress"] for row in items]
        online_success_values = [row["online_success_score"] for row in items]
        elapsed_values = [row["elapsed_seconds"] for row in items if row["elapsed_seconds"] is not None]
        total_elapsed_values = [
            row["total_elapsed_seconds"] for row in items if row["total_elapsed_seconds"] is not None
        ]
        statuses = " ".join(f"{row['frame']}:{row['status']}" for row in items)
        if elapsed_values:
            elapsed_label = f"avg request {sum(elapsed_values) / len(elapsed_values):.2f}s"
        else:
            elapsed_label = "avg request n/a"
        if total_elapsed_values:
            elapsed_label += f", avg total {sum(total_elapsed_values) / len(total_elapsed_values):.2f}s"
        cards.append(
            f"""
            <section class="card">
              <h3>{html.escape(operation)} / {html.escape(episode)} / {html.escape(model)} / {html.escape(strategy)}</h3>
              <div class="charts">
                <div><label>success_probability</label>{sparkline(success_values, color="#2563eb")}</div>
                <div><label>progress</label>{sparkline(progress_values, color="#16a34a")}</div>
                <div><label>online_success_score</label>{sparkline(online_success_values, color="#9333ea")}</div>
              </div>
              <p class="elapsed">{html.escape(elapsed_label)}</p>
              <p class="statuses">{html.escape(statuses)}</p>
            </section>
            """
        )

    output_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Robot Failure Detector Sweep Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #111827; }}
    h1 {{ font-size: 24px; margin-bottom: 6px; }}
    h2 {{ margin-top: 28px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 7px 8px; text-align: left; font-size: 13px; }}
    th {{ background: #f3f4f6; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 14px; }}
    .card {{ border: 1px solid #d1d5db; border-radius: 6px; padding: 12px; }}
    .card h3 {{ font-size: 14px; margin: 0 0 10px; }}
    .charts {{ display: grid; grid-template-columns: 1fr; gap: 8px; }}
    label {{ display: block; font-size: 12px; color: #4b5563; margin-bottom: 2px; }}
    .statuses {{ font-size: 12px; color: #374151; line-height: 1.45; }}
    .note {{ max-width: 920px; color: #4b5563; }}
  </style>
</head>
<body>
  <h1>Robot Failure Detector Sweep Report</h1>
  <p class="note">Scores are for successful demonstration episodes. They estimate online-monitoring usefulness:
  final online success recognition, no false intervention, curve smoothness, and final status. The online success
  score is 0.60 * success_probability + 0.40 * progress, with a {INTERVENTION_PENALTY:.2f} penalty when
  should_intervene is true; threshold is {ONLINE_SUCCESS_THRESHOLD:.2f}. Confirmed success/intervention requires
  {CONFIRMATION_FRAMES} consecutive API results. Request latency is reported separately as average successful
  HTTP-call time, plus total time including retries when available, and is not included in the overall score.
  Failure detection still needs failed examples.</p>
  <h2>Strategy Scores</h2>
  <table>
    <thead><tr>{''.join(f'<th>{html.escape(key)}</th>' for key in metrics[0].keys())}</tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <h2>Curves</h2>
  <div class="grid">{''.join(cards)}</div>
</body>
</html>
""",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", default="artifacts/qwen_pick_v2")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir) if args.output_dir else input_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    records = read_records(input_dir)
    if not records:
        raise SystemExit(f"No sweep records found in {input_dir}")
    metrics = strategy_metrics(records)

    write_csv(records, metrics, output_dir)
    (output_dir / "strategy_scores.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_html(records, metrics, output_dir / "report.html")
    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    print(f"report: {output_dir / 'report.html'}")


if __name__ == "__main__":
    main()
