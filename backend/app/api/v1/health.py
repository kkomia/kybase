"""存活探针。

基础设施级接口，只回显进程状态与版本，不含业务逻辑（工程规范 §3.3）。
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import API_VERSION, get_settings

router = APIRouter()


class HealthResponse(BaseModel):
    """探针响应。"""

    status: str
    app: str
    version: str
    api_version: str


@router.get("/health", response_model=HealthResponse, summary="服务存活探针")
def health() -> HealthResponse:
    """返回服务存活状态与版本信息。"""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        api_version=API_VERSION,
    )
