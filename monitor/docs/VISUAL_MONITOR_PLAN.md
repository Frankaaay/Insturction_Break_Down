# Mage + Qwen 纯视觉 Monitor 方案（讨论稿 V0.1）

> 状态：待讨论，尚未冻结。本文只定义第一阶段的纯视觉 Monitor，不包含触觉、快慢 Monitor、恢复工具或真正的 Replan Harness。

## 1. 目标与范围

第一阶段验证以下组合是否显著优于 Qwen-VL-32B 单独使用 `BEFORE + NOW`：

> Mage 负责理解长视频中的过程和事件，Qwen-VL-32B 负责结合 `BEFORE + NOW` 做最终决策。

当前 Monitor 只输出三种决策：

- `next`
- `wait`
- `replan`

暂不实现：

- 触觉、夹爪、IMU 等非视觉信息融合；
- Fast/Slow Monitor；
- Retry、Reset、Rewrite、Human Intervention 等恢复工具；
- 真正的剩余任务重规划；
- 机器人在线干预。

当前 `replan` 的临时含义是：

> 当前原子操作无法依靠当前执行器和原指令继续完成，需要交给未来的 Recovery/Replan Harness 处理。

## 2. 整体结构

```text
原子操作开始
    ↓
记录 BEFORE
    ↓
Mage 观看从操作开始到 NOW 的完整视频
    ↓
Mage 输出结构化事件总结和关键时间点
    ↓
提取 Mage 指定的少量原始证据帧
    ↓
Qwen-VL-32B 输入：
BEFORE + NOW + Mage 总结 + 可选证据帧
    ↓
Qwen 输出：next / wait / replan
```

### 2.1 Mage 的职责

Mage 是长视频过程观察器：

- 观察完整动作过程；
- 识别抓取、抬起、滑动、掉落、重新抓取等事件；
- 判断事件是否已经恢复；
- 给出关键时间点；
- 不直接决定 `next`、`wait` 或 `replan`。

初步视频长度：

- UMI：从原子操作开始到当前时刻，最长约 12 秒；
- 真实机器人：20-30 秒 Rolling Window；
- 超长动作的历史摘要和窗口机制后续讨论。

### 2.2 Qwen-VL-32B 的职责

Qwen-VL-32B 是最终判断器：

- 根据 `BEFORE` 理解初始状态；
- 根据 `NOW` 判断当前后置条件；
- 根据 Mage 总结理解中间发生的事情；
- 必要时查看 Mage 指定的中间证据帧；
- 判断当前操作应该 `next`、`wait` 还是 `replan`。

Qwen 不能只看 Mage 的文字总结。至少应始终保留：

- `BEFORE`；
- `NOW`；
- Mage 结构化报告；
- Mage 关键事件对应的少量原始证据帧。

否则 Mage 一旦漏检或描述错误，Qwen 无法根据原始视觉证据复核。

## 3. Mage Prompt 设计

Mage Prompt 需要从头设计。其目标是视觉事件抽取，而不是操作成败判断。

### 3.1 输入

```text
Atomic action
Manipulated object
Target/destination
Expected visual transitions
Success predicates
Relevant camera ID
Elapsed time
Complete video from action start to NOW
```

### 3.2 输出草案

```json
{
  "current_phase": "approach | grasp | transport | release | verify | unknown",
  "events": [
    {
      "type": "missed_grasp | grasp_acquired | slip | drop | regrasp | wrong_object | wrong_destination | stalled | goal_visible",
      "start_s": 0.0,
      "end_s": 0.0,
      "resolved": true,
      "confidence": 0.0,
      "evidence": "observable description"
    }
  ],
  "latest_state": [
    {
      "predicate": "object_in_gripper",
      "value": "true | false | unknown",
      "evidence": "observable description"
    }
  ],
  "critical_timestamps_s": [0.0],
  "unknowns": []
}
```

### 3.3 核心约束

- 只报告可观察事实；
- 看不清时输出 `unknown`；
- 不根据机械臂姿态或动作意图猜测抓取成功；
- 每个事件必须带时间；
- 必须区分异常是否已经恢复；
- 不输出 `next`、`wait` 或 `replan`。

## 4. Qwen Prompt 设计

Qwen Prompt 以 VoLoAgent 的 Monitoring Prompt 为底稿，在其基础上增加 Mage 信息和新的决策约束。

### 4.1 复用 VoLoAgent 的部分

保留：

- Overall Task；
- Current Subgoal；
- Remaining Subgoals；
- `BEFORE + NOW`；
- `complete / in_progress / failure`；
- `next / continue / replan`；
- 严格 JSON 输出；
- 证据不足时不得提前判断完成。

其中将 VoLoAgent 的 `continue` 在本项目接口中命名为 `wait`。

### 4.2 新增输入

```text
MAGE TEMPORAL REPORT
MAGE EVIDENCE FRAMES
SUCCESS PREDICATES
INVARIANTS
CURRENT EXECUTOR CAPABILITIES
ELAPSED TIME / TIMEOUT
PREVIOUS MONITOR STATUS
```

### 4.3 需要修改的 VoLoAgent 规则

不能直接使用“检测到掉落就一律 Replan”的规则。最终决策应考虑当前执行器能否从 `NOW` 状态自行恢复。

建议 Qwen 输出：

```json
{
  "status": "success | in_progress | failed",
  "decision": "next | wait | replan",
  "reason_code": "GOAL_SATISFIED | NORMAL_PROGRESS | RETRYING | MISSED_GRASP | DROP | WRONG_OBJECT | WRONG_DESTINATION | STALLED | PLAN_INVALID",
  "mage_verification": "supported | partially_supported | rejected",
  "reason": "one short evidence-based sentence"
}
```

