"""事件存储（骨架）：内存索引 + JSONL 持久化，支持重启恢复与幂等。"""
from __future__ import annotations

from typing import Any


class EventStore:
    """已接受事件的追加式存储，重启后不重复计量、不丢失。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path

    def append(self, event: dict[str, Any]) -> bool:
        """追加一个事件；若幂等键已存在则返回 False。"""
        raise NotImplementedError

    def all_events(self) -> list[dict[str, Any]]:
        """返回全部已接受事件（按接受顺序）。"""
        raise NotImplementedError

    def close(self) -> None:
        """刷盘。"""
        raise NotImplementedError
