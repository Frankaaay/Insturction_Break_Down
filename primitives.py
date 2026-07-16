# -*- coding: utf-8 -*-
"""解析 `action primitives` 文件,产出用于校验 LLM 输出的注册表。

文件格式(以其为唯一数据源,不在代码里重复定义操作):
    A_001 拿起
    Pick
    logic0: Pick up the <obj_a>.
    ...
"""

import re
from pathlib import Path

PRIMITIVES_FILE = Path(__file__).parent / "action primitives"

_ACTION_RE = re.compile(r"^(A_\d{3})\s+(\S+)\s*$")
_LOGIC_RE = re.compile(r"^logic(\d+):")


def load_primitives_text() -> str:
    """原子操作文件原文,直接嵌入 prompt。"""
    return PRIMITIVES_FILE.read_text(encoding="utf-8")


def load_registry() -> dict:
    """解析文件 → {action_id: {"zh": 中文名, "en": 英文名, "logics": {0, 1, ...}}}"""
    registry: dict[str, dict] = {}
    current = None
    expect_en_name = False
    for line in load_primitives_text().splitlines():
        line = line.strip()
        m = _ACTION_RE.match(line)
        if m:
            current = m.group(1)
            registry[current] = {"zh": m.group(2), "en": None, "logics": set()}
            expect_en_name = True
            continue
        if current is None:
            continue
        if expect_en_name and line:
            registry[current]["en"] = line
            expect_en_name = False
            continue
        m = _LOGIC_RE.match(line)
        if m:
            registry[current]["logics"].add(int(m.group(1)))
    return registry


def validate_steps(steps: list[dict], registry: dict) -> list[str]:
    """校验 LLM 输出的步骤,返回错误信息列表(空列表 = 通过)。"""
    errors = []
    for i, step in enumerate(steps, 1):
        action_id = step.get("action_id")
        logic = step.get("logic")
        if action_id not in registry:
            errors.append(f"第 {i} 步: 未知操作 {action_id!r}")
            continue
        entry = registry[action_id]
        if not isinstance(logic, int) or logic not in entry["logics"]:
            errors.append(
                f"第 {i} 步: {action_id} ({entry['en']}) 不存在 logic{logic},"
                f"合法值: {sorted(entry['logics'])}"
            )
        action = step.get("action")
        if action and entry["en"] and action != entry["en"]:
            errors.append(
                f"第 {i} 步: 操作名 {action!r} 与 {action_id} 的定义 {entry['en']!r} 不符"
            )
        if not step.get("zh"):
            errors.append(f"第 {i} 步: 缺少中文渲染句 zh")
    return errors
