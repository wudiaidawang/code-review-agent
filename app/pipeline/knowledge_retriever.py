"""可插拔知识检索器 — 为 LLM 审查提供规范依据

M3：检索失败降级为空上下文，不阻塞 Pipeline。
知识条目带来源、版本、许可信息。
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class KnowledgeRetriever(Protocol):
    """可插拔知识检索器协议。任何实现只需满足此接口。"""

    def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """返回 top_k 条知识条目，每条含 content/source/version/license。"""
        ...


class NullRetriever:
    """空检索器 — 默认实现，始终返回空结果（M3 降级策略）。"""

    def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        return []


class StaticKnowledge:
    """内置静态知识条目 — 不依赖 ChromaDB/embedding，直接按规则匹配。

    用作 RAG 不可用时的降级方案。
    """

    _RULES: list[dict] = [
        {"content": "避免使用 eval/exec；使用 ast.literal_eval 或更安全的替代方案。", "source": "OWASP", "version": "2021", "license": "CC-BY-SA-4.0", "tags": ["security", "injection"]},
        {"content": "SQL 查询应使用参数化查询（parameterized query），禁止字符串拼接。", "source": "OWASP Top 10 A03:2021", "version": "2021", "license": "CC-BY-SA-4.0", "tags": ["security", "sql", "injection"]},
        {"content": "密码/密钥/Token 不应硬编码在源码中；使用环境变量或密钥管理服务。", "source": "OWASP Top 10 A07:2021", "version": "2021", "license": "CC-BY-SA-4.0", "tags": ["security", "credentials"]},
        {"content": "函数参数默认值不应使用可变对象（如 [] 或 {}）；使用 None + 内部初始化。", "source": "Google Python Style Guide", "version": "2.59", "license": "CC-BY-4.0", "tags": ["style", "python"]},
        {"content": "异常捕获应明确指定异常类型（except ValueError），避免裸 except。", "source": "PEP 8 / Google Python Style Guide", "version": "2.59", "license": "CC-BY-4.0", "tags": ["style", "python", "error-handling"]},
        {"content": "文件操作应使用 with 语句（上下文管理器）确保资源释放。", "source": "Python Documentation", "version": "3.10", "license": "PSF", "tags": ["style", "python", "resource"]},
        {"content": "对不可信输入使用 html.escape/shlex.quote 等转义，防止命令/HTML 注入。", "source": "CWE-79 / CWE-78", "version": "4.15", "license": "MITRE", "tags": ["security", "injection", "xss"]},
    ]

    def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """简单关键词匹配：query 中的词命中 tags 即返回。"""
        query_lower = query.lower()
        scored = []
        for r in self._RULES:
            score = sum(1 for tag in r["tags"] if tag in query_lower)
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:top_k]]
