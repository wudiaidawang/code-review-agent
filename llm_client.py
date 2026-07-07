"""LLM 连接 — 智谱 GLM (OpenAI 兼容接口)"""

import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


def get_zhipu_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("ZHIPU_API_KEY"),
        base_url=os.getenv("ZHIPU_API_URL"),
    )


def get_zhipu_model() -> str:
    return os.getenv("ZHIPU_MODEL", "glm-4.5-air")


def chat(prompt: str, system: str = "", temperature: float = 0.3, max_tokens: int = 2000) -> str:
    """单轮对话，返回文本内容"""
    client = get_zhipu_client()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = client.chat.completions.create(
        model=get_zhipu_model(),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content
