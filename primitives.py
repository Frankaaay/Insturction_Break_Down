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
EXPERT_FILE = Path(__file__).parent / "expert primitives.md"

_ACTION_RE = re.compile(r"^(A_\d{3})\s+(\S+)\s*$")
_LOGIC_RE = re.compile(r"^logic(\d+):")
_LOGIC_FULL_RE = re.compile(r"^logic(\d+):\s*(.+)$")
_COEF_RE = re.compile(r"^coef_(\w+)\s+可选值:")
_EXPERT_HEAD_RE = re.compile(r"^##\s+(E_\d{3})\s+(\S+)\s+(\S+)\s*$")


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


def load_atomic_catalog() -> list[dict]:
    """解析原子操作文件为前端展示用的完整目录。

    返回 [{"id", "zh", "en", "logics": [{"n", "en", "zh"}], "coefs": {name: {"en": [...], "zh": [...]}}}]
    """
    catalog: list[dict] = []
    current: dict | None = None
    pending_logic: dict | None = None   # 刚读到 logicN 英文行,等中文行
    pending_coef: str | None = None     # 刚读到 coef 头,等英文/中文选项行
    expect_en_name = False
    for line in load_primitives_text().splitlines():
        line = line.strip()
        m = _ACTION_RE.match(line)
        if m:
            current = {"id": m.group(1), "zh": m.group(2), "en": None,
                       "logics": [], "coefs": {}}
            catalog.append(current)
            expect_en_name = True
            pending_logic = pending_coef = None
            continue
        if current is None or not line:
            continue
        if expect_en_name:
            current["en"] = line
            expect_en_name = False
            continue
        m = _LOGIC_FULL_RE.match(line)
        if m:
            pending_logic = {"n": int(m.group(1)), "en": m.group(2), "zh": ""}
            current["logics"].append(pending_logic)
            pending_coef = None
            continue
        m = _COEF_RE.match(line)
        if m:
            pending_coef = m.group(1)
            current["coefs"][pending_coef] = {"en": [], "zh": []}
            pending_logic = None
            continue
        if pending_logic is not None:
            pending_logic["zh"] = line
            pending_logic = None
            continue
        if pending_coef is not None:
            slot = current["coefs"][pending_coef]
            key = "en" if not slot["en"] else "zh"
            slot[key] = [v.strip() for v in line.split("/")]
            if key == "zh":
                pending_coef = None
    return catalog


def load_expert_catalog() -> list[dict]:
    """解析 expert primitives.md 为前端展示用目录。

    返回 [{"id", "zh", "en", "logics": [...], "typical_length", "categories": {slot: [...]}}]
    """
    if not EXPERT_FILE.exists():
        return []
    catalog: list[dict] = []
    current: dict | None = None
    pending_logic: dict | None = None
    in_categories = False
    for raw in EXPERT_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        m = _EXPERT_HEAD_RE.match(line)
        if m:
            current = {"id": m.group(1), "zh": m.group(2), "en": m.group(3),
                       "logics": [], "typical_length": None, "categories": {}}
            catalog.append(current)
            pending_logic = None
            in_categories = False
            continue
        if current is None or not line:
            continue
        m = _LOGIC_FULL_RE.match(line)
        if m:
            pending_logic = {"n": int(m.group(1)), "en": m.group(2), "zh": ""}
            current["logics"].append(pending_logic)
            in_categories = False
            continue
        if line.startswith("典型长度:"):
            value = line.split(":", 1)[1].strip()
            current["typical_length"] = int(value) if value.isdigit() else value
            in_categories = False
            continue
        if line.startswith("适用对象:"):
            in_categories = True
            continue
        if pending_logic is not None:
            pending_logic["zh"] = line
            pending_logic = None
            continue
        if in_categories and ":" in line:
            slot, objs = line.split(":", 1)
            current["categories"][slot.strip()] = [v.strip() for v in objs.split("/")]
    return catalog


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
