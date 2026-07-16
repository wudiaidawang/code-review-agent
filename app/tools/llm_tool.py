"""LLM 工具 — OpenAI 兼容接口（智谱 GLM / DeepSeek / Qwen 均可，改 env 即可切换）"""

import os
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

load_dotenv()


def get_client(timeout: float | None = None):
    """实际调用时才加载可选 SDK，保证 mock/offline 测试无需 OpenAI 依赖。"""
    from openai import OpenAI

    return OpenAI(
        api_key=os.getenv("ZHIPU_API_KEY"),
        base_url=os.getenv("ZHIPU_API_URL"),
        timeout=timeout if timeout is not None else 20.0,
        # 重试由 chat_completion 的 tenacity 统一管理，避免 SDK 与外层叠加重试
        # 导致一次评测请求长时间无可观测地阻塞。
        max_retries=0,
    )


def get_model() -> str:
    return os.getenv("ZHIPU_MODEL", "glm-4.5-air")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def chat_completion(messages: list[dict], temperature: float = 0.3, max_tokens: int = 2000,
                    timeout: float | None = None, extra_body: dict | None = None) -> str:
    """底层对话调用，失败自动重试（指数退避，最多 3 次）。返回文本内容。

    extra_body 透传 OpenAI 兼容接口之外的厂商参数，
    例如智谱推理模型的 {"thinking": {"type": "disabled"}}。
    """
    client = get_client(timeout=timeout)
    kwargs = {}
    if extra_body:
        kwargs["extra_body"] = extra_body
    response = client.chat.completions.create(
        model=get_model(),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        **kwargs,
    )
    return response.choices[0].message.content


def chat(prompt: str, system: str = "", temperature: float = 0.3, max_tokens: int = 2000,
         timeout: float | None = None, extra_body: dict | None = None) -> str:
    """单轮对话，返回文本内容"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat_completion(messages, temperature=temperature, max_tokens=max_tokens,
                           timeout=timeout, extra_body=extra_body)
