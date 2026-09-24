"""FastAPI 应用工厂。"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from api.errors import (
    app_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from api.routes import router
from energy import __version__
from energy.errors import AppError


def create_app() -> FastAPI:
    app = FastAPI(
        title="energy-load-management",
        version=__version__,
        description="本地能源监测与负载调度：幂等遥测接入、滞回调度、可重放事件日志。",
    )
    app.include_router(router)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    @app.get("/")
    def read_root() -> dict:
        # 保持骨架既有对外行为兼容
        return {"Hello": "World"}

    return app
