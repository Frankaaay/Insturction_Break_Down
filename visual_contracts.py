# -*- coding: utf-8 -*-
"""Action-specific visual contracts rendered inside one common monitor prompt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionVisualContract:
    action_id: str
    logic: int
    name: str
    version: int
    required_slots: tuple[str, ...]
    succeeded: tuple[str, ...]
    in_progress: tuple[str, ...]
    failed: tuple[str, ...]
    unknown: tuple[str, ...]
    cautions: tuple[str, ...]

    @property
    def key(self) -> tuple[str, int]:
        return self.action_id, self.logic

    @property
    def contract_key(self) -> str:
        return f"{self.action_id}:logic{self.logic}"


PICK_CONTRACT = ActionVisualContract(
    action_id="A_001",
    logic=0,
    name="Pick / 拿起",
    version=1,
    required_slots=("obj_a",),
    succeeded=(
        "能确认拿起的是指定 obj_a。",
        "在窗口结尾的 NOW 画面中，目标物仍明确离开原支撑面，并稳定地被手握持。",
        "必须有目标物底部、原支撑面或二者间隙等直接可见证据。",
    ),
    in_progress=(
        "手正在接近、抓握或开始抬起，但尚未明确离开支撑面。",
        "抓空、滑落或掉落后仍在重新尝试。",
        "窗口中曾拿起目标物，但在 NOW 画面中已经放回原支撑面。",
    ),
    failed=(
        "明确拿起了错误物体，而指定目标物仍留在原处。",
        "存在其他直接可见、且本次操作已经失败的事实。",
    ),
    unknown=(
        "目标物身份、底部、支撑面或握持关系被遮挡，无法可靠判断。",
    ),
    cautions=(
        "手靠近、手合拢或动作停止都不等于成功。",
        "看不见目标物离开支撑面的证据时不能猜测成功。",
    ),
)


CARRY_CONTRACT = ActionVisualContract(
    action_id="A_003",
    logic=0,
    name="Carry / 搬运",
    version=1,
    required_slots=("obj_a",),
    succeeded=(
        "能确认搬运的是指定 obj_a。",
        "目标物仍然稳定地被手握持。",
        "目标物与手相对本步骤 BEFORE 位置发生了明确可见的移动。",
        "一旦上述条件直接可见即可成功，不要求判断最终目标位置或精确移动距离。",
    ),
    in_progress=(
        "目标物仍被拿着，但尚未看到足够明确的位移。",
        "正在调整握姿，或暂时滑落后仍在重新抓取、继续移动。",
    ),
    failed=(
        "仅在明确搬运了错误物体、而指定 obj_a 没有被搬运时判失败。",
    ),
    unknown=(
        "无法确认手中物体是不是指定 obj_a。",
        "遮挡导致无法确认是否仍被握持，或相机运动导致无法判断物体位移。",
    ),
    cautions=(
        "这是宽松成功、极窄失败的中间步骤。",
        "移动慢、暂时停止、方向不明确、目标区域不可见或移动距离较短都不得判 failed。",
        "以上情况应返回 in_progress；只有关键视觉证据不可见时返回 unknown。",
        "不能把相机自身运动误当成目标物移动。",
    ),
)


PLACE_ON_SURFACE_CONTRACT = ActionVisualContract(
    action_id="A_002",
    logic=1,
    name="Place on surface / 放到表面",
    version=1,
    required_slots=("obj_a", "sur_a"),
    succeeded=(
        "能确认物体是指定 obj_a，目标表面是指定 sur_a。",
        "目标物已经接触指定表面，并由该表面承托。",
        "手已经释放，或手已不再是目标物的主要支撑。",
        "目标物稳定留在指定表面，没有继续掉落或倾倒。",
    ),
    in_progress=(
        "目标物仍被手持，正在下降、对准或调整放置位置。",
        "目标物接触表面但手尚未释放，或放置不稳但仍在调整。",
        "掉落后仍在重新拿起并继续放置。",
    ),
    failed=(
        "明确把指定物体放到了错误表面。",
        "明确放置了错误物体。",
        "目标物掉落到指定表面以外，且当前视频明确表明本次操作已经失败。",
    ),
    unknown=(
        "物体与表面的接触区域被遮挡。",
        "无法确认物体是否由指定表面承托、手是否释放，或目标表面身份不明确。",
    ),
    cautions=(
        "仅凭手松开、动作停止或物体接近表面不能判成功。",
        "成功必须同时满足指定表面承托、解除手部主要支撑和稳定留置。",
    ),
)


ACTION_CONTRACTS = {
    contract.key: contract
    for contract in (PICK_CONTRACT, CARRY_CONTRACT, PLACE_ON_SURFACE_CONTRACT)
}


def get_visual_contract(action_id: str, logic: int) -> ActionVisualContract | None:
    return ACTION_CONTRACTS.get((action_id, logic))


def supports_visual_contract(action_id: str | None, logic: Any) -> bool:
    return isinstance(action_id, str) and isinstance(logic, int) and (action_id, logic) in ACTION_CONTRACTS


def visual_contract_metadata(action_id: str, logic: int) -> dict[str, Any]:
    contract = get_visual_contract(action_id, logic)
    if not contract:
        raise ValueError(f"没有 Visual Monitor 动作契约: {action_id}/logic{logic}")
    return {
        "contract_key": contract.contract_key,
        "contract_name": contract.name,
        "contract_version": contract.version,
    }


def _bullets(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items)


def build_monitor_prompt(assignment: dict[str, Any], sequence: int) -> str:
    action_id = assignment.get("action_id")
    logic = assignment.get("logic")
    contract = get_visual_contract(action_id, logic)
    if not contract:
        raise ValueError(f"没有 Visual Monitor 动作契约: {action_id}/logic{logic}")
    slots = assignment.get("slots") or {}
    missing = [name for name in contract.required_slots if not str(slots.get(name, "")).strip()]
    if missing:
        raise ValueError(f"动作契约缺少 slots: {', '.join(missing)}")
    slot_text = "，".join(f"{name}={slots[name]}" for name in contract.required_slots)
    return f"""你是实时视觉观察器。只基于给出的本步骤 BEFORE 初始图、随后 6 秒视频和窗口结尾 NOW 图，判断当前原子操作在检查点结束时的视觉状态。

