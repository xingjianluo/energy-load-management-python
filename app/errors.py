"""归一化错误：内部异常统一映射为可区分、不泄露实现细节的对外错误。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("elm.errors")


class AppError(Exception):
    """对外错误基类：携带稳定错误码与 HTTP 状态码。"""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str = "", detail: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.detail = detail or {}

    def to_payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.detail}}


class ValidationError(AppError):
    """请求体结构非法（非单条遥测拒绝，而是整个请求无法解析）。"""

    code = "validation_error"
    http_status = 422


class ConflictError(AppError):
    """并发冲突已被确定性地裁决，重试不会改变结果。"""

    code = "conflict"
    http_status = 409


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404


class StoreError(AppError):
    """持久化失败：对外不暴露路径/IO 细节。"""

    code = "store_unavailable"
    http_status = 503


def register_error_handlers(app: FastAPI) -> None:
    """注册异常处理器：任何内部异常都被归一化为稳定 JSON 错误。"""

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # 记录完整堆栈到日志，但对外只返回归一化错误码
        logger.exception("unhandled error: %s", exc)
        err = AppError("internal error")
        return JSONResponse(status_code=err.http_status, content=err.to_payload())
