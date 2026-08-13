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
    version=2,
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
        "因遮挡、画质或物体身份不明确而无法确认是否拿起指定 obj_a；description_zh 必须说明具体不确定原因。",
        "不得因为某个物体正在被手操作，就自动把它称为指定 obj_a。",
    ),
    failed=(
        "明确拿起了错误物体，而指定目标物仍留在原处。",
        "存在其他直接可见、且本次操作已经失败的事实。",
    ),
)


CARRY_CONTRACT = ActionVisualContract(
    action_id="A_003",
    logic=0,
    name="Carry / 搬运",
    version=2,
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
        "无法确认手中物体是不是指定 obj_a，或遮挡导致无法确认握持和位移；description_zh 必须说明具体不确定原因。",
        "移动慢、暂时停止、方向不明确、目标区域不可见或移动距离较短，都属于 in_progress。",
    ),
    failed=(
        "仅在明确搬运了错误物体、而指定 obj_a 没有被搬运时判失败。",
    ),
)


PLACE_ON_SURFACE_CONTRACT = ActionVisualContract(
    action_id="A_002",
    logic=1,
    name="Place on surface / 放到表面",
    version=2,
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
        "因遮挡、画质、物体身份或表面身份不明确而无法确认放置结果；description_zh 必须说明具体不确定原因。",
    ),
    failed=(
        "明确把指定物体放到了错误表面。",
        "明确放置了错误物体。",
        "目标物掉落到指定表面以外，且当前视频明确表明本次操作已经失败。",
    ),
)


ACTION_CONTRACTS = {
    contract.key: contract
    for contract in (PICK_CONTRACT, CARRY_CONTRACT, PLACE_ON_SURFACE_CONTRACT)
}


HUMAN_OPERATOR_CONTEXT = """当前测试执行者限定：
- 当前阶段由人类用手执行操作；人手是合法且唯一需要评估的执行者。
- 画面中的机械臂、夹爪暂时视为无关背景，不要求机械臂参与动作，也不判断机械臂是否执行。
- 不得因为机械臂静止、未接近目标物、未抓握目标物或未参与动作而返回 failed。
- 人手使指定目标物满足动作成功后置条件时，应按 succeeded 判定，不能写成“由人手操作所以失败”。
- 状态只依据指定目标物、人的手、支撑面或目标位置之间直接可见的关系变化判断。"""


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

{HUMAN_OPERATOR_CONTEXT}

公共状态规则：
- in_progress：操作尚未完成，或者因遮挡、画质、物体身份等原因暂时无法确认；description_zh 必须写明具体原因。可恢复的抓空、滑脱或掉落后继续尝试也属于 in_progress。
- succeeded：本动作契约列出的成功后置条件在窗口结尾 NOW 画面中仍然明确成立。窗口中途曾经满足、但结尾已不满足，不能判为 succeeded。
- failed：根据当前直接可见事实，本次原子操作已经失败。只有 failed 才填写自然语言 failure_reason，原因不使用预定义枚举。
- 在判断动作状态前先核对实际被操作物体是否为指定 obj_a。不得因为某个物体正在被手操作、位于画面中央或最显眼，就自动把它称为 obj_a。
- 能确认实际操作物不是 obj_a 时返回 failed；无法确认物体身份时返回 in_progress，并说明不确定原因。

本动作 succeeded 判据：
{_bullets(contract.succeeded)}

本动作 in_progress 判据：
{_bullets(contract.in_progress)}

本动作 failed 边界：
{_bullets(contract.failed)}

