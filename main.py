from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI

from app.api import router
from app.config import Settings, load_settings
from app.dispatch import LoadDispatcher
from app.errors import register_error_handlers
from app.telemetry import TelemetryService


def create_app(
    settings: Settings | None = None,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="energy-load-management")
    app.state.settings = settings
    app.state.telemetry = TelemetryService(settings, now=now)
    app.state.dispatcher = LoadDispatcher(settings, now=now)
    app.include_router(router)
    register_error_handlers(app)

    @app.get("/")
    def read_root():
        return {"Hello": "World"}

    return app


app = create_app()
