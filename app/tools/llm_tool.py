"""LLM 工具 — OpenAI 兼容接口（智谱 GLM / DeepSeek / Qwen 均可，改 env 即可切换）"""

import os
from dotenv import load_dotenv
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

load_dotenv()


def get_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("ZHIPU_API_KEY"),
        base_url=os.getenv("ZHIPU_API_URL"),
    )


def get_model() -> str:
    return os.getenv("ZHIPU_MODEL", "glm-4.5-air")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def chat_completion(messages: list[dict], temperature: float = 0.3, max_tokens: int = 2000) -> str:
    """底层对话调用，失败自动重试（指数退避，最多 3 次）。返回文本内容。"""
    client = get_client()
    response = client.chat.completions.create(
        model=get_model(),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content


def chat(prompt: str, system: str = "", temperature: float = 0.3, max_tokens: int = 2000) -> str:
    """单轮对话，返回文本内容"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat_completion(messages, temperature=temperature, max_tokens=max_tokens)
