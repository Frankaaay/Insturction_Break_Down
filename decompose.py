# -*- coding: utf-8 -*-
"""把复杂指令拆解为原子操作序列的命令行工具。

用法:
    python decompose.py "把水壶放到桌子上" --provider deepseek
    python decompose.py "把牛奶放进冰箱" --provider glm --json
    python decompose.py                       # 不带指令 → 进入交互式控制台

退出码(单条模式): 0=拆解成功  2=指令不明确  3=无法完成  1=其他错误
"""

import argparse
import json
import re
import sys

# Windows 控制台默认编码非 UTF-8,中文输入会变乱码、输出会 UnicodeEncodeError
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from primitives import load_registry, validate_steps
from prompt_builder import build_messages
from providers import PROVIDERS, chat

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_llm_json(text: str) -> dict:
    """剥离可能的 Markdown 代码围栏后解析 JSON。"""
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    return json.loads(cleaned)


def decompose(instruction: str, provider: str, model: str | None = None) -> dict:
    """调 LLM 拆解指令,校验后返回结果 dict(status/steps/reason)。"""
    messages = build_messages(instruction)
    registry = load_registry()
    last_error = None
    for attempt in range(2):  # JSON 解析或校验失败时自动重试 1 次
        raw = chat(provider, messages, model=model)
        try:
            result = parse_llm_json(raw)
        except json.JSONDecodeError as e:
            last_error = f"LLM 返回的不是合法 JSON: {e}\n原始返回:\n{raw}"
            continue
        status = result.get("status")
        if status == "ok":
            errors = validate_steps(result.get("steps", []), registry)
            if errors:
                last_error = "步骤校验失败:\n" + "\n".join(errors) + f"\n原始返回:\n{raw}"
                continue
            return result
        if status in ("ambiguous", "infeasible"):
            if not result.get("reason"):
                last_error = f"status 为 {status} 但缺少 reason\n原始返回:\n{raw}"
                continue
            return result
        last_error = f"未知 status: {status!r}\n原始返回:\n{raw}"
    raise RuntimeError(last_error)


def render_text(instruction: str, result: dict) -> str:
    lines = [f"指令: {instruction}", ""]
    for i, step in enumerate(result["steps"], 1):
        tag = f"[{step['action_id']} {step['action']}/logic{step['logic']}]"
        en = f"  ({step['en']})" if step.get("en") else ""
        lines.append(f"{i}. {tag}  {step['zh']}{en}")
    return "\n".join(lines)


def run_once(instruction: str, provider: str, model: str | None, as_json: bool) -> int:
    """拆解一条指令并打印结果,返回退出码。"""
    try:
        result = decompose(instruction, provider, model)
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1

    if as_json:
        output = dict(result)
        output["instruction"] = instruction
        print(json.dumps(output, ensure_ascii=False, indent=2))
    elif result["status"] == "ok":
        print(render_text(instruction, result))
    elif result["status"] == "ambiguous":
        print(f"[指令不明确] {result['reason']}")
    else:
        print(f"[无法完成] {result['reason']}")

    return {"ok": 0, "ambiguous": 2, "infeasible": 3}[result["status"]]


def interactive(provider: str, model: str | None, as_json: bool) -> int:
    """交互式控制台:循环读入指令并拆解,exit / quit / q 或 Ctrl+C 退出。"""
    print(f"指令拆解控制台 (提供商: {provider}"
          f"{', 模型: ' + model if model else ''})")
    print("输入指令后回车,输入 exit / quit / q 退出。\n")
    while True:
        try:
            instruction = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见!")
            return 0
        if not instruction:
            continue
        if instruction.lower() in ("exit", "quit", "q"):
            print("再见!")
            return 0
        run_once(instruction, provider, model, as_json)
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="把复杂指令拆解为原子操作序列")
    parser.add_argument("instruction", nargs="?", default=None,
                        help='待拆解的指令,如 "把水壶放到桌子上";省略则进入交互式控制台')
    parser.add_argument("--provider", default="deepseek", choices=list(PROVIDERS),
                        help="LLM 提供商 (默认: deepseek)")
    parser.add_argument("--model", default=None, help="覆盖该提供商的默认模型")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="输出结构化 JSON 而非文本列表")
    args = parser.parse_args()

    if args.instruction is None:
        return interactive(args.provider, args.model, args.as_json)
    return run_once(args.instruction, args.provider, args.model, args.as_json)


if __name__ == "__main__":
    sys.exit(main())
