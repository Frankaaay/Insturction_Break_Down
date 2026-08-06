"""Build a standalone HTML report for the 4 fail-case 16-frame runs.

Reads per-frame result JSONs written by run_openrouter_sweep.py and renders,
per case: an SVG success_probability curve with status-coded markers and
segment bands, a Chinese segment-by-segment interpretation, and the full
per-frame evidence with head-camera thumbnails.

Usage: python scripts/build_fail_case_report.py
Output: artifacts/fail_case_report_16f.html
"""

import glob
import html
import json
from pathlib import Path

OUTPUT = Path("artifacts/fail_case_report_16f_task.html")

STATUS_LABEL = {
    "succeeded": "succeeded",
    "in_progress": "in_progress",
    "needs_more_observation": "needs_more_obs",
    "at_risk": "at_risk",
    "failed": "failed",
}

CASES = [
    {
        "key": "fail1",
        "title": "fail1 — Pick / 拿起 蓝色24号小球（注入任务文本）",
        "subtitle": "指令注入：“Pick up the Blue No. 24 small ball.” · evidence 现在能逐帧点名对象；中段不再有 0.65 的暧昧分",
        "dir": "artifacts/qwen_fail3_16f_pv2_task",
        "episode": "fail1.zip",
        "frames_dir": "fail_case_frames_16/fail1",
        "segments": [
            (0, 1, "接近与接触",
             "“the blue ball with black lines (Blue No. 24 small ball) is visible... not yet lifted”，0.30–0.40。对象身份从第一帧起就被点名确认，且明确“没有搬错对象的迹象”。"),
            (2, 11, "反复尝试、始终未提起",
             "十帧里九帧稳定给 0.40（f9 短暂 0.60“正在合拢抓取”），evidence 一致写“球还在抽屉/架子上，未被提起”。对比通用描述版本这一段在 0.40–0.65 之间摇摆——任务文本让判定更果断。"),
            (12, 13, "中途一次短暂提起",
             "f12“球已提离表面、夹持稳定”→ succeeded 0.95；f13 回到“部分提起”0.70。结合 f14 回落看，这次抓起没有保持住——正是“多次尝试”过程中一次接近成功的抓取。"),
            (14, 14, "又回落", "“not clearly lifted or securely grasped”→ 0.40。"),
            (15, 15, "最终成功",
             "“identified as the Blue No. 24 small ball... securely grasped and lifted off the shelf surface”→ succeeded 0.95。整条曲线=多轮尝试的锯齿+末帧成功，与真实过程一致。"),
        ],
    },
    {
        "key": "fail2",
        "title": "fail2 — Carry / 搬运 咖啡色穿棕色针织衫小熊（注入任务文本）",
        "subtitle": "指令注入：“Carry the Coffee-colored bear wearing a brown knitted sweater.” · 对象正确时曲线几乎不变——阴性对照通过",
        "dir": "artifacts/qwen_fail3_16f_pv2_task",
        "episode": "fail2.zip",
        "frames_dir": "fail_case_frames_16/fail2",
        "segments": [
            (0, 1, "还没开始",
             "现在能明确定位对象：“the coffee-colored bear... is visible on the countertop... not being held”，0.30。"),
            (2, 14, "搬运进行中（平稳段）",
             "“grippers securely holding the teddy bear... transported toward a target location”，0.70–0.85。与通用描述版本几乎重合——说明注入任务文本对“对象正确”的 episode 没有副作用。"),
            (15, 15, "末帧：送达放下被读成“未完成”",
             "“the bear is positioned on a black dish rack... not being held... carry has not been completed”→ 0.30。与通用版相同的语义坑：carry 成功判据只认“仍被夹持”，需要补“或已送达目标位置放下”。"),
        ],
    },
    {
        "key": "fail3",
        "title": "fail3 — Carry / 搬运 调料托盘（注入任务文本）",
        "subtitle": "指令注入：“Carry the Spice tray.” · 搬错对象首次被明确检出：2 帧 failed、5 帧 at_risk、7 帧 should_intervene",
        "dir": "artifacts/qwen_fail3_16f_pv2_task",
        "episode": "fail3.zip",
        "frames_dir": "fail_case_frames_16/fail3",
        "segments": [
            (0, 2, "开局即报“搬错对象”",
             "“holding a white rectangular container labeled ‘果蔬空间’, which is NOT the Spice tray... No Spice tray is visible in any of the frames”→ failed 0.10 / at_risk 0.30 / failed 0.20，三帧全部 should_intervene=True。通用描述版本同一段给的是 0.75 的“一切正常”。"),
            (3, 4, "标签读不到，分数回升",
             "f3“看不到能证明它是调料托盘的文字”0.65，f4 甚至“appears to be the intended Spice tray based on shape”0.70——锯齿的根源：‘果蔬空间’标签并非每帧都可读。"),
            (5, 9, "标签再次可见，再次报警",
             "f5、f8、f9“labeled ‘果蔬空间’... fundamentally misaligned with the task goal”→ 0.30 at_risk + intervene；夹在中间的 f6–f7 标签不可见时回到 0.70。"),
            (10, 14, "后段标签不可见", "0.65–0.75，evidence 退回“白色容器被夹稳移动”。"),
            (15, 15, "末帧再次报警",
             "at_risk 0.30 + intervene。全程 16 帧中 7 帧触发干预、2 帧 failed——聚合规则（例如“任意 2 帧确认 wrong object 即报警”）即可把锯齿转成稳定的失败判定。"),
        ],
    },
    {
        "key": "fail4_close",
        "title": "fail4 按指令评（close）— 关闭格兰仕微波炉",
        "subtitle": "A_019 · 审核拒绝原因：采集内容不符合要求 · 实际执行的动作是“打开”，按“关闭”评末段崩塌",
        "dir": "artifacts/qwen_fail4_16f_pv2",
        "episode": "fail4.zip",
        "frames_dir": "fail_case_frames_16/fail4",
        "segments": [
            (0, 2, "初始状态被当成任务完成",
             "门本来就是关的，模型给 0.70–0.90 并两次判 succeeded。初始状态基线规则（软约束）在 close 任务上没能压住这个先验。"),
            (3, 10, "门实际在被拉开，模型仍报“已关好”",
             "对照真实图片，f4 起门缝已出现、f10 门已明显打开；但在“任务是关门”的先验下，evidence 持续写“door fully closed, latched”（f7 甚至编造“previous frames show the door being pushed shut”）。指令先验偏置了感知，这一段是幻觉。"),
            (11, 12, "开始动摇，但又幻觉了一次",
             "f11 首次承认“door is visibly open, gripper attempting to close”（0.60），f12 又写“door fully closed... confirming the action was performed”（0.95, succeeded）——对照图片门是开的。"),
            (13, 15, "失败信号成立",
             "f13：“door is still open after multiple frames, indicating potential failure to execute the close command”→ at_risk 0.20 + should_intervene=True（全部实验里第一个干预信号）。f14–f15 稳定在 0.20–0.40，末帧结论“关门未完成”。"),
        ],
    },
    {
        "key": "fail4_open",
        "title": "fail4 按实际动作评（open）— 打开微波炉",
        "subtitle": "同一段视频换成 open 任务重跑：教科书式成功曲线，且把“第一次拉门失败”的重试过程也抓出来了",
        "dir": "artifacts/qwen_fail4_as_open_16f",
        "episode": "fail4.zip",
        "frames_dir": "fail_case_frames_16/fail4",
        "segments": [
            (0, 2, "初始状态：门关着，未开始",
             "“door is closed... no interaction is evident”→ 0.10–0.20。初始状态基线规则在 open 任务上完全生效：门关着=还没开始，给低分。"),
            (3, 4, "接触把手，门开出缝",
             "f3“夹爪已接触把手，门还没动”0.35；f4“door is partially ajar, showing a visible gap”0.65。"),
            (5, 6, "第一次拉门失败，门弹回",
             "f5“缝很小，没开起来”0.40；f6“door is closed in the latest frame... the action may be stuck or ineffective”→ at_risk 0.20 + should_intervene=True。这就是 8 帧采样漏掉的“操作过快”的过程。"),
            (7, 9, "第二次尝试，门半开", "“door is partially open... clearly in the process of opening”，稳定 0.65。"),
            (10, 15, "完全打开，成功平台",
             "“door is visibly open, exposing the interior... satisfying the success criteria”→ 0.95，succeeded 连续 6 帧。与按 close 评的末段崩塌对比：末帧分差 0.95 vs 0.20，“做的不是指令要求的动作”被清晰识别。"),
        ],
    },
]


