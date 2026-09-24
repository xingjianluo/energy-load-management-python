from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.telemetry import TelemetryService
from main import create_app

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("elm.test")

FIXED_NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def now() -> datetime:
    return FIXED_NOW


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(store_path=str(tmp_path / "events.jsonl"))


@pytest.fixture()
def svc(settings) -> TelemetryService:
    clock = lambda: FIXED_NOW  # noqa: E731
    service = TelemetryService(settings, now=clock)
    service.register_device("load1", "load")
    service.register_device("gen1", "generator")
    service.register_device("bat1", "storage")
    return service


@pytest.fixture()
def client(settings):
    app = create_app(settings, now=lambda: FIXED_NOW)
    with TestClient(app) as c:
        yield c


def make_payload(event_id: str, *, device="load1", metric="power", value=5.0,
                 unit="kW", ts: datetime | None = None, **extra):
    p = {
        "event_id": event_id,
        "device_id": device,
        "metric": metric,
        "value": value,
        "unit": unit,
        "timestamp": (ts or FIXED_NOW).isoformat(),
    }
    p.update(extra)
    return p


def log_case(title: str, raw, result) -> None:
    """单测日志：打印原始输入与处理结果/判定依据。"""
    logger.info("CASE %s\n  input=%s\n  result=%s", title, raw, result)
