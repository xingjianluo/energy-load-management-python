"""HTTP 接口：端到端流程、错误归一化、既有行为兼容。"""
from __future__ import annotations

from datetime import timedelta

from tests.conftest import FIXED_NOW, log_case, make_payload


def test_root_compatible(client):
    """既有对外行为保持兼容。"""
    r = client.get("/")
    assert r.status_code == 200 and r.json() == {"Hello": "World"}


def test_full_flow(client):
    assert client.post("/devices", json={"device_id": "load1", "kind": "load"}).status_code == 201
    assert client.post("/devices", json={"device_id": "bat1", "kind": "storage"}).status_code == 201

    p1 = make_payload("e1", value=11.0)
    p2 = make_payload("e2", device="bat1", metric="soc", value=90, unit="percent")
    r = client.post("/telemetry", json=[p1, p2])
    body = r.json()
    log_case("api-ingest", [p1, p2], body)
    assert r.status_code == 200 and body["accepted"] == 2

    # 重复提交同一批：全部 duplicate，历史不变
    r2 = client.post("/telemetry", json=[p1, p2]).json()
    assert r2["duplicates"] == 2 and r2["accepted"] == 0
    assert len(client.get("/telemetry/history").json()) == 2

    snap = client.get("/snapshot").json()
    log_case("api-snapshot", None, snap)
    assert snap["load_kw"] == 11.0 and abs(snap["storage_soc"] - 0.9) < 1e-9

    dec = client.post("/dispatch/evaluate").json()
    log_case("api-evaluate", snap, dec)
    assert dec["action"] == "shed" and dec["mode_after"] == "shedding"
    assert client.get("/dispatch/status").json()["mode"] == "shedding"
    assert len(client.get("/dispatch/decisions").json()) == 1


def test_single_object_ingest(client):
    client.post("/devices", json={"device_id": "load1", "kind": "load"})
    r = client.post("/telemetry", json=make_payload("e1"))
    assert r.status_code == 200 and r.json()["accepted"] == 1


def test_malformed_body_normalized_error(client):
    r = client.post("/telemetry", json="garbage")
    log_case("malformed-body", "garbage", r.json())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"  # 归一化、不泄露细节


def test_invalid_command_normalized_error(client):
    r = client.post("/dispatch/command", json={"command": "explode"})
    log_case("bad-command", "explode", r.json())
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_device_conflict(client):
    client.post("/devices", json={"device_id": "d", "kind": "load"})
    r = client.post("/devices", json={"device_id": "d", "kind": "storage"})
    log_case("device-conflict", "d as storage", r.json())
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
    # 幂等重注册同 kind 不报错
    assert client.post("/devices", json={"device_id": "d", "kind": "load"}).status_code == 201


def test_unknown_fields_do_not_break_queries(client):
    client.post("/devices", json={"device_id": "load1", "kind": "load"})
    p = make_payload("e1", value=2.0, new_field_x={"nested": [1, 2]})
    assert client.post("/telemetry", json=p).json()["accepted"] == 1
    history = client.get("/telemetry/history").json()
    log_case("unknown-field-history", p, history)
    assert history[0]["new_field_x"] == {"nested": [1, 2]}
    assert client.get("/snapshot").status_code == 200


def test_rejected_telemetry_visible_in_response(client):
    p = make_payload("e1", device="ghost")
    r = client.post("/telemetry", json=[p]).json()
    log_case("api-reject", [p], r)
    assert r["rejected"] == 1
    assert r["results"][0]["reason"] == "unknown_device"


def test_tiered_dispatch_endpoints(settings):
    """分级模式下的状态/计划查询；旧字段保持兼容。"""
    from fastapi.testclient import TestClient

    from app.config import Settings
    from main import create_app

    tiered = Settings(
        **{**settings.__dict__,
           "controllable_loads": ("load1:1:3",),
           "shed_target_margin_kw": 0.5}
    )
    from tests.conftest import FIXED_NOW as now
    with TestClient(create_app(tiered, now=lambda: now)) as client:
        client.post("/devices", json={"device_id": "load1", "kind": "load"})
        client.post("/telemetry", json=make_payload("e1", value=10.4))
        dec = client.post("/dispatch/evaluate").json()
        log_case("api-tiered-evaluate", {"net": 10.4}, dec)
        assert dec["level_after"] == 1 and dec["plan"]["status"] == "ok"
        assert dec["plan"]["steps"] == [
            {"device_id": "load1", "priority": 1, "shed_kw": 3.0}
        ]
        status = client.get("/dispatch/status").json()
        log_case("api-tiered-status", None, status)
        assert status["mode"] == "shedding"  # 旧字段不变
        assert status["level"] == 1 and status["tiered"] is True
        assert status["shed_loads"][0]["device_id"] == "load1"
        plan = client.get("/dispatch/plan").json()
        assert plan["level"] == 1 and plan["total_shed_kw"] == 3.0
        # 决策历史结构兼容：旧字段齐全，新字段为增量
        rec = client.get("/dispatch/decisions").json()[-1]
        assert {"decision_id", "mode_before", "mode_after", "action",
                "reasons", "snapshot", "thresholds"} <= rec.keys()
