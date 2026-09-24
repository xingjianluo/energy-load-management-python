"""归一化错误类型（骨架）。内部异常统一映射为可区分的对外错误。"""
from __future__ import annotations


class AppError(Exception):
    """对外可区分、不泄露实现细节的错误基类。"""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class ValidationError(AppError):
    code = "validation_error"
    http_status = 422


class ConflictError(AppError):
    code = "conflict"
    http_status = 409


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404
