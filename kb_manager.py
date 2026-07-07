"""ChromaDB 知识库管理 — 代码规范、漏洞模式、审查历史"""

import os
import chromadb
from chromadb.config import Settings


class KnowledgeBase:
    """管理3类审查知识库"""

    def __init__(self, persist_dir: str = "./chroma_db"):
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        self.client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        self._ensure_collections()

    # ------------------------------------------------------------------
    def _ensure_collections(self):
        existing = {c.name for c in self.client.list_collections()}

        if "code_standards" not in existing:
            self.code_standards = self.client.create_collection(
                name="code_standards",
                metadata={"description": "代码规范知识库 (Google Python Style Guide 等)"},
            )
        else:
            self.code_standards = self.client.get_collection("code_standards")

        if "vuln_patterns" not in existing:
            self.vuln_patterns = self.client.create_collection(
                name="vuln_patterns",
                metadata={"description": "常见漏洞模式 (OWASP Top 10 / CWE)"},
            )
        else:
            self.vuln_patterns = self.client.get_collection("vuln_patterns")

        if "review_history" not in existing:
            self.review_history = self.client.create_collection(
                name="review_history",
                metadata={"description": "项目历史审查记录"},
            )
        else:
            self.review_history = self.client.get_collection("review_history")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def add_code_standards(self, items: list[dict]):
        """批量写入代码规范"""
        if not items:
            return
        self.code_standards.add(
            ids=[item["id"] for item in items],
            documents=[item["text"] for item in items],
            metadatas=[item.get("meta", {}) for item in items],
        )

    def add_vuln_patterns(self, items: list[dict]):
        """批量写入漏洞模式"""
        if not items:
            return
        self.vuln_patterns.add(
            ids=[item["id"] for item in items],
            documents=[item["text"] for item in items],
            metadatas=[item.get("meta", {}) for item in items],
        )

    def add_review_record(self, item: dict):
        """写入一条审查记录"""
        self.review_history.add(
            ids=[item["id"]],
            documents=[item["text"]],
            metadatas=[item.get("meta", {})],
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query(self, query_text: str, n_results: int = 5) -> list[str]:
        """跨知识库检索，返回相关文本列表"""
        results = []
        for col in [self.code_standards, self.vuln_patterns, self.review_history]:
            try:
                r = col.query(query_texts=[query_text], n_results=n_results)
                docs = r.get("documents", [[]])[0]
                results.extend([d for d in docs if d])
            except Exception:
                continue
        return results