输出规则：
- evidence 只写直接可见事实，不写隐藏推理、操作意图或控制建议。
- evidence 时间戳和 completion_evidence_timestamp_s 都相对这段 6 秒视频开头，范围为 0 到 6 秒。
- succeeded 的 completion_evidence_timestamp_s 必须在最后 1 秒（5 到 6 秒），并对应 NOW 中仍成立的成功后置条件。
- 只有 succeeded 才填写 completion_evidence_timestamp_s；其他状态必须为 null。
- 非 failed 状态的 failure_reason 必须为 null。
- 不决定继续、推进、停止或恢复；这些属于独立控制层。
- 只返回符合 JSON Schema 的 JSON。"""


def build_chain_monitor_prompt(assignment: dict[str, Any], sequence: int) -> str:
    """Build one prompt that evaluates the planner's complete, stable step chain."""
    steps = assignment.get("steps") or []
    if not steps:
        raise ValueError("整链 Visual Monitor 缺少步骤")
    rendered_steps: list[str] = []
    for index, step in enumerate(steps, start=1):
        action_id = step.get("action_id")
        logic = step.get("logic")
        contract = get_visual_contract(action_id, logic)
        if not contract:
            raise ValueError(f"没有 Visual Monitor 动作契约: {action_id}/logic{logic}")
        slots = step.get("slots") or {}
        missing = [name for name in contract.required_slots if not str(slots.get(name, "")).strip()]
        if missing:
            raise ValueError(f"动作契约缺少 slots: {', '.join(missing)}")
        slot_text = "，".join(f"{name}={slots[name]}" for name in contract.required_slots)
        rendered_steps.append(f"""步骤 {index} / step_id={step['step_id']}
动作：{contract.name}（{contract.contract_key}，contract v{contract.version}）
描述：{step.get('zh') or step.get('action') or contract.name}
参数：{slot_text}
succeeded 判据：
{_bullets(contract.succeeded)}
in_progress 判据：
{_bullets(contract.in_progress)}
failed 边界：
{_bullets(contract.failed)}""")

    ledger = assignment.get("confirmed_steps") or []
    current_index = int(assignment.get("current_step_index", 0))
    pending_ids = [step["step_id"] for step in steps[current_index:]]
    ledger_text = "、".join(
        f"{item['step_id']}=succeeded" for item in ledger
    ) or "无"
    unfinished = assignment.get("unfinished_steps") or [
        {"step_id": step["step_id"], "status": "in_progress", "description_zh": None}
        for step in steps[current_index:]
    ]
    unfinished_text = "\n".join(
        f"- {item['step_id']}: 上一窗口状态={item.get('status') or 'in_progress'}；"
        f"观察={item.get('description_zh') or '尚无上一窗口观察'}"
        for item in unfinished
    )
    return f"""你是实时视觉观察器。你要在同一个 6 秒视频窗口内评估完整操作链，而不是只判断当前一个原子动作。

原始指令：{assignment.get('instruction') or '未提供'}
检查点序号：{sequence}

后端已经确认且不可回退的步骤：
{ledger_text}

当前及后续未完成步骤（上一窗口摘要只作为时序参考）：
{unfinished_text}

{HUMAN_OPERATOR_CONTEXT}

{chr(10).join(rendered_steps)}

时序与状态规则：
- 只评估尚未由后端确认的步骤。step_updates 必须从当前步骤开始，按顺序返回未确认后缀 {pending_ids} 的连续前缀。
- 一旦遇到第一个 in_progress 或 failed 就停止输出，不要再为更后面的尚未执行步骤生成占位结果。只有前面的步骤都 succeeded 才能继续输出下一步。
- 不得重复输出已确认步骤，也不得跳步、自行重新拆解、改名或重排步骤。
- 上一窗口摘要不是本窗口的视觉证据，不能直接复制为 evidence；必须结合本次 WINDOW 和 NOW 更新判断。
- succeeded：视频中有直接证据表明该步骤完成。中间步骤只需在窗口内真实发生过，不要求其后置条件保持到 NOW；例如 Pick 后继续 Carry/Place，Pick 仍可 succeeded。
- 最后一个步骤以及代表整个任务完成的状态必须在窗口结尾 NOW 仍明确成立；中途成立但 NOW 已撤销，不能判最终成功。
- in_progress：该步骤正在执行、尚未完成，或者因遮挡、画质、物体身份等原因暂时无法确认；description_zh 必须写明具体原因。可恢复的抓空、滑脱、掉落后继续尝试属于 in_progress。
- 如果画面和目标物清晰可见，但当前窗口尚未开始相关动作或目标物仍保持初始状态，返回 in_progress。
- failed：直接可见该步骤已失败；只有 failed 填自然语言 failure_reason，不使用预定义枚举。
- 在判断动作状态前先核对实际被操作物体是否为指定 obj_a。不得因为某个物体正在被手操作、位于画面中央或最显眼，就自动把它称为 obj_a。
- 能确认实际操作物不是 obj_a 时返回 failed；无法确认物体身份时返回 in_progress，并说明不确定原因。
- 后续步骤不能绕过尚未 succeeded 的前序步骤；若当前步骤 failed，立即在该步骤停止 step_updates。
- 不能仅凭手靠近、动作停止、夹爪闭合或短暂接触推断成功。

输出规则：
- step_updates 至少包含当前 step_id，并且只能是未确认后缀的连续前缀。
- evidence 只写直接可见事实，不写隐藏推理、意图、控制建议或 decision。
- 所有时间戳相对本次 6 秒 WINDOW 开头，范围 0 到 6 秒。
- 中间步骤 succeeded 的 completion_evidence_timestamp_s 可位于窗口任意时刻。
- 最后步骤 succeeded 的 completion_evidence_timestamp_s 必须在最后 1 秒（5 到 6 秒），并对应 NOW 中仍成立的最终状态。
- 只有 succeeded 填 completion_evidence_timestamp_s；其他状态必须为 null。
- 只有 failed 填 failure_reason；其他状态必须为 null。
- 顶层 status 概括整条任务：全部步骤 succeeded 才是 succeeded；出现 failed 是 failed；其他情况都是 in_progress。
- 只有顶层 succeeded 才填写 task_completion_evidence_timestamp_s，且必须在最后 1 秒；其他状态必须为 null。
- 只返回符合 JSON Schema 的 JSON。"""
