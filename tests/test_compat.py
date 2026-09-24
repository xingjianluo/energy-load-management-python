"""前向兼容与错误归一化测试。

- 请求中出现未知的额外字段：不影响既有字段处理，既有查询结构不变；
- 未知的新"指标"仍按安全策略拒绝（不静默入库），但不会破坏其他点或查询；
- 内部异常归一化为 internal_error，不泄露实现细节。
"""

import pytest
from fastapi.testclient import TestClient

import main
from energy.config import Settings
from energy.errors import AppError, ErrorCode
from energy.service import EnergyService
from tests.conftest import iso


@pytest.fixture
def client(data_dir, clock, monkeypatch):
    import energy.service as es

    svc = EnergyService(settings=Settings(data_dir=data_dir), clock=clock)
    monkeypatch.setattr(es, "_service", svc)
    return TestClient(main.app), svc


def test_extra_fields_do_not_break_acceptance_or_queries(client):
    c, svc = client
    r = c.post("/api/v1/telemetry", json={
        "points": [{
            "device_id": "load-001", "metric": "power_w", "value": 1000,
            "unit": "W", "ts": iso(svc._clock), "future_field": {"nested": 1},
        }],
        "future_top_level": "ignored",
    })
    assert r.status_code == 200
    assert r.json()["accepted"] == 1
    state = c.get("/api/v1/state").json()
    # 既有字段集合保持稳定
    assert set(state.keys()) == {
        "generated_at", "battery_soc", "solar_generation_w",
        "load_power_w", "net_load_w", "devices",
    }


def test_unknown_metric_rejected_but_batch_and_queries_unaffected(client):
    c, svc = client
    t = iso(svc._clock)
    r = c.post("/api/v1/telemetry", json={"points": [
        {"device_id": "load-001", "metric": "humidity", "value": 50,
         "unit": "%", "ts": t},
        {"device_id": "solar-001", "metric": "power_w", "value": 100,
         "unit": "W", "ts": t},
    ]})
    body = r.json()
    assert body["rejected"] == 1
    assert body["results"][0]["reason"] == "unknown_metric"
    assert body["accepted"] == 1
    assert c.get("/api/v1/state").status_code == 200


def test_internal_error_is_normalized_without_detail_leak(client, monkeypatch):
    c, svc = client

    def boom(_snap):
        raise RuntimeError("secret internal detail /etc/passwd")

    monkeypatch.setattr(svc._scheduler, "evaluate", boom)
    # 让 telemetry 接受点后触发调度内部异常
    with pytest.raises(AppError) as info:
        svc.ingest_batch(_batch(iso(svc._clock)))
    assert info.value.code is ErrorCode.INTERNAL_ERROR
    assert "secret" not in str(info.value.message)


def _batch(t):
    from energy.models import TelemetryBatchIn, TelemetryPointIn

    return TelemetryBatchIn(points=[
        TelemetryPointIn(device_id="load-001", metric="power_w",
                         value=1000, unit="W", ts=t),
    ])


def test_http_internal_error_shape(client, monkeypatch):
    c, svc = client

    def boom(_cmd, _snap):
        raise RuntimeError("boom internals")

    monkeypatch.setattr(svc._scheduler, "apply_command", boom)
    r = c.post("/api/v1/commands",
               json={"command_id": "x", "command_type": "curtail"})
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "internal_error"
    assert "boom" not in str(body)
