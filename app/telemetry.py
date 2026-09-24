"""遥测接入：校验、幂等去重、乱序/过期/未来时间戳判定、设备注册表。

校验顺序固定，保证结果稳定且可区分：
1. 结构/必填字段  2. 字段类型  3. 未知设备  4. 未知指标
5. 非法单位  6. 未来时间戳  7. 过期时间戳  8. 乱序  9. 幂等去重
去重在任何状态变更之前完成：重复提交同一批数据不会改变状态或历史口径。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from typing import Any, Callable

from app.config import Settings
from app.errors import ConflictError
from app.models import (
    KNOWN_METRICS,
    REQUIRED_FIELDS,
    UNIT_SCALE,
    BatchIngestResponse,
    DeviceSnapshot,
    IngestResult,
    IngestStatus,
    RejectReason,
    SystemSnapshot,
)
from app.storage import EventStore

logger = logging.getLogger("elm.telemetry")

DEVICE_KINDS = ("load", "generator", "storage", "meter")


def _parse_timestamp(raw: Any) -> datetime | None:
    """接受 ISO 8601 字符串或 epoch 秒；非法返回 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    return None


class TelemetryService:
    """线程安全的遥测服务。"""

    def __init__(
        self,
        settings: Settings,
        store: EventStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store or EventStore(settings.store_path)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._devices: dict[str, str] = {}  # device_id -> kind
        self._latest: dict[tuple[str, str], tuple[datetime, float]] = {}
        self._registry_path = (
            settings.store_path + ".devices.json" if settings.store_path else None
        )
        self._load_registry()
        for d in settings.known_devices:
            self._devices.setdefault(d, "meter")
        # 重放历史，重建最新值索引（不重复计量：仅更新内存视图）
        for ev in self._store.all_events():
            self._apply_to_view(ev)

    # ---------- 设备注册表 ----------

    def _load_registry(self) -> None:
        if self._registry_path and os.path.exists(self._registry_path):
            try:
                with open(self._registry_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self._devices.update({str(k): str(v) for k, v in data.items()})
            except (OSError, json.JSONDecodeError):
                logger.warning("device registry corrupted, starting empty")

    def _save_registry(self) -> None:
        if not self._registry_path:
            return
        tmp = self._registry_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._devices, fh)
        os.replace(tmp, self._registry_path)

    def register_device(self, device_id: str, kind: str) -> dict[str, str]:
        """注册设备（幂等）；同 ID 不同 kind 冲突时报 409。"""
        if kind not in DEVICE_KINDS:
            raise ConflictError(f"unknown device kind: {kind}")
        with self._lock:
            existing = self._devices.get(device_id)
            if existing is not None:
                if existing != kind:
                    raise ConflictError(
                        f"device {device_id} already registered as {existing}"
                    )
                return {"device_id": device_id, "kind": existing}
            self._devices[device_id] = kind
            self._save_registry()
            return {"device_id": device_id, "kind": kind}

    def list_devices(self) -> dict[str, str]:
        with self._lock:
            return dict(self._devices)

    # ---------- 遥测接入 ----------

    def ingest(self, payload: Any) -> IngestResult:
        """处理单条遥测；任何情况下都返回稳定可区分的结果。"""
        with self._lock:
            return self._ingest_locked(payload)

    def _reject(self, reason: RejectReason, detail: str) -> IngestResult:
        logger.info("ingest rejected reason=%s detail=%s", reason, detail)
        return IngestResult(status=IngestStatus.REJECTED, reason=reason, detail=detail)

    def _ingest_locked(self, payload: Any) -> IngestResult:
        if not isinstance(payload, dict):
            return self._reject(RejectReason.MISSING_FIELD, "payload must be an object")

        # 幂等去重最先做：重复提交结果稳定为 duplicate，且不触碰任何状态
        event_id = payload.get("event_id")
        if isinstance(event_id, str) and self._store.contains(event_id):
            return IngestResult(status=IngestStatus.DUPLICATE, event_id=event_id)

        missing = [f for f in REQUIRED_FIELDS if f not in payload or payload[f] is None]
        if missing:
            return self._reject(
                RejectReason.MISSING_FIELD, f"missing fields: {','.join(missing)}"
            )

        device_id, metric, unit = (
            payload["device_id"], payload["metric"], payload["unit"],
        )
        value, raw_ts = payload["value"], payload["timestamp"]
        if not isinstance(event_id, str) or not event_id:
            return self._reject(RejectReason.INVALID_VALUE, "event_id must be a string")
        if not isinstance(device_id, str) or not device_id:
            return self._reject(RejectReason.INVALID_VALUE, "device_id must be a string")
        if not isinstance(metric, str) or not metric:
            return self._reject(RejectReason.INVALID_VALUE, "metric must be a string")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return self._reject(RejectReason.INVALID_VALUE, "value must be a number")
        if not isinstance(unit, str):
            return self._reject(RejectReason.INVALID_UNIT, "unit must be a string")

        if device_id not in self._devices:
            return self._reject(
                RejectReason.UNKNOWN_DEVICE, f"device not registered: {device_id}"
            )
        if metric not in KNOWN_METRICS:
            return self._reject(
                RejectReason.UNKNOWN_METRIC, f"unknown metric: {metric}"
            )
        if unit not in KNOWN_METRICS[metric]:
            return self._reject(
                RejectReason.INVALID_UNIT,
                f"unit {unit} not valid for metric {metric}",
            )

        ts = _parse_timestamp(raw_ts)
        if ts is None:
            return self._reject(RejectReason.INVALID_VALUE, "unparseable timestamp")

        now = self._now()
        skew = (ts - now).total_seconds()
        if skew > self._settings.max_future_skew_seconds:
            return self._reject(
                RejectReason.FUTURE_TIMESTAMP, f"timestamp {skew:.0f}s in the future"
            )
        age = (now - ts).total_seconds()
        if age > self._settings.max_stale_seconds:
            return self._reject(
                RejectReason.STALE_TIMESTAMP, f"timestamp {age:.0f}s old"
            )

        key = (device_id, metric)
        latest = self._latest.get(key)
        if latest is not None and ts <= latest[0]:
            return self._reject(
                RejectReason.OUT_OF_ORDER,
                f"timestamp not after latest accepted for {device_id}/{metric}",
            )

        base_unit, scale = UNIT_SCALE[unit]
        normalized = float(value) * scale
        event = {
            "event_id": event_id,
            "device_id": device_id,
            "metric": metric,
            "value": normalized,
            "unit": base_unit,
            "raw_value": value,
            "raw_unit": unit,
            "timestamp": ts.isoformat(),
            "accepted_at": now.isoformat(),
        }
        # 未知扩展字段原样保留（前向兼容）
        for k, v in payload.items():
            if k not in event and k not in ("raw_value", "raw_unit"):
                event.setdefault(k, v)

        if not self._store.append(event):
            return IngestResult(status=IngestStatus.DUPLICATE, event_id=event_id)
        self._apply_to_view(event)
        logger.info(
            "ingest accepted event=%s device=%s metric=%s value=%s%s",
            event_id, device_id, metric, normalized, base_unit,
        )
        return IngestResult(status=IngestStatus.ACCEPTED, event_id=event_id)

    def ingest_batch(self, payloads: list[Any]) -> BatchIngestResponse:
        """批量接入；每条独立判定，重复提交整批结果一致。"""
        results = [self.ingest(p) for p in payloads]
        return BatchIngestResponse(
            results=results,
            accepted=sum(r.status == IngestStatus.ACCEPTED for r in results),
            duplicates=sum(r.status == IngestStatus.DUPLICATE for r in results),
            rejected=sum(r.status == IngestStatus.REJECTED for r in results),
        )

    # ---------- 状态视图 ----------

    def _apply_to_view(self, event: dict[str, Any]) -> None:
        key = (event["device_id"], event["metric"])
        ts = datetime.fromisoformat(event["timestamp"])
        latest = self._latest.get(key)
        if latest is None or ts > latest[0]:
            self._latest[key] = (ts, float(event["value"]))

    def snapshot(self) -> SystemSnapshot:
        """系统实时快照：调度判定的输入。"""
        with self._lock:
            devices: dict[str, DeviceSnapshot] = {}
            load_kw = generation_kw = 0.0
            socs: list[float] = []
            for (device_id, metric), (ts, val) in self._latest.items():
                snap = devices.setdefault(
                    device_id, DeviceSnapshot(device_id=device_id, last_seen=ts)
                )
                snap.metrics[metric] = val
                if snap.last_seen is None or ts > snap.last_seen:
                    snap.last_seen = ts
                kind = self._devices.get(device_id)
                if metric == "power" and kind == "load":
                    load_kw += val
                elif metric == "power" and kind == "generator":
                    generation_kw += val
                elif metric == "soc" and kind == "storage":
                    socs.append(val)
            return SystemSnapshot(
                load_kw=load_kw,
                generation_kw=generation_kw,
                storage_soc=min(socs) if socs else 1.0,
                net_load_kw=load_kw - generation_kw,
                devices=devices,
            )

    def history(self, device_id: str | None = None) -> list[dict[str, Any]]:
        """已接受事件历史（只含 accepted，口径与重启后一致）。"""
        events = self._store.all_events()
        if device_id is not None:
            events = [e for e in events if e.get("device_id") == device_id]
        return events
