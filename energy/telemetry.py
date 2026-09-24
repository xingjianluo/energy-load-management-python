"""遥测接入：逐点校验、幂等、乱序/重复判定。

语义（对同一批中的每个点独立给出稳定、可区分的结论）：
- accepted  : 通过全部校验并首次持久化；
- duplicate : dedupe_key 命中历史已接受点（重复提交不改状态、不重复计量）；
- rejected  : 因明确原因拒绝（未知设备/指标/单位/缺字段/数值非法/
              时间戳过期、未来、乱序）。

幂等键 dedupe_key：
- 优先使用客户端提供的 client_event_id；
- 否则由 (device_id, metric, value, unit, ts) 规范化哈希得到，
  因此同一点原样重复提交必然被识别为 duplicate。
"""

import hashlib
import json
import logging
import math
from datetime import UTC, datetime

from energy.clock import Clock
from energy.config import Settings
from energy.devices import DeviceRegistry
from energy.models import (
    BatchResultOut,
    EventKind,
    PointResultOut,
    PointStatus,
    RejectReason,
    TelemetryBatchIn,
    TelemetryPointIn,
)
from energy.store import EventStore

logger = logging.getLogger("energy.telemetry")


def parse_timestamp(raw: str) -> datetime:
    """解析 RFC3339/ISO8601 时间戳，统一返回时区感知 UTC 时间。"""
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        # 朴素时间戳不接受，避免服务端时区猜测造成口径歧义
        raise ValueError("timestamp must include timezone offset")
    return dt.astimezone(UTC)


def canonical_dedupe_key(point: TelemetryPointIn) -> str:
    basis = json.dumps(
        [point.device_id, point.metric, point.value, point.unit, point.ts],
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return "pt-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def event_id_for(dedupe_key: str) -> str:
    return "evt-" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:32]


