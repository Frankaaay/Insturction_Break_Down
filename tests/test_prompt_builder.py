# -*- coding: utf-8 -*-
"""Planner prompt 的静态回归测试。"""

import unittest

from primitives import load_primitives_text
from prompt_builder import build_messages


class PromptBuilderTests(unittest.TestCase):
    def setUp(self):
        self.primitives = load_primitives_text()
        self.system_prompt = build_messages("测试指令")[0]["content"]

    def test_variable_descriptions_are_neutral_and_logic_scoped(self):
        self.assertIn("具体角色由所在 logic 决定", self.primitives)
        self.assertNotIn("如门、柜门", self.primitives)
        self.assertNotIn("如抽屉、推拉门", self.primitives)
        self.assertIn("只由该动作选中的 logic 模板决定", self.system_prompt)

    def test_open_uses_obj_and_distinguishes_power_on(self):
        self.assertIn("logic0: Open the <obj_a>.", self.system_prompt)
        self.assertIn("“打开笔记本电脑”默认指翻开屏幕/上盖", self.system_prompt)
        self.assertIn('"action_id": "A_017", "action": "Open"', self.system_prompt)
        self.assertIn('"button_a": "笔记本电脑电源按钮"', self.system_prompt)

    def test_tool_use_example_omits_incidental_carry(self):
        start = self.system_prompt.index("指令: 拿纸巾擦桌子")
        end = self.system_prompt.index("指令: 打开笔记本电脑", start)
        example = self.system_prompt[start:end]
        self.assertIn('"action_id": "A_001"', example)
        self.assertIn('"action_id": "A_009"', example)
        self.assertNotIn('"action_id": "A_003"', example)
        self.assertIn("不得仅因为物体在 Pick、Wipe、Insert", self.system_prompt)

    def test_explicit_relocation_keeps_pick_carry_place(self):
        start = self.system_prompt.index("指令: 把水壶放到桌子上")
        end = self.system_prompt.index("指令: 把牛奶放进冰箱", start)
        example = self.system_prompt[start:end]
        action_positions = [
            example.index('"action_id": "A_001"'),
            example.index('"action_id": "A_003"'),
            example.index('"action_id": "A_002"'),
        ]
        self.assertEqual(action_positions, sorted(action_positions))


if __name__ == "__main__":
    unittest.main()
