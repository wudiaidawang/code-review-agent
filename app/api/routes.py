"""API 路由 — /review 提交审查、查询、健康检查。"""

import time
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.api.schemas import (
    ReviewRequest, ReviewResponse, RunSummary, RunListResponse,
)
from app.pipeline.review_pipeline import ReviewPipeline
from app.persistence.store import RunStore
from app.models.ids import new_id


store = RunStore()
pipeline = ReviewPipeline()


def register_routes(app):
    """向 FastAPI app 注册所有路由。"""

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/review", response_model=ReviewResponse)
    async def create_review(req: ReviewRequest):
        """提交一次代码审查请求。"""
        run_id = new_id("run")
        created_at = datetime.now(timezone.utc).isoformat()

        try:
            t0 = time.perf_counter()
            output = pipeline.run(req.repo_path, req.base_ref, req.head_ref)
            elapsed_ms = (time.perf_counter() - t0) * 1000
        except Exception as exc:
            raise HTTPException(status_code=500, detail={
                "code": "REVIEW_FAILED", "message": str(exc),
            })

        result = {
            "run_id": run_id,
            "repo_url": req.repo_path,
            "base_ref": req.base_ref,
            "head_ref": req.head_ref,
            "created_at": created_at,
            "plan": output.plan,
            "change_set": output.change_set,
            "issues": [i.to_dict() for i in output.issues],
            "evidence": [e.to_dict() for e in output.evidence],
            "trace": [t.to_dict() for t in output.trace],
            "markdown": output.markdown,
            "json_report": output.json,
            "duration_ms": round(elapsed_ms, 1),
        }

        store.save(run_id, result)
        return result

    @app.get("/review/{run_id}", response_model=ReviewResponse)
    async def get_review(run_id: str):
        """查询一次审查运行的结果。"""
        data = store.load(run_id)
        if data is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"run_id={run_id} 不存在",
            })
        return data

    @app.get("/runs", response_model=RunListResponse)
    async def list_runs():
        """列出所有历史审查运行。"""
        records = store.list_runs()
        return RunListResponse(
            runs=[RunSummary(
                run_id=r.run_id, repo_url=r.repo_url_or_path,
                base_ref=r.base_ref, head_ref=r.head_ref,
                created_at=r.created_at, risk_level=r.risk_level,
                issue_count=r.issue_count, duration_ms=r.duration_ms,
            ) for r in records],
            total=len(records),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail},
        )