## 5. `next`、`wait`、`replan` 定义

### 5.1 `next`

当前原子操作已经成功。

要求：

- `NOW` 明确满足全部成功谓词；
- 相比 `BEFORE`，目标状态确实发生变化；
- 结果已经保持稳定；
- 没有尚未解决的关键异常。

例如：

- 目标物体已经被稳定拿起；
- 物体已经搬到目标位置；
- 门已经关闭并保持关闭。

### 5.2 `wait`

当前操作尚未成功，但当前执行器使用原指令仍有能力自行完成。

包括：

- 机械臂仍在接近或对准；
- 第一次没有拿起来，但正在继续尝试；
- 抓空后正在重新调整；
- 物体短暂滑动，但仍在控制中；
- 物体掉回桌面，但机器人正在重新抓取；
- Mage 报告了异常，但异常已经在同一次 Attempt 中恢复；
- 当前证据不足，且尚未超时。

核心定义：

> 只要不改变当前指令、不切换工具、不增加新步骤，当前 VLA 仍能自行完成，就输出 `wait`。

因此：

```text
没拿起来不等于必须 replan。
没拿起来但还在继续尝试，应输出 wait。
```

### 5.3 `replan`

当前执行器无法使用原指令自行恢复并完成操作。

包括：

- 抓错对象；
- 物体掉到不可达位置；
- 机器人掉落物体后空手继续错误动作；
- 杯子倒下，当前“拿杯倒水”指令已经无法继续；
- 液体洒出或关键环境状态被破坏；
- 放错目标；
- 已完成状态发生 Regression；
- 多次尝试后仍无进展；
- 超过等待时间或重试预算；
- 必须增加恢复步骤、切换技能或改写指令。

核心定义：

> 如果当前状态不能由当前执行器使用同一条指令自行恢复，则输出 `replan`。

当前阶段不进一步区分：

```text
retry / reset / rewrite / human / abort
```

它们暂时都进入 `replan`。未来加入 Recovery Harness 后，再将其展开成不同恢复动作。

## 6. 数据要求

不能继续只使用 Episode 级别的 `success/fail` 标签，需要标注关键事件及对应的视频 Prefix。

### 6.1 `next` 数据

现有成功数据基本充足，但应覆盖：

- 快速成功；
- 缓慢成功；
- 多次尝试后成功；
- 遮挡情况下成功；
- 操作完成后保持稳定；
- 看似完成但随后发生 Regression。

### 6.2 `wait` 数据

这是当前最重要的数据类型：

- 第一次抓空但继续尝试；
- 多次尝试后最终成功；
- 暂时没有拿起来；
- 滑动后重新稳定；
- 掉回桌面后重新抓取；
- 正常执行但动作较慢；
- 暂时遮挡或视觉证据不足。

现有“尝试几次最后成功”的数据应拆成多个时间点：

```text
第一次失败                  -> wait
重新对准                    -> wait
成功抓住但尚未完成整个 subgoal -> wait
最终完成                    -> next
```

### 6.3 `replan` 数据

需要覆盖：

- Wrong Object；
- Wrong Destination；
- 物体掉到不可达位置；
- 杯子倾倒；
- 环境状态被破坏；
- 已完成状态发生 Regression；
- 当前执行器空手继续错误动作；
- 明确超时或重试耗尽。

Wrong Object 和 Wrong Destination 可以先通过真实视频的 Instruction Relabeling 构造，后续再补真实失败数据。

### 6.4 不可评测数据

如果视频突然结束，但不知道：

- 是否已经达到 Timeout；
- 后续是否还会继续尝试；
- 当前执行器为什么停止；

则标记为：

```text
not_evaluable
```

这种数据可以评测 Mage 是否识别出“没有拿起来”或“没有进展”，但不应用于评测 Qwen 应该输出 `wait` 还是 `replan`。

## 7. 第一轮实验

对同一批人工标注数据比较：

1. Qwen-VL-32B：仅 `BEFORE + NOW`；
2. Mage：单独判断；
3. Mage 总结 + Qwen `BEFORE + NOW`；
4. Mage 总结 + 关键证据帧 + Qwen `BEFORE + NOW`。

主要指标：

- `next` Precision：避免过早进入下一步；
- `replan` Recall：避免漏掉真正阻塞性的失败；
- `wait` Accuracy：避免打断正常重试；
- 状态翻转次数；
- 失败检测延迟；
- Mage 关键事件时间覆盖率。

## 8. 待商榷问题

以下问题尚未确定，需要讨论后再修改方案和 Prompt：

1. 物体掉回桌面时，什么情况下认为 VLA 能自行重新抓取？
2. 连续几次抓取失败后，应从 `wait` 转为 `replan`？
3. 使用统一 Timeout，还是不同原子操作使用不同 Timeout？
4. Qwen 是否始终查看 Mage 证据帧，还是只在 Mage 报告异常时查看？
5. UMI 数据的 Mage 视频窗口使用 8 秒还是 12 秒？
6. `replan` 是否保留该名称，还是内部使用更准确的 `needs_recovery`？
7. 杯子倾倒、洒水等状态是否需要额外定义 Invariant？

## 9. 当前方案摘要

> Mage 找出完整视频中的事件；Qwen 基于 VoLoAgent 的 `BEFORE + NOW` 框架判断：已经完成则 `next`，当前执行器还能依靠原指令自行恢复则 `wait`，无法自行恢复则 `replan`。

在上述定义和边界案例达成一致前，本方案只作为讨论稿，不应直接视为实现规格。
