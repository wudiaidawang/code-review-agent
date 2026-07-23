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


class OwnedInvestigateRequest(BaseModel):
    repo_id: str = Field(..., min_length=16, max_length=80)
    question: str = Field(..., min_length=1, max_length=4000)


class OwnedReviewRequest(BaseModel):
    repo_id: str = Field(..., min_length=16, max_length=80)
    base_ref: str = Field(default="HEAD~1", min_length=1, max_length=128)
    head_ref: str = Field(default="HEAD", min_length=1, max_length=128)


class InvestigateResponse(BaseModel):
    question: str
    answer: str
    evidence: list[dict]
    files_visited: list[str]
    findings: list[str]
    plan: list[str]
    trace: list[str]
    steps: list[dict] = []
    investigation_id: str = ""
    is_follow_up: bool = False
    reused_evidence_refs: list[str] = []
    duration_ms: float


class JobAcceptedResponse(BaseModel):
    job_id: str
    status: str
    stream_url: str
    result_url: str


class GitHubImportRequest(BaseModel):
    url: str = Field(..., description="GitHub 公开仓库 HTTPS 地址")


class AuthRequest(BaseModel):
    username: str
    password: str


class ConversationRequest(BaseModel):
    id: str
    title: str = "新建调查"
    repo: dict | None = None
    messages: list[dict] = []
    version: int = Field(default=0, ge=0)


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
