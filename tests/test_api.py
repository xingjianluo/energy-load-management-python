"""HTTP 接口测试：畸形输入、可区分错误、兼容行为、端到端往返。"""

import pytest
from fastapi.testclient import TestClient

import main
from energy.clock import FakeClock
from energy.config import Settings
from energy.service import EnergyService
from tests.conftest import iso


@pytest.fixture
def client(data_dir, clock, monkeypatch):
    import energy.service as es

    svc = EnergyService(settings=Settings(data_dir=data_dir), clock=clock)
    monkeypatch.setattr(es, "_service", svc)
    c = TestClient(main.app)
    c._svc = svc  # type: ignore[attr-defined]
    return c


def test_root_keeps_legacy_behavior(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"Hello": "World"}  # 既有行为兼容


def test_health_and_devices(client):
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    devices = client.get("/api/v1/devices").json()["devices"]
    assert {d["device_id"] for d in devices} >= {"battery-001", "load-001", "solar-001"}


def test_telemetry_endpoint_roundtrip(client):
    svc = client._svc
    t = iso(svc._clock)
    resp = client.post(
        "/api/v1/telemetry",
        json={
            "points": [
                {"device_id": "load-001", "metric": "power_w", "value": 6000,
                 "unit": "W", "ts": t},
                {"device_id": "solar-001", "metric": "power_w", "value": 0,
                 "unit": "W", "ts": t},
                {"device_id": "battery-001", "metric": "soc", "value": 0.9,
                 "unit": "fraction", "ts": t},
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 3 and body["rejected"] == 0
    state = client.get("/api/v1/state").json()
    assert state["net_load_w"] == 6000.0
    last = client.get("/api/v1/decisions").json()["decisions"][-1]
    assert last["curtailed"] is True
    assert last["input_snapshot"]["net_load_w"] == 6000.0


def test_unknown_device_returns_distinguishable_error(client):
    svc = client._svc
    resp = client.post(
        "/api/v1/telemetry",
        json={"points": [
            {"device_id": "ghost", "metric": "power_w", "value": 1,
             "unit": "W", "ts": iso(svc._clock)},
        ]},
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["reason"] == "unknown_device"


def test_structural_bad_json_normalized_to_validation_error(client):
    resp = client.post(
        "/api/v1/telemetry",
        content="{not-json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert isinstance(body["details"]["issues"], list)


def test_missing_required_field_normalized(client):
    resp = client.post("/api/v1/telemetry", json={"points": [{"device_id": "x"}]})
    assert resp.status_code == 422
    issues = resp.json()["details"]["issues"]
    fields = {i["field"] for i in issues}
    assert any("value" in f or "unit" in f or "metric" in f or "ts" in f for f in fields)


def test_empty_batch_rejected_with_batch_empty(client):
    resp = client.post("/api/v1/telemetry", json={"points": []})
    assert resp.status_code == 422
    assert resp.json()["error"] == "batch_empty"


def test_command_endpoint_and_duplicate(client):
    svc = client._svc
    # 先喂数据进入安全状态
    t = iso(svc._clock)
    client.post("/api/v1/telemetry", json={"points": [
        {"device_id": "load-001", "metric": "power_w", "value": 1000, "unit": "W", "ts": t},
        {"device_id": "solar-001", "metric": "power_w", "value": 0, "unit": "W", "ts": t},
        {"device_id": "battery-001", "metric": "soc", "value": 0.9, "unit": "fraction", "ts": t},
    ]})
    payload = {"command_id": "c-1", "command_type": "curtail"}
    r1 = client.post("/api/v1/commands", json=payload)
    r2 = client.post("/api/v1/commands", json=payload)
    assert r1.json()["status"] == "applied"
    assert r2.json()["status"] == "duplicate"
    assert r1.json()["decision_id"] == r2.json()["decision_id"]


def test_release_conflict_returns_409_shape(client):
    svc = client._svc
    t = iso(svc._clock)
    client.post("/api/v1/telemetry", json={"points": [
        {"device_id": "load-001", "metric": "power_w", "value": 6000, "unit": "W", "ts": t},
        {"device_id": "solar-001", "metric": "power_w", "value": 0, "unit": "W", "ts": t},
        {"device_id": "battery-001", "metric": "soc", "value": 0.9, "unit": "fraction", "ts": t},
    ]})
    resp = client.post(
        "/api/v1/commands", json={"command_id": "r", "command_type": "release"}
    )
    assert resp.json()["status"] == "conflict"
    assert "trigger" in resp.json()["reason"]


def test_events_filtering(client):
    svc = client._svc
    t = iso(svc._clock)
    client.post("/api/v1/telemetry", json={"points": [
        {"device_id": "load-001", "metric": "power_w", "value": 1000, "unit": "W", "ts": t},
        {"device_id": "solar-001", "metric": "power_w", "value": 200, "unit": "W", "ts": t},
    ]})
    load_events = client.get("/api/v1/events?device_id=load-001").json()["events"]
    assert all(e["device_id"] == "load-001" for e in load_events)
    assert len(load_events) == 1