class TelemetryService:
    def __init__(
        self,
        registry: DeviceRegistry,
        store: EventStore,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._registry = registry
        self._store = store
        self._clock = clock
        self._settings = settings
        # (device_id, metric) -> 最近接受点的事件时间；重放历史得到
        self._latest_ts: dict[tuple[str, str], datetime] = {}
        self._dedupe_index: dict[str, str] = {}  # dedupe_key -> event_id
        for event in store.all_events():
            if (
                event.kind is EventKind.TELEMETRY
                and event.device_id
                and event.metric
                and event.occurred_at is not None
            ):
                key = (event.device_id, event.metric)
                prev = self._latest_ts.get(key)
                if prev is None or event.occurred_at > prev:
                    self._latest_ts[key] = event.occurred_at
            if event.dedupe_key:
                self._dedupe_index[event.dedupe_key] = event.event_id

    # ------------------------------------------------------------------
    def _validate_point(
        self, point: TelemetryPointIn
    ) -> tuple[datetime | None, str | None]:
        """返回 (ts, reject_reason)；reject_reason 非空即拒绝。"""
        # 缺失/空字符串字段（None 已被 pydantic 结构层拦截，此处防御兜底）
        for field_name in ("device_id", "metric", "unit", "ts"):
            value = getattr(point, field_name)
            if not isinstance(value, str) or not value.strip():
                return None, RejectReason.MISSING_FIELD.value
        # 数值：布尔不算数；NaN/Inf 不接受
        value = point.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, RejectReason.MALFORMED_VALUE.value
        fvalue = float(value)
        if math.isnan(fvalue) or math.isinf(fvalue):
            return None, RejectReason.MALFORMED_VALUE.value

        spec = self._registry.get(point.device_id)
        if spec is None:
            return None, RejectReason.UNKNOWN_DEVICE.value
        metric_spec = spec.metrics.get(point.metric)
        if metric_spec is None:
            return None, RejectReason.UNKNOWN_METRIC.value
        if point.unit != metric_spec.unit:
            return None, RejectReason.ILLEGAL_UNIT.value
        if not (metric_spec.min_value <= fvalue <= metric_spec.max_value):
            return None, RejectReason.VALUE_OUT_OF_RANGE.value

        # 时间戳：格式 -> 过期 -> 未来 -> 乱序
        try:
            ts = parse_timestamp(point.ts)
        except (ValueError, TypeError):
            return None, RejectReason.BAD_TIMESTAMP.value
        now = self._clock.now()
        age_seconds = (now - ts).total_seconds()
        if age_seconds > self._settings.past_tolerance_seconds:
            return None, RejectReason.STALE_TIMESTAMP.value
        if age_seconds < -self._settings.future_tolerance_seconds:
            return None, RejectReason.FUTURE_TIMESTAMP.value

        latest = self._latest_ts.get((point.device_id, point.metric))
        if latest is not None and ts < latest:
            return None, RejectReason.OUT_OF_ORDER.value
        return ts, None

    # ------------------------------------------------------------------
    def ingest_batch(self, batch: TelemetryBatchIn) -> BatchResultOut:
        """处理一个遥测批次；调用方需持有 store.lock（保证并发串行化）。"""
        results: list[PointResultOut] = []
        accepted = duplicate = rejected = 0

        logger.info(
            "ingest batch start batch_id=%s size=%d", batch.batch_id, len(batch.points)
        )

        for index, point in enumerate(batch.points):
            logger.info(
                "raw input [%d] device=%s metric=%s value=%s unit=%s ts=%s client_event_id=%s",
                index,
                point.device_id,
                point.metric,
                point.value,
                point.unit,
                point.ts,
                point.client_event_id,
            )

            ts, reason = self._validate_point(point)
            if reason is not None:
                rejected += 1
                logger.info(
                    "decision [%d] REJECT reason=%s basis=registry/timestamp-policy",
                    index,
                    reason,
                )
                results.append(
                    PointResultOut(
                        index=index, status=PointStatus.REJECTED, reason=reason
                    )
                )
                continue

            assert ts is not None
            dedupe_key = point.client_event_id or canonical_dedupe_key(point)

            existing_id = self._dedupe_index.get(dedupe_key)
            if existing_id is not None:
                duplicate += 1
                logger.info(
                    "decision [%d] DUPLICATE dedupe_key=%s existing_event=%s state=unchanged",
                    index,
                    dedupe_key,
                    existing_id,
                )
                results.append(
                    PointResultOut(
                        index=index,
                        status=PointStatus.DUPLICATE,
                        reason="duplicate",
                        event_id=existing_id,
                    )
                )
                continue

            event = self._store.append_new(
                event_id=event_id_for(dedupe_key),
                kind=EventKind.TELEMETRY,
                occurred_at=ts,
                device_id=point.device_id,
                metric=point.metric,
                value=float(point.value),
                unit=point.unit,
                dedupe_key=dedupe_key,
                payload={"batch_id": batch.batch_id},
            )
            self._dedupe_index[dedupe_key] = event.event_id
            key = (point.device_id, point.metric)
            prev = self._latest_ts.get(key)
            if prev is None or ts >= prev:
                self._latest_ts[key] = ts
            accepted += 1
            logger.info(
                "decision [%d] ACCEPTED event_id=%s seq=%d latest_ts(%s,%s)=%s (was %s)",
                index,
                event.event_id,
                event.seq,
                point.device_id,
                point.metric,
                ts.isoformat(),
                prev.isoformat() if prev else None,
            )
            results.append(
                PointResultOut(
                    index=index,
                    status=PointStatus.ACCEPTED,
                    event_id=event.event_id,
                )
            )

        out = BatchResultOut(
            batch_id=batch.batch_id,
            accepted=accepted,
            duplicate=duplicate,
            rejected=rejected,
            results=results,
        )
        logger.info(
            "ingest batch done accepted=%d duplicate=%d rejected=%d",
            accepted,
            duplicate,
            rejected,
        )
        return out