动作：{contract.name}（{contract.contract_key}，contract v{contract.version}）
操作描述：{assignment.get('zh') or assignment.get('action') or contract.name}
动作参数：{slot_text}
检查点序号：{sequence}
上一次状态：{assignment.get('previous_status') or '无'}（只作为时序参考，本次仍以直接可见证据为准）

公共状态规则：
- in_progress：操作尚未完成，但仍在执行、调整或仍有机会继续完成。可恢复的抓空、滑脱或掉落后继续尝试也属于 in_progress。
- succeeded：本动作契约列出的成功后置条件在窗口结尾 NOW 画面中仍然明确成立。窗口中途曾经满足、但结尾已不满足，不能判为 succeeded。
- failed：根据当前直接可见事实，本次原子操作已经失败。只有 failed 才填写自然语言 failure_reason，原因不使用预定义枚举。
- unknown：关键区域遮挡、画质不足或证据不足，无法可靠判断。不能猜测。

本动作 succeeded 判据：
{_bullets(contract.succeeded)}

本动作 in_progress 判据：
{_bullets(contract.in_progress)}

本动作 failed 边界：
{_bullets(contract.failed)}

本动作 unknown 边界：
{_bullets(contract.unknown)}

本动作防误判规则：
{_bullets(contract.cautions)}

输出规则：
- evidence 只写直接可见事实，不写隐藏推理、操作意图或控制建议。
- evidence 时间戳和 completion_evidence_timestamp_s 都相对这段 6 秒视频开头，范围为 0 到 6 秒。
- succeeded 的 completion_evidence_timestamp_s 必须在最后 1 秒（5 到 6 秒），并对应 NOW 中仍成立的成功后置条件。
- 只有 succeeded 才填写 completion_evidence_timestamp_s；其他状态必须为 null。
- 非 failed 状态的 failure_reason 必须为 null。
- 不决定继续、推进、停止或恢复；这些属于独立控制层。
- 只返回符合 JSON Schema 的 JSON。"""
