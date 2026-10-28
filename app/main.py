from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import get_settings
from app.core.exceptions import (
    CredentialStoreError,
    DataSourceConnectionError,
    NotImplementedFeatureError,
    ResourceNotFoundError,
    UnsafeOperationError,
)
from app.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="企业数据库自动理解与 NL2SQL 的模块化单体骨架。",
)
app.include_router(api_router, prefix=settings.api_prefix)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # 校验失败的响应只保留 loc/type/msg。绝不回显 input、ctx 或异常字符串——
    # 原始请求体可能包含密码等凭证，SecretStr 无法保护校验阶段。
    safe_errors = [
        {
            "loc": [str(item) for item in error.get("loc", [])],
            "type": str(error.get("type", "")),
            "msg": str(error.get("msg", ""))[:300],
        }
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": safe_errors})


@app.exception_handler(ResourceNotFoundError)
async def resource_not_found_handler(request: Request, exc: ResourceNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc), "resource": exc.resource})


@app.exception_handler(NotImplementedFeatureError)
async def not_implemented_handler(request: Request, exc: NotImplementedFeatureError) -> JSONResponse:
    return JSONResponse(status_code=501, content={"detail": str(exc)})


@app.exception_handler(UnsafeOperationError)
async def unsafe_operation_handler(request: Request, exc: UnsafeOperationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def bad_request_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(CredentialStoreError)
async def credential_store_error_handler(
    request: Request, exc: CredentialStoreError
) -> JSONResponse:
    # 凭证文件损坏/不可写：返回安全错误（503），绝不静默当空存储继续。
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(DataSourceConnectionError)
async def connection_error_handler(request: Request, exc: DataSourceConnectionError) -> JSONResponse:
    # 服务层通常已把连接错误转为 ok=false 结果；兜底路径也只返回脱敏信息。
    return JSONResponse(
        status_code=502,
        content={"detail": str(exc), "reason": exc.reason},
    )


@app.get("/health", tags=["system"])
def health_check() -> dict[str, str]:
    # TODO(M8): 如有部署需要，再加入元数据库和模型服务的健康检查。
    return {"status": "ok", "app": settings.app_name}