def load_frames(case: dict) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(f"{case['dir']}/*__f*.json")):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "result" not in data:
            continue
        if data["episode"] != case["episode"]:
            continue
        rows.append(data)
    rows.sort(key=lambda item: item["frame"])
    return rows


def marker_svg(x: float, y: float, status: str, intervene: bool) -> str:
    parts = []
    if intervene:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="none" stroke="var(--critical)" stroke-width="2"/>')
    if status == "succeeded":
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5.5" fill="var(--good)" stroke="var(--surface-1)" stroke-width="2"/>')
        parts.append(
            f'<path d="M {x-2.6:.1f} {y:.1f} l 1.8 1.9 l 3.4 -3.7" fill="none" stroke="var(--surface-1)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    elif status == "at_risk":
        parts.append(
            f'<path d="M {x:.1f} {y-6:.1f} L {x+5.5:.1f} {y+4.5:.1f} L {x-5.5:.1f} {y+4.5:.1f} Z" '
            f'fill="var(--serious)" stroke="var(--surface-1)" stroke-width="1.5"/>'
        )
    elif status == "failed":
        parts.append(f'<rect x="{x-4.5:.1f}" y="{y-4.5:.1f}" width="9" height="9" fill="var(--critical)" stroke="var(--surface-1)" stroke-width="1.5"/>')
    elif status == "needs_more_observation":
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="var(--surface-1)" stroke="var(--muted)" stroke-width="2"/>')
    else:  # in_progress
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="1.5"/>')
    return "".join(parts)


def chart_svg(case: dict, rows: list[dict]) -> str:
    width, height = 880, 280
    ml, mr, mt, mb = 44, 16, 26, 34
    plot_w, plot_h = width - ml - mr, height - mt - mb
    n = len(rows)

    def xpos(i: int) -> float:
        return ml + i * plot_w / max(1, n - 1)

    def ypos(v: float) -> float:
        return mt + (1 - max(0.0, min(1.0, v))) * plot_h

    grid = []
    for v in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = ypos(v)
        grid.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{width-mr}" y2="{y:.1f}" stroke="var(--grid)" stroke-width="1"/>')
        grid.append(f'<text x="{ml-8}" y="{y+4:.1f}" text-anchor="end" class="axis">{v:.2f}</text>')

    bands = []
    for idx, (start, end, title, _body) in enumerate(case["segments"], 1):
        x0 = xpos(start) - (plot_w / max(1, n - 1)) * 0.5 if start > 0 else ml
        x1 = xpos(end) + (plot_w / max(1, n - 1)) * 0.5 if end < n - 1 else width - mr
        x0, x1 = max(ml, x0), min(width - mr, x1)
        if idx % 2 == 0:
            bands.append(f'<rect x="{x0:.1f}" y="{mt}" width="{x1-x0:.1f}" height="{plot_h}" fill="var(--band)"/>')
        cx = (x0 + x1) / 2
        bands.append(f'<circle cx="{cx:.1f}" cy="{mt-11}" r="8.5" fill="none" stroke="var(--muted)" stroke-width="1"/>')
        bands.append(f'<text x="{cx:.1f}" y="{mt-7}" text-anchor="middle" class="badge">{idx}</text>')

    pts = [(xpos(i), ypos(float(r["result"]["success_probability"]))) for i, r in enumerate(rows)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    markers, ticks, hits = [], [], []
    for i, r in enumerate(rows):
        res = r["result"]
        x, y = pts[i]
        markers.append(marker_svg(x, y, res["status"], bool(res.get("should_intervene"))))
        ticks.append(f'<text x="{x:.1f}" y="{height-10}" text-anchor="middle" class="axis">f{i}</text>')
        tip = (
            f"f{i} · {STATUS_LABEL.get(res['status'], res['status'])}"
            f"&#10;p_succ={res['success_probability']}  p_fail={res['failure_probability']}"
            f"&#10;progress={res['progress']}  intervene={res.get('should_intervene')}"
        )
        hits.append(
            f'<rect x="{x - plot_w/(2*max(1,n-1)):.1f}" y="{mt}" width="{plot_w/max(1,n-1):.1f}" height="{plot_h}" '
            f'fill="transparent" data-tip="{tip}" data-x="{x:.0f}"/>'
        )

    return f"""
<svg viewBox="0 0 {width} {height}" class="curve" role="img" aria-label="{html.escape(case['title'])} success probability curve">
  <rect x="0" y="0" width="{width}" height="{height}" fill="var(--surface-1)"/>
  {''.join(bands)}
  {''.join(grid)}
  <line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+plot_h}" stroke="var(--baseline)" stroke-width="1"/>
  <line x1="{ml}" y1="{mt+plot_h}" x2="{width-mr}" y2="{mt+plot_h}" stroke="var(--baseline)" stroke-width="1"/>
  <polyline points="{line}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>
  {''.join(markers)}
  {''.join(ticks)}
  <g class="hits">{''.join(hits)}</g>
</svg>"""


def evidence_html(case: dict, rows: list[dict]) -> str:
    seg_of = {}
    for idx, (start, end, _t, _b) in enumerate(case["segments"], 1):
        for f in range(start, end + 1):
            seg_of[f] = idx

    blocks = []
    for idx, (start, end, title, body) in enumerate(case["segments"], 1):
        rng = f"f{start}" if start == end else f"f{start}–f{end}"
        vals = [float(rows[f]["result"]["success_probability"]) for f in range(start, end + 1)]
        vr = f"{min(vals):.2f}" if min(vals) == max(vals) else f"{min(vals):.2f}–{max(vals):.2f}"
        frame_items = []
        for f in range(start, end + 1):
            res = rows[f]["result"]
            ev = "".join(f"<li>{html.escape(e)}</li>" for e in res.get("evidence", []))
            rk = "".join(f'<li class="risk">{html.escape(k)}</li>' for k in res.get("risk_factors", []))
            iv = ' <span class="chip chip-critical">should_intervene</span>' if res.get("should_intervene") else ""
            img = f"{case['frames_dir']}/frames/head_right/{f:02d}.jpg"
            frame_items.append(f"""
      <details class="frame">
        <summary><span class="fno">f{f}</span>
          <span class="chip chip-{res['status']}">{STATUS_LABEL.get(res['status'], res['status'])}</span>
          <span class="nums">p_succ={res['success_probability']} · p_fail={res['failure_probability']} · progress={res['progress']}</span>{iv}
        </summary>
        <div class="framebody">
          <a href="{img}" target="_blank"><img src="{img}" loading="lazy" alt="f{f} head camera"></a>
          <ul>{ev}{rk}</ul>
        </div>
      </details>""")
        blocks.append(f"""
    <div class="segment">
      <h4><span class="badge2">{idx}</span> {html.escape(rng)}（{vr}）：{html.escape(title)}</h4>
      <p>{html.escape(body)}</p>
      {''.join(frame_items)}
    </div>""")
    return "".join(blocks)


def main() -> None:
    sections = []
    for case in CASES:
        rows = load_frames(case)
        if len(rows) == 0:
            raise SystemExit(f"no records for {case['key']}")
        sections.append(f"""
  <section class="case" id="{case['key']}">
    <h2>{html.escape(case['title'])}</h2>
    <p class="sub">{html.escape(case['subtitle'])}</p>
    <div class="chartwrap">{chart_svg(case, rows)}<div class="tooltip" hidden></div></div>
    <div class="legend">
      <span><svg width="18" height="12"><circle cx="9" cy="6" r="4.5" fill="var(--series-1)"/></svg>in_progress</span>
      <span><svg width="18" height="12"><circle cx="9" cy="6" r="4" fill="var(--surface-1)" stroke="var(--muted)" stroke-width="2"/></svg>needs_more_observation</span>
      <span><svg width="18" height="12"><circle cx="9" cy="6" r="5" fill="var(--good)"/></svg>succeeded</span>
      <span><svg width="18" height="12"><path d="M9 1 L14 10 L4 10 Z" fill="var(--serious)"/></svg>at_risk</span>
      <span><svg width="18" height="12"><circle cx="9" cy="6" r="5" fill="none" stroke="var(--critical)" stroke-width="2"/></svg>should_intervene</span>
    </div>
    {evidence_html(case, rows)}
  </section>""")

    OUTPUT.write_text(f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>失败 case 曲线与逐帧 evidence（16 帧 · prompt v2）</title>
<style>
  .viz-root {{
    color-scheme: light;
    --surface-1: #fcfcfb; --page: #f9f9f7;
    --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --band: rgba(11,11,11,0.035);
    --series-1: #2a78d6; --good: #0ca30c; --serious: #ec835a; --critical: #d03b3b;
    --border: rgba(11,11,11,0.10);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) .viz-root {{
      color-scheme: dark;
      --surface-1: #1a1a19; --page: #0d0d0d;
      --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --baseline: #383835; --band: rgba(255,255,255,0.05);
      --series-1: #3987e5; --border: rgba(255,255,255,0.10);
    }}
  }}
  body {{ margin: 0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
  .viz-root {{ background: var(--page); color: var(--ink); padding: 28px 20px 60px; }}
  main {{ max-width: 940px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .note {{ color: var(--ink-2); font-size: 14px; max-width: 860px; line-height: 1.6; }}
  .case {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px 12px; margin-top: 26px; }}
  h2 {{ font-size: 17px; margin: 0 0 2px; }}
  .sub {{ color: var(--ink-2); font-size: 13px; margin: 0 0 12px; }}
  .curve {{ width: 100%; height: auto; display: block; }}
  .chartwrap {{ position: relative; }}
  .tooltip {{ position: absolute; pointer-events: none; background: var(--ink); color: var(--surface-1);
    font-size: 12px; line-height: 1.5; padding: 6px 9px; border-radius: 6px; white-space: pre; transform: translate(-50%, 0); top: 8px; z-index: 2; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--ink-2); margin: 6px 0 14px; }}
  .legend span {{ display: inline-flex; align-items: center; gap: 5px; }}
  .axis {{ font-size: 11px; fill: var(--muted); }}
  .badge {{ font-size: 11px; fill: var(--ink-2); }}
  .segment {{ border-top: 1px solid var(--grid); padding: 12px 2px 10px; }}
  .segment h4 {{ margin: 0 0 6px; font-size: 14.5px; }}
  .segment > p {{ margin: 0 0 10px; color: var(--ink-2); font-size: 13.5px; line-height: 1.65; }}
  .badge2 {{ display: inline-flex; width: 19px; height: 19px; border: 1px solid var(--muted); border-radius: 50%;
    align-items: center; justify-content: center; font-size: 12px; color: var(--ink-2); margin-right: 2px; }}
  details.frame {{ border: 1px solid var(--grid); border-radius: 7px; margin: 6px 0; background: var(--page); }}
  details.frame summary {{ cursor: pointer; padding: 7px 10px; display: flex; align-items: center; gap: 10px; font-size: 13px; flex-wrap: wrap; }}
  .fno {{ font-weight: 600; min-width: 28px; }}
  .nums {{ color: var(--ink-2); font-variant-numeric: tabular-nums; }}
  .chip {{ font-size: 11.5px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--border); }}
  .chip-succeeded {{ background: var(--good); color: #fff; }}
  .chip-at_risk {{ background: var(--serious); color: #fff; }}
  .chip-failed, .chip-critical {{ background: var(--critical); color: #fff; }}
  .chip-in_progress {{ background: var(--series-1); color: #fff; }}
  .chip-needs_more_observation {{ background: var(--page); color: var(--ink-2); }}
  .framebody {{ display: flex; gap: 14px; padding: 4px 12px 12px; align-items: flex-start; flex-wrap: wrap; }}
  .framebody img {{ width: 240px; border-radius: 6px; border: 1px solid var(--border); }}
  .framebody ul {{ margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.6; color: var(--ink); flex: 1; min-width: 260px; }}
  .framebody li.risk {{ color: var(--serious); }}
</style>
</head>
<body>
<div class="viz-root">
<main>
  <h1>失败 case 曲线与逐帧 evidence</h1>
  <p class="note">模型 qwen3-vl-32b-instruct · 策略 start_sliding_window（每次带 f0 + 最近 4 帧）· 3 相机分图 · 16 帧均匀采样 ·
  prompt v2（去掉成功偏置 / 初始状态基线 / p_succ=当前帧完成置信度）。fail1–3 额外注入了 episode 级任务文本
  （动作+具体物体，来自各 case 的 json），并要求模型先在场景中定位指令命名的对象、发现操纵对象不符时按失败证据处理。
  fail4 两条为此前按 close / open 两种指令的运行结果。曲线为 success_probability；
  图上 ①② 等圆圈编号对应下方分段解读，鼠标悬停可看每帧数值，点开每帧可看头部相机画面与模型 evidence 原文。
  成功基线参考（100 个成功 place episode，8 帧）：末帧均值 0.94，p10 = 0.745。</p>
  {''.join(sections)}
</main>
</div>
<script>
  document.querySelectorAll('.chartwrap').forEach(wrap => {{
    const tip = wrap.querySelector('.tooltip');
    wrap.querySelectorAll('.hits rect').forEach(r => {{
      r.addEventListener('mouseenter', () => {{
        tip.textContent = r.dataset.tip.replaceAll('\\u000a', '\\n');
        tip.hidden = false;
        const svg = wrap.querySelector('svg');
        const frac = parseFloat(r.dataset.x) / svg.viewBox.baseVal.width;
        tip.style.left = (frac * svg.clientWidth) + 'px';
      }});
      r.addEventListener('mouseleave', () => tip.hidden = true);
    }});
  }});
</script>
</body>
</html>
""", encoding="utf-8")
    print(f"report: {OUTPUT}")


if __name__ == "__main__":
    main()
