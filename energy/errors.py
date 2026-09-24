"""对外可区分的归一化错误类型（骨架）。"""

from enum import Enum


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "validation_error"
    UNKNOWN_DEVICE = "unknown_device"
    UNKNOWN_METRIC = "unknown_metric"
    ILLEGAL_UNIT = "illegal_unit"
    MISSING_FIELD = "missing_field"
    MALFORMED_VALUE = "malformed_value"
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    BAD_TIMESTAMP = "bad_timestamp"
    STALE_TIMESTAMP = "stale_timestamp"
    FUTURE_TIMESTAMP = "future_timestamp"
    OUT_OF_ORDER = "out_of_order"
    BATCH_EMPTY = "batch_empty"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    STORAGE_ERROR = "storage_error"
    INTERNAL_ERROR = "internal_error"


class AppError(Exception):
    """所有可预期业务错误的基类。"""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = 500

    def __init__(self, message: str = "", details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message or self.code.value
        self.details = details or {}


class ValidationAppError(AppError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = 422


class BatchEmptyAppError(AppError):
    code = ErrorCode.BATCH_EMPTY
    http_status = 422


class NotFoundAppError(AppError):
    code = ErrorCode.NOT_FOUND
    http_status = 404


class ConflictAppError(AppError):
    code = ErrorCode.CONFLICT
    http_status = 409


class StorageAppError(AppError):
    code = ErrorCode.STORAGE_ERROR
    http_status = 500
