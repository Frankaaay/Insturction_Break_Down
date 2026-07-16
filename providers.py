# -*- coding: utf-8 -*-
"""LLM 提供商注册表。三家均兼容 OpenAI API 格式,新增提供商在 PROVIDERS 里加一行即可。"""

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

PROVIDERS = {
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZHIPU_API_KEY",
        "default_model": "glm-4.6",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "deepseek/deepseek-chat-v3-0324",
    },
}


def get_client(provider: str) -> tuple[OpenAI, str]:
    """返回 (client, default_model)。API key 从环境变量或 .env 读取。"""
    if provider not in PROVIDERS:
        raise ValueError(f"未知提供商 {provider!r},可选: {', '.join(PROVIDERS)}")
    cfg = PROVIDERS[provider]
    api_key = os.environ.get(cfg["env_key"])
    if not api_key:
        raise RuntimeError(
            f"环境变量 {cfg['env_key']} 未设置,请在 .env 中填入 {provider} 的 API key"
        )
    client = OpenAI(api_key=api_key, base_url=cfg["base_url"])
    return client, cfg["default_model"]


def chat(provider: str, messages: list[dict], model: str | None = None,
         temperature: float = 0.1) -> str:
    """调用所选提供商的 chat completion,返回回复文本。"""
    client, default_model = get_client(provider)
    resp = client.chat.completions.create(
        model=model or default_model,
        messages=messages,
        temperature=temperature,
    )
    return resp.choices[0].message.content
