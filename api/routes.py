"""HTTP 路由。"""

from fastapi import APIRouter, Depends, Query

from energy.models import (
    BatchResultOut,
    CommandIn,
    TelemetryBatchIn,
)
from energy.service import EnergyService, get_service
from api.serialization import (
    command_result_out,
    decision_out,
    device_out,
    event_out,
    snapshot_out,
)

router = APIRouter(prefix="/api/v1", tags=["energy"])


def service() -> EnergyService:
    return get_service()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/telemetry", response_model=BatchResultOut)
def ingest_telemetry(
    body: TelemetryBatchIn, svc: EnergyService = Depends(service)
) -> BatchResultOut:
    return svc.ingest_batch(body)


@router.get("/state")
def get_state(svc: EnergyService = Depends(service)) -> dict:
    return snapshot_out(svc.current_snapshot())


@router.post("/commands")
def submit_command(
    body: CommandIn, svc: EnergyService = Depends(service)
) -> dict:
    return command_result_out(svc.submit_command(body))


@router.get("/decisions")
def list_decisions(svc: EnergyService = Depends(service)) -> dict:
    return {"decisions": [decision_out(d) for d in svc.list_decisions()]}


@router.get("/events")
def list_events(
    device_id: str | None = Query(default=None),
    metric: str | None = Query(default=None),
    svc: EnergyService = Depends(service),
) -> dict:
    return {
        "events": [
            event_out(e) for e in svc.list_events(device_id=device_id, metric=metric)
        ]
    }


@router.get("/devices")
def list_devices(svc: EnergyService = Depends(service)) -> dict:
    return {"devices": [device_out(d) for d in svc.registry().list_devices()]}
