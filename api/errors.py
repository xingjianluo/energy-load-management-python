"""HTTP 错误归一化：AppError 与请求结构错误 -> 稳定 JSON，不泄露内部细节。"""

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from energy.errors import AppError, ErrorCode

logger = logging.getLogger("energy.api")


def _safe_details(details: dict) -> dict:
    # 只允许基础类型透出，避免泄露内部对象结构
    clean: dict = {}
    for key, value in details.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
        elif isinstance(value, (list, tuple)):
            clean[key] = [v for v in value if isinstance(v, (str, int, float, bool))]
    return clean


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.info("app error path=%s code=%s", request.url.path, exc.code.value)
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "error": exc.code.value,
            "message": exc.message,
            "details": _safe_details(exc.details),
        },
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # 请求级结构错误（类型不对/JSON 残缺/字段缺失）给出稳定、可机读的明细
    logger.info("request validation error path=%s", request.url.path)
    issues = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", []) if p != "body"]
        issues.append(
            {
                "field": ".".join(loc),
                "type": err.get("type", "invalid"),
                "message": err.get("msg", "invalid request"),
            }
        )
    return JSONResponse(
        status_code=422,
        content={
            "error": ErrorCode.VALIDATION_ERROR.value,
            "message": "request validation failed",
            "details": {"issues": issues},
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": ErrorCode.INTERNAL_ERROR.value,
            "message": "internal error",
            "details": {},
        },
    )
