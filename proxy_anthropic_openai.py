"""
轻量协议代理：Anthropic Messages API → OpenAI Chat Completions API
用于让 Claude Code 对接阿里百炼的 OpenAI 兼容端点
"""
from flask import Flask, request, jsonify
import requests
import uuid
import time

app = Flask(__name__)

UPSTREAM_BASE = "https://ws-6v9xyut4wbxm0e6w.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
UPSTREAM_KEY = "sk-ws-H.EDMPRMD.UgzO.MEQCICJX70KXTq4jKBHQ-c3LQXuN32GnRKArZi_52McULnFxAiAr9rKB3z52y8zVhaROZSvm_tgPpRH5ET0iGnF_fDHzpQ"


@app.route("/v1/messages", methods=["POST"])
def messages():
    body = request.get_json(force=True)

    # 1. 构造 OpenAI 请求体
    openai_messages = []

    # Anthropic 的 system 是顶层字段，OpenAI 是 message 角色
    if "system" in body:
        if isinstance(body["system"], str):
            openai_messages.append({"role": "system", "content": body["system"]})
        elif isinstance(body["system"], list):
            # system 可能是 [{"type":"text","text":"..."}]
            text = "".join(s.get("text", "") for s in body["system"] if s.get("type") == "text")
            if text:
                openai_messages.append({"role": "system", "content": text})

    for msg in body.get("messages", []):
        content = msg.get("content", "")
        # Anthropic content 可能是字符串或数组
        if isinstance(content, list):
            text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            content = "\n".join(text_parts)
        openai_messages.append({"role": msg["role"], "content": content})

    openai_body = {
        "model": body.get("model", "qwen-max"),
        "messages": openai_messages,
        "max_tokens": body.get("max_tokens", 4096),
    }
    if "temperature" in body:
        openai_body["temperature"] = body["temperature"]
    if "top_p" in body:
        openai_body["top_p"] = body["top_p"]
    if "stop_sequences" in body and isinstance(body["stop_sequences"], list) and body["stop_sequences"]:
        openai_body["stop"] = body["stop_sequences"]

    # 2. 发送到上游
    resp = requests.post(
        f"{UPSTREAM_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {UPSTREAM_KEY}",
            "Content-Type": "application/json",
        },
        json=openai_body,
        timeout=300,
    )

    if resp.status_code != 200:
        return jsonify({"error": f"upstream error: {resp.status_code}", "detail": resp.text}), 502

    data = resp.json()
    print(f"[proxy] upstream resp keys: {list(data.keys())}")

    # 百炼返回两种格式：简化 {"text":"..."} 或标准 {"choices":[...]}
    if "choices" in data:
        choice = data["choices"][0]
        text = choice["message"]["content"]
        finish_reason = choice.get("finish_reason")
    elif "text" in data:
        text = data["text"]
        finish_reason = data.get("finish_reason")
    else:
        return jsonify({"error": "unknown upstream format", "raw": data}), 502

    # 3. 翻译为 Anthropic 响应
    anthropic_resp = {
        "id": data.get("id", f"msg_{uuid.uuid4().hex[:12]}"),
        "type": "message",
        "role": "assistant",
        "model": data.get("model", body.get("model", "qwen-max")),
        "content": [{"type": "text", "text": text}],
        "stop_reason": map_stop_reason(finish_reason),
        "stop_sequence": None,
        "usage": {
            "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
        },
    }
    return jsonify(anthropic_resp)


def map_stop_reason(finish_reason):
    if finish_reason == "stop":
        return "end_turn"
    if finish_reason == "length":
        return "max_tokens"
    return finish_reason or "end_turn"


if __name__ == "__main__":
    print("代理启动: http://127.0.0.1:4000 → 阿里百炼 qwen-max")
    app.run(host="127.0.0.1", port=4000, debug=False)
