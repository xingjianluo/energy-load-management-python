"""只追加（append-only）事件日志。

持久化策略：
- 每个已接受事件写一行 JSON，并 flush+fsync 后才算"已接受"；
- 启动时按 seq 顺序重放；若最后一行在崩溃中被截断（无法解析），
  则把文件截回最后一个完整行——已确认事件不丢、未确认事件不算数，
  因此不会重复计量；
- 内部一把 RLock，append/读取互斥；门面服务可用同一把锁包裹更大临界区。
"""

import json
import os
import threading
from datetime import datetime

from energy.clock import Clock
from energy.config import Settings
from energy.errors import StorageAppError
from energy.models import Event, EventKind

_DT_FIELDS = ("received_at", "occurred_at")


def _dump(event: Event) -> str:
    record = {
        "seq": event.seq,
        "event_id": event.event_id,
        "kind": event.kind.value,
        "received_at": event.received_at.isoformat(),
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "device_id": event.device_id,
        "metric": event.metric,
        "value": event.value,
        "unit": event.unit,
        "dedupe_key": event.dedupe_key,
        "payload": event.payload,
    }
    return json.dumps(record, ensure_ascii=False, sort_keys=True)


def _load(line: str) -> Event:
    record = json.loads(line)
    kwargs = {}
    for f in _DT_FIELDS:
        raw = record.get(f)
        kwargs[f] = datetime.fromisoformat(raw) if raw else None
    return Event(
        seq=record["seq"],
        event_id=record["event_id"],
        kind=EventKind(record["kind"]),
        device_id=record.get("device_id"),
        metric=record.get("metric"),
        value=record.get("value"),
        unit=record.get("unit"),
        dedupe_key=record.get("dedupe_key"),
        payload=record.get("payload", {}),
        **kwargs,
    )


class EventStore:
    def __init__(self, settings: Settings, clock: Clock) -> None:
        self._settings = settings
        self._clock = clock
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._next_seq = 1
        self._path = os.path.join(settings.data_dir, settings.event_log_file)
        os.makedirs(settings.data_dir, exist_ok=True)
        self._recover()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def path(self) -> str:
        return self._path

    def _recover(self) -> None:
        """读取日志，截断崩溃遗留的不完整尾行。"""
        if not os.path.exists(self._path):
            return
        good: list[Event] = []
        good_lines: list[str] = []
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    event = _load(line)
                except (ValueError, KeyError):
                    # 损坏行只可能出现在尾部（只追加+行级 fsync）
                    break
                good.append(event)
                good_lines.append(line if line.endswith("\n") else line + "\n")
        for event in good:
            if event.seq >= self._next_seq:
                self._next_seq = event.seq + 1
        self._events = good
        # 回写干净前缀（修复 torn tail）
        with open(self._path, "w", encoding="utf-8") as fh:
            fh.writelines(good_lines)
            fh.flush()
            os.fsync(fh.fileno())

    def append_new(
        self,
        *,
        event_id: str,
        kind: EventKind,
        occurred_at: datetime | None,
        device_id: str | None,
        metric: str | None,
        value: float | None,
        unit: str | None,
        dedupe_key: str | None,
        payload: dict,
    ) -> Event:
        """分配 seq 并持久化一个新事件。"""
        event = Event(
            seq=-1,
            event_id=event_id,
            kind=kind,
            received_at=self._clock.now(),
            occurred_at=occurred_at,
            device_id=device_id,
            metric=metric,
            value=value,
            unit=unit,
            dedupe_key=dedupe_key,
            payload=payload,
        )
        with self._lock:
            seq = self._next_seq
            event = Event(
                seq=seq,
                event_id=event.event_id or f"evt-seq-{seq}",
                kind=event.kind,
                received_at=event.received_at,
                occurred_at=event.occurred_at,
                device_id=event.device_id,
                metric=event.metric,
                value=event.value,
                unit=event.unit,
                dedupe_key=event.dedupe_key,
                payload=event.payload,
            )
            try:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(_dump(event) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                raise StorageAppError(f"failed to persist event: {exc}") from exc
            self._events.append(event)
            self._next_seq += 1
            return event

    def peek_next_seq(self) -> int:
        """查看下一个将分配的 seq（不分配）。调用方须持锁。"""
        return self._next_seq

    def all_events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            for event in self._events:
                if event.event_id == event_id:
                    return event
        return None
