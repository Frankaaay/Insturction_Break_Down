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
    temporal_mode: str
    terminal_evidence_policy: str
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
    version=3,
    required_slots=("obj_a",),
    temporal_mode="transition_or_state",
    terminal_evidence_policy="current_now",
    succeeded=(
        "能确认拿起的是指定 obj_a。",
        "WINDOW 中能直接看到目标物明确离开原支撑面，并在拿起事件发生时稳定地被手握持。",
        "若本步骤作为独立原子操作评估，CURRENT_NOW 中该拿起状态仍须成立；若作为整链中间步骤，后续 Carry/Place 不撤销已经直接可见的 Pick 事件。",
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
    version=3,
    required_slots=("obj_a",),
    temporal_mode="transition_event",
    terminal_evidence_policy="current_now",
    succeeded=(
        "能确认搬运的是指定 obj_a。",
        "在搬运事件发生时，目标物稳定地被手握持。",
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
    version=3,
    required_slots=("obj_a", "sur_a"),
    temporal_mode="persistent_state",
    terminal_evidence_policy="current_now",
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


TURN_CONTRACT = ActionVisualContract(
    action_id="A_014",
    logic=0,
    name="Turn over / 翻面",
    version=1,
    required_slots=("obj_a",),
    temporal_mode="transition_or_state",
    terminal_evidence_policy="current_now",
    succeeded=(
        "能确认翻面的是指定 obj_a。",
        "WINDOW 中直接看到目标物越过边缘或绕水平轴翻转，使原本朝下的一面变为朝上；不能把桌面内平移或水平旋转当成翻面。",
        "若本步骤是当前或最终步骤，CURRENT_NOW 中目标物仍稳定保持翻面后的朝向。",
        "如果正反面相似，必须有文字、颜色、图案、边缘或其他直接可见特征证明朝向确实改变。",
    ),
    in_progress=(
        "目标物正在被拿起、倾斜或翻到一半，但尚未形成明确的反面朝上状态。",
        "只发生了平移或桌面内水平旋转，没有直接证据证明完成翻面。",
        "正反面外观太相似、被手遮挡或画质不足，无法确认朝向是否改变；description_zh 必须说明具体不确定原因。",
        "窗口中曾翻面，但 CURRENT_NOW 又恢复为本步骤开始时的朝向。",
    ),
    failed=(
        "明确翻动了错误物体，而指定 obj_a 没有被翻面。",
        "存在其他直接可见、且本次翻面操作已经失败的事实。",
    ),
)


WIPE_CONTRACT = ActionVisualContract(
    action_id="A_009",
    logic=0,
    name="Wipe / 擦拭",
    version=1,
    required_slots=("obj_a", "sur_a"),
    temporal_mode="transition_event",
    terminal_evidence_policy="event_in_window",
    succeeded=(
        "能确认使用的是指定擦拭物 obj_a，接触的是指定表面 sur_a。",
        "WINDOW 中直接看到 obj_a 与 sur_a 持续接触，并沿表面产生明确、连续的擦拭位移。",
        "成功只要求明确完成擦拭动作，不要求判断表面已经完全擦干净，也不要求擦拭状态保持到 CURRENT_NOW。",
        "不能仅凭接近表面、悬空移动、一次短暂触碰或动作停止推断已经擦拭。",
    ),
    in_progress=(
        "擦拭物正在接近表面、刚开始接触，或动作尚未形成明确连续的擦拭轨迹。",
        "擦拭物可能悬空移动，无法确认是否持续接触指定表面。",
        "手、身体或其他物体遮挡接触区域，无法确认擦拭动作；description_zh 必须说明具体不确定原因。",
        "擦拭动作中断但仍可能继续完成。",
    ),
    failed=(
        "明确擦拭了错误表面，而指定 sur_a 没有被擦拭。",
        "明确使用了错误擦拭物，而指定 obj_a 没有参与。",
        "存在其他直接可见、且本次擦拭操作已经失败的事实。",
    ),
)


OPEN_CONTRACT = ActionVisualContract(
    action_id="A_017",
    logic=0,
    name="Open / 打开",
    version=1,
    required_slots=("obj_a",),
    temporal_mode="transition_or_state",
    terminal_evidence_policy="current_now",
    succeeded=(
        "能确认打开的是指定 obj_a。",
        "WINDOW 中能看到目标由关闭状态变化到功能性打开状态，开口、内部空间或可访问区域明确暴露。",
        "若本步骤是当前或最终步骤，CURRENT_NOW 中目标仍明确保持打开；只接触、轻微移动或很小缝隙不足以证明成功。",
        "若 Open 是整链中间步骤，窗口内明确看到打开事件即可，后续合法的 Insert/Close 不撤销该历史步骤。",
    ),
    in_progress=(
        "目标正在被拉、抬、旋转或分离，但尚未形成明确可用的打开状态。",
        "打开程度、开口或内部空间被手遮挡，无法确认是否打开；description_zh 必须说明具体不确定原因。",
        "目标仍关闭，或窗口中曾打开但作为当前/最终步骤时 CURRENT_NOW 已重新关闭。",
    ),
    failed=(
        "明确打开了错误物体，而指定 obj_a 没有被打开。",
        "存在其他直接可见、且本次打开操作已经失败的事实。",
    ),
)


CLOSE_CONTRACT = ActionVisualContract(
    action_id="A_016",
    logic=0,
    name="Close / 关闭",
    version=1,
    required_slots=("obj_a",),
    temporal_mode="transition_or_state",
    terminal_evidence_policy="current_now",
    succeeded=(
        "能确认关闭的是指定 obj_a。",
        "WINDOW 中能看到目标由打开状态变化到功能性关闭状态，原本暴露的开口或内部空间被对应部件覆盖。",
        "若本步骤是当前或最终步骤，CURRENT_NOW 中目标仍明确保持关闭，且不存在足以表明仍打开的明显缝隙。",
        "不要求推断不可见的机械锁扣是否锁死，除非锁扣状态在画面中直接可见。",
    ),
    in_progress=(
        "目标正在向关闭方向移动，但仍存在明显开口或尚未到达关闭位置。",
        "手仍在调整，或关键边缘被遮挡，无法确认是否关闭；description_zh 必须说明具体不确定原因。",
        "窗口中曾关闭，但作为当前/最终步骤时 CURRENT_NOW 已重新打开。",
    ),
    failed=(
        "明确关闭了错误物体，而指定 obj_a 没有被关闭。",
        "存在其他直接可见、且本次关闭操作已经失败的事实。",
    ),
)


INSERT_CONTRACT = ActionVisualContract(
    action_id="A_010",
    logic=0,
    name="Insert / 插入",
    version=1,
    required_slots=("obj_a", "obj_b"),
    temporal_mode="persistent_state",
    terminal_evidence_policy="current_now",
    succeeded=(
        "能确认插入物是指定 obj_a，接收目标是指定 obj_b。",
        "obj_a 明确进入 obj_b 的开口、插槽或受约束区域，并形成直接可见的插入关系；仅靠近、搭在边缘或放入宽大开放区域不足以证明 Insert。",
        "插入深度足以稳定保留，手已经释放或不再是 obj_a 的主要支撑。",
        "若本步骤是当前或最终步骤，CURRENT_NOW 中插入关系仍然成立。",
    ),
    in_progress=(
        "obj_a 正在对准开口，或只有尖端/一小部分进入且仍在调整。",
        "手仍持续握持，无法确认插入关系是否稳定。",
        "开口、插槽或插入深度被遮挡，无法确认是否真正进入；description_zh 必须说明具体不确定原因。",
        "插入后滑出或掉落，但仍在重新尝试。",
    ),
    failed=(
        "明确插入了错误物体。",
        "明确把指定 obj_a 插进了错误的容器、开口或插槽，而指定 obj_b 没有接收该物体。",
        "obj_a 明确落在目标外，且当前视频直接表明本次插入操作已经失败。",
    ),
)


ACTION_CONTRACTS = {
    contract.key: contract
    for contract in (
        PICK_CONTRACT,
        CARRY_CONTRACT,
        PLACE_ON_SURFACE_CONTRACT,
        TURN_CONTRACT,
        WIPE_CONTRACT,
        OPEN_CONTRACT,
        CLOSE_CONTRACT,
        INSERT_CONTRACT,
    )
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
        "terminal_evidence_policy": contract.terminal_evidence_policy,
    }


def _bullets(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _temporal_rule(contract: ActionVisualContract) -> str:
    rules = {
        "transition_or_state": (
            "transition_or_state：既可由 WINDOW 中直接可见的状态变化证明，也可由 CURRENT_NOW 中仍清晰成立的持久结果证明；"
            "如果变化发生在前一窗口但当前结果仍明确成立，以 CURRENT_NOW 的直接可见状态判断。"
        ),
        "transition_event": (
            "transition_event：必须在本次 WINDOW 中直接看到动作事件及其必要接触/位移；"
            "不能仅凭 CURRENT_NOW 的静态画面推断此前发生过该动作。"
        ),
        "persistent_state": (
            "persistent_state：CURRENT_NOW 中明确成立的稳定最终关系足以证明成功，即使释放动作发生在更早窗口；"
            "WINDOW、CHAIN_BEFORE 和已确认前置 Step 的成功关键帧用于核对目标身份并排除短暂接触、掉落或错误目标。"
        ),
    }
    return rules[contract.temporal_mode]


def _duration_text(window_duration_s: float) -> str:
    return f"{window_duration_s:.3f}".rstrip("0").rstrip(".")


def _atomic_terminal_rules(
    contract: ActionVisualContract, duration: str, tail_start: str,
) -> tuple[str, str]:
    if contract.terminal_evidence_policy == "event_in_window":
        return (
            "succeeded：本动作契约列出的短时动作事件在本次 WINDOW 中有明确直接证据。"
            "动作完成后不要求接触或运动一直保持到 CURRENT_NOW；不能只凭静态 NOW 倒推动作发生。",
            f"succeeded 的 completion_evidence_timestamp_s 可以位于本次 WINDOW 任意时刻（0 到 {duration} 秒），"
            "必须指向动作事件最清楚的证据帧。",
        )
    return (
        "succeeded：本动作契约列出的成功后置条件在窗口结尾 CURRENT_NOW 中仍然明确成立。"
        "窗口中途曾经满足、但结尾已不满足或已撤销，不能判为 succeeded。",
        f"succeeded 的 completion_evidence_timestamp_s 必须在最后 1 秒（{tail_start} 到 {duration} 秒），"
        "并对应 CURRENT_NOW 中仍成立的成功后置条件。",
    )


def _chain_terminal_rules(
    final_contract: ActionVisualContract, duration: str, tail_start: str,
) -> tuple[str, str, str]:
    if final_contract.terminal_evidence_policy == "event_in_window":
        return (
            "最后一个步骤属于短时事件动作：只要本次 WINDOW 中直接看到该事件明确完成，"
            "任务即可成功；事件不要求持续到 CURRENT_NOW，也不能仅凭静态 NOW 倒推成功。",
            f"最后步骤 succeeded 的 completion_evidence_timestamp_s 可以位于本次 WINDOW 任意时刻（0 到 {duration} 秒），"
            "并指向事件最清楚的证据帧。",
            "只有顶层 succeeded 才填写 task_completion_evidence_timestamp_s；其值必须位于本次 WINDOW 内，"
            "并对应最后步骤完成事件。其他状态必须为 null。",
        )
    return (
        "最后一个步骤以及代表整个任务完成的状态必须在窗口结尾 CURRENT_NOW 中仍明确成立；"
        "中途成立但 NOW 已撤销，不能判最终成功。",
        f"最后步骤 succeeded 的 completion_evidence_timestamp_s 必须在最后 1 秒（{tail_start} 到 {duration} 秒），"
        "并对应 CURRENT_NOW 中仍成立的最终状态。",
        "只有顶层 succeeded 才填写 task_completion_evidence_timestamp_s，且必须在最后 1 秒；"
        "其他状态必须为 null。",
    )


def build_monitor_prompt(
    assignment: dict[str, Any], sequence: int, window_duration_s: float = 7.0,
) -> str:
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
    duration = _duration_text(window_duration_s)
    tail_start = _duration_text(max(0.0, window_duration_s - 1.0))
    succeeded_rule, completion_rule = _atomic_terminal_rules(contract, duration, tail_start)
    return f"""你是实时视觉观察器。只基于给出的本步骤 BEFORE 初始图、随后 {duration} 秒连续视频和窗口结尾 CURRENT_NOW 图，判断当前原子操作在检查点结束时的视觉状态。

动作：{contract.name}（{contract.contract_key}，contract v{contract.version}）
操作描述：{assignment.get('zh') or assignment.get('action') or contract.name}
动作参数：{slot_text}
检查点序号：{sequence}
上一次状态：{assignment.get('previous_status') or '无'}（只作为时序参考，本次仍以直接可见证据为准）

时间语义：{_temporal_rule(contract)}

{HUMAN_OPERATOR_CONTEXT}

公共状态规则：
- in_progress：操作尚未完成，或者因遮挡、画质、物体身份等原因暂时无法确认；description_zh 必须写明具体原因。可恢复的抓空、滑脱或掉落后继续尝试也属于 in_progress。
- {succeeded_rule}
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
- evidence 时间戳和 completion_evidence_timestamp_s 都相对这段 {duration} 秒视频开头，范围为 0 到 {duration} 秒。
- {completion_rule}
- 只有 succeeded 才填写 completion_evidence_timestamp_s；其他状态必须为 null。
- 非 failed 状态的 failure_reason 必须为 null。
- 不决定继续、推进、停止或恢复；这些属于独立控制层。
- 只返回符合 JSON Schema 的 JSON。"""


def build_chain_monitor_prompt(
    assignment: dict[str, Any], sequence: int, window_duration_s: float = 7.0,
) -> str:
    """Build one prompt that evaluates the planner's complete, stable step chain."""
    steps = assignment.get("steps") or []
    if not steps:
        raise ValueError("整链 Visual Monitor 缺少步骤")
    confirmed_ids = {
        item["step_id"] for item in assignment.get("confirmed_steps", [])
        if item.get("status") == "succeeded"
    }
    current_index = int(assignment.get("current_step_index", 0))
    current_step_id = assignment.get("current_step_id") or steps[current_index]["step_id"]
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
        backend_status = (
            "succeeded（后端权威、不可回退）" if step["step_id"] in confirmed_ids
            else "current（本轮首先判断）" if step["step_id"] == current_step_id
            else "pending（仅当前序步骤 succeeded 后才可判断）"
        )
        rendered_steps.append(f"""步骤 {index} / step_id={step['step_id']}
动作：{contract.name}（{contract.contract_key}，contract v{contract.version}）
描述：{step.get('zh') or step.get('action') or contract.name}
参数：{slot_text}
后端状态：{backend_status}
时间语义：{_temporal_rule(contract)}
succeeded 判据：
{_bullets(contract.succeeded)}
in_progress 判据：
{_bullets(contract.in_progress)}
failed 边界：
{_bullets(contract.failed)}""")

    ledger = assignment.get("confirmed_steps") or []
    pending_ids = [step["step_id"] for step in steps[current_index:]]
    ledger_text = "、".join(
        f"{item['step_id']}=succeeded（已附带唯一冻结成功关键帧）" for item in ledger
    ) or "无"
    unfinished = assignment.get("unfinished_steps") or [
        {"step_id": step["step_id"]} for step in steps[current_index:]
    ]
    unfinished_text = "\n".join(
        f"- {item['step_id']}: 尚未被后端确认"
        for item in unfinished
    )
    duration = _duration_text(window_duration_s)
    tail_start = _duration_text(max(0.0, window_duration_s - 1.0))
    final_step = steps[-1]
    final_contract = get_visual_contract(final_step.get("action_id"), final_step.get("logic"))
    if not final_contract:
        raise ValueError(
            f"没有 Visual Monitor 动作契约: {final_step.get('action_id')}/logic{final_step.get('logic')}"
        )
    final_state_rule, final_completion_rule, task_completion_rule = _chain_terminal_rules(
        final_contract, duration, tail_start,
    )
    return f"""你是实时视觉观察器。你要在同一个 {duration} 秒连续视频窗口内评估完整操作链，而不是只判断当前一个原子动作。

原始指令：{assignment.get('instruction') or '未提供'}
检查点序号：{sequence}
当前执行 Step：step_id={current_step_id}，步骤序号={current_index + 1}

后端已经确认且不可回退的步骤：
{ledger_text}

当前及后续未完成步骤：
{unfinished_text}

视觉输入严格按 CHAIN_BEFORE、每个已确认前置 Step 的一张 HISTORY_STEP_KEYFRAME、唯一的 CURRENT_WINDOW 视频、CURRENT_NOW 排列。不存在任何历史视频。每张 HISTORY_STEP_KEYFRAME 只证明与其绑定的 Step 已由后端确认成功，不要求用当前视频重新看到该动作。

{HUMAN_OPERATOR_CONTEXT}

{chr(10).join(rendered_steps)}

时序与状态规则：
- 只评估尚未由后端确认的步骤。step_updates 必须从当前步骤开始，按顺序返回未确认后缀 {pending_ids} 的连续前缀。
- 一旦遇到第一个 in_progress 或 failed 就停止输出，不要再为更后面的尚未执行步骤生成占位结果。只有前面的步骤都 succeeded 才能继续输出下一步。
- 每个尚未确认的 Step 都必须由本次 CURRENT_WINDOW 或 CURRENT_NOW 中属于该 Step 自身的直接视觉证据支持。看到后续动作或后续状态，不等于在本窗口看到了前序动作；禁止根据动作链的逻辑先后关系反推前序 Step 已完成。
- 特别地，看到正在 Wipe、Place 或执行其他后续动作，不能据此编造本窗口中曾出现 Pick 或 Carry。若当前 Step 的明确动作证据不在本窗口，即使后续动作看起来已经开始，也必须只返回当前 Step=in_progress，并在 description_zh 中说明“当前窗口缺少该步骤的直接完成证据”；不得输出后续 Step。
- 不得把发生在本窗口开始之前的动作虚构到 0.0 秒附近，也不得为未直接看到的动作补写时间戳、运动过程或完成画面。
- 不得重复输出已确认步骤，也不得跳步、自行重新拆解、改名或重排步骤。
- 后端标记 succeeded 的历史 Step 是权威事实，并有对应冻结关键帧；不得重新判断、否认或回退，也不得在 description_zh、failure_reason 或 evidence 中声称这些前置 Step 尚未完成。
- 只从当前执行 Step 开始判断。当前为 Place 时，如果 Pick/Carry 已由后端确认，禁止回答“尚未拿起”“尚未搬运”或语义等价内容；只判断目标物是否已经稳定位于指定表面、是否仍由手支撑、表面身份是否可确认。
- Place 是 persistent_state：即使放置/释放过程发生在上一窗口，只要 CURRENT_NOW 能确认指定目标物稳定留在指定 sur_a 且手已不再支撑，就必须判定 Place succeeded；不要求当前视频重新出现释放动作。
- succeeded：视频中有直接证据表明该步骤完成。中间步骤只需在窗口内真实发生过，不要求其后置条件保持到 NOW；例如 Pick 后继续 Carry/Place，Pick 仍可 succeeded。
- {final_state_rule}
- in_progress：该步骤正在执行、尚未完成，或者因遮挡、画质、物体身份等原因暂时无法确认；description_zh 必须写明具体原因。可恢复的抓空、滑脱、掉落后继续尝试属于 in_progress。
- 如果画面和目标物清晰可见，但当前窗口尚未开始相关动作或目标物仍保持初始状态，返回 in_progress。
- failed：直接可见该步骤已失败；只有 failed 填自然语言 failure_reason，不使用预定义枚举。
- 在判断动作状态前先核对实际被操作物体是否为指定 obj_a。不得因为某个物体正在被手操作、位于画面中央或最显眼，就自动把它称为 obj_a。
- 能确认实际操作物不是 obj_a 时返回 failed；无法确认物体身份时返回 in_progress，并说明不确定原因。
- 后续步骤不能绕过尚未 succeeded 的前序步骤；若当前步骤 failed，立即在该步骤停止 step_updates。
- 不能仅凭手靠近、动作停止、夹爪闭合或短暂接触推断成功。

输出规则：
- step_updates 至少包含当前 step_id，并且只能是未确认后缀的连续前缀。
- evidence 只写直接可见事实，不写隐藏推理、意图、控制建议或 decision。返回前逐条检查：把视频冻结在该 timestamp_s 时，单张画面必须与 observation 的物体、接触关系、位置和状态一致；如果冻结帧不能支持这句话，就不能使用该时间点和描述。
- evidence 的 observation 不得把“由后续状态推测此前发生过”写成当前帧事实。例如当前帧正在擦拭时，可以写“纸巾正由手按压在桌面上”，不能写“纸巾刚被完全拿起并稳定握持在空中”。
- 所有时间戳相对本次 {duration} 秒 WINDOW 开头，范围 0 到 {duration} 秒。
- 中间步骤 succeeded 的 completion_evidence_timestamp_s 可位于窗口任意时刻。
- {final_completion_rule}
- 只有 succeeded 填 completion_evidence_timestamp_s；其他状态必须为 null。
- 只有 failed 填 failure_reason；其他状态必须为 null。
- 顶层 status 概括整条任务：全部步骤 succeeded 才是 succeeded；出现 failed 是 failed；其他情况都是 in_progress。
- {task_completion_rule}
- 只返回符合 JSON Schema 的 JSON。"""
