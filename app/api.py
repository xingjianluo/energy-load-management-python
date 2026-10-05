"""HTTP 接口：遥测接入、快照查询、调度评估与命令。"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel

from app.dispatch import LoadDispatcher
from app.errors import ValidationError
from app.models import BatchIngestResponse, IngestResult
from app.telemetry import TelemetryService

router = APIRouter()


def _telemetry(request: Request) -> TelemetryService:
    return request.app.state.telemetry


def _dispatcher(request: Request) -> LoadDispatcher:
    return request.app.state.dispatcher


class DeviceRegistration(BaseModel):
    device_id: str
    kind: str  # load | generator | storage | meter


class DispatchCommand(BaseModel):
    command: str  # shed | restore


class LoadRegistration(BaseModel):
    device_id: str
    priority: int  # 数值越大越重要（越晚削减、越早恢复）
    sheddable_kw: float  # 可削减容量（kW，> 0）


@router.post("/devices", status_code=201)
def register_device(body: DeviceRegistration, request: Request) -> dict:
    return _telemetry(request).register_device(body.device_id, body.kind)


@router.get("/devices")
def list_devices(request: Request) -> dict:
    return _telemetry(request).list_devices()


@router.post("/telemetry", response_model=BatchIngestResponse)
def ingest_telemetry(request: Request, payload: Annotated[Any, Body()]) -> BatchIngestResponse:
    """接入单条或批量遥测；每条独立判定，重复提交结果一致。"""
    svc = _telemetry(request)
    if isinstance(payload, list):
        return svc.ingest_batch(payload)
    if isinstance(payload, dict):
        return svc.ingest_batch([payload])
    raise ValidationError("body must be an object or a list of objects")


@router.get("/telemetry/history")
def telemetry_history(request: Request, device_id: str | None = None) -> list[dict]:
    return _telemetry(request).history(device_id)


@router.get("/snapshot")
def system_snapshot(request: Request):
    return _telemetry(request).snapshot()


@router.get("/dispatch/status")
def dispatch_status(request: Request) -> dict:
    """当前模式、削减档位与生效中的削减明细（mode 字段保持兼容）。"""
    return _dispatcher(request).status()


@router.post("/dispatch/loads", status_code=201)
def register_load(body: LoadRegistration, request: Request) -> dict:
    """注册/更新可控负载的优先级与可削减容量。"""
    try:
        return _dispatcher(request).register_load(
            body.device_id, body.priority, body.sheddable_kw
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


@router.get("/dispatch/loads")
def list_loads(request: Request) -> list[dict]:
    """全部可控负载配置（按削减顺序）。"""
    return _dispatcher(request).list_loads()


@router.post("/dispatch/evaluate")
def dispatch_evaluate(request: Request):
    """用当前快照评估并应用一次调度决策。"""
    svc, disp = _telemetry(request), _dispatcher(request)
    return disp.evaluate(svc.snapshot())


@router.post("/dispatch/command")
def dispatch_command(body: DispatchCommand, request: Request):
    svc, disp = _telemetry(request), _dispatcher(request)
    try:
        return disp.command(body.command, svc.snapshot())
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


@router.get("/dispatch/decisions")
def dispatch_decisions(request: Request) -> list[dict]:
    return [d.model_dump(mode="json") for d in _dispatcher(request).decisions()]
