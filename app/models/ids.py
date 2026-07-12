"""稳定 ID 生成 — 可引用事实/结论对象的身份来源

Evidence / Finding / Issue / ReviewRun 都需要一个稳定 id，供跨对象按 id 引用
（如 Issue.evidence_ids 反查 Evidence）。带前缀便于人肉扫描：ev_/fnd_/iss_/run_。
"""

import uuid


def new_id(prefix: str) -> str:
    """生成形如 `ev_1a2b3c4d` 的短 id。前缀标明对象种类，后缀取 uuid4 前 8 位。"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"
