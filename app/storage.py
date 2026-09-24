"""事件存储：内存索引 + JSONL 追加持久化。

保证：
- 幂等：同一 event_id 只会计量一次（append 返回 False 表示已存在）。
- 恢复：重启后从 JSONL 重放，已接受事件不丢失、不重复计量。
- 并发：内部锁保证读写互斥，冲突写只有一个确定结果。
- 兼容：事件中的未知字段原样保留，重放时不做schema裁剪。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from app.errors import StoreError


class EventStore:
    """已接受事件的追加式存储。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._fh = None
        if path:
            self._load(path)
            try:
                self._fh = open(path, "a", encoding="utf-8")
            except OSError as exc:
                raise StoreError("store unavailable") from exc

    def _load(self, path: str) -> None:
        """启动时重放历史；坏行不污染整体状态，跳过并继续。"""
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # 截断/损坏行：跳过，不影响其余事件
                    self._index(event)
        except OSError as exc:
            raise StoreError("store unavailable") from exc

    def _index(self, event: dict[str, Any]) -> bool:
        eid = event.get("event_id")
        if eid is None or eid in self._by_id:
            return False
        self._by_id[eid] = event
        self._events.append(event)
        return True

    def append(self, event: dict[str, Any]) -> bool:
        """追加事件；幂等键已存在时返回 False 且状态不变。"""
        with self._lock:
            if event.get("event_id") in self._by_id:
                return False
            if self._fh is not None:
                try:
                    self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                    self._fh.flush()
                    os.fsync(self._fh.fileno())
                except OSError as exc:
                    # 部分失败不污染内存状态
                    raise StoreError("store unavailable") from exc
            self._index(event)
            return True

    def contains(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._by_id

    def all_events(self) -> list[dict[str, Any]]:
        """全部已接受事件（按接受顺序的副本）。"""
        with self._lock:
            return list(self._events)

    def count(self) -> int:
        with self._lock:
            return len(self._events)

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
