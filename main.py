from fastapi import FastAPI

from app.api import router
from app.config import Settings, load_settings
from app.dispatch import LoadDispatcher
from app.errors import register_error_handlers
from app.telemetry import TelemetryService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="energy-load-management")
    app.state.settings = settings
    app.state.telemetry = TelemetryService(settings)
    app.state.dispatcher = LoadDispatcher(settings)
    app.include_router(router)
    register_error_handlers(app)
    return app


app = create_app()


@app.get("/")
def read_root():
    return {"Hello": "World"}
