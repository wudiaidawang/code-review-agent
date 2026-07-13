"""FastAPI 应用工厂。"""

from fastapi import FastAPI
from app.api.routes import register_routes


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Code Review Platform",
        version="1.0.0",
        description="面向代码变更审查的 AI 平台 — 确定性工具 + LLM 语义推理",
    )
    register_routes(app)
    return app
