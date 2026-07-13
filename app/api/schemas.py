"""API 请求/响应 Pydantic schema — 统一错误格式。"""

from pydantic import BaseModel, Field


# ---- 请求 ----

class ReviewRequest(BaseModel):
    repo_path: str = Field(..., description="本地仓库路径或 URL")
    base_ref: str = Field(default="HEAD~1", description="基准 ref")
    head_ref: str = Field(default="HEAD", description="目标 ref")


# ---- 响应 ----

class InvestigateRequest(BaseModel):
    repo_path: str = Field(..., description="本地仓库路径")
    question: str = Field(..., description="关于代码库的问题（中文/英文）")


class InvestigateResponse(BaseModel):
    question: str
    answer: str
    evidence: list[dict]
    files_visited: list[str]
    findings: list[str]
    trace: list[str]
    duration_ms: float


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class RunSummary(BaseModel):
    run_id: str
    repo_url: str
    base_ref: str
    head_ref: str
    created_at: str
    risk_level: str
    issue_count: int
    duration_ms: float


class RunListResponse(BaseModel):
    runs: list[RunSummary]
    total: int


class ReviewResponse(BaseModel):
    run_id: str
    repo_url: str
    base_ref: str
    head_ref: str
    created_at: str
    plan: dict
    change_set: dict
    issues: list[dict]
    evidence: list[dict]
    trace: list[dict]
    markdown: str
    json_report: str
    duration_ms: float
