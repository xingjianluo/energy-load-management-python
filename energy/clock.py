"""可注入的时间源，便于测试控制"当前时间"。"""

from datetime import UTC, datetime
from typing import Callable


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        # 统一使用时区感知 UTC，避免朴素时间戳比较歧义
        return datetime.now(UTC)


class FakeClock(Clock):
    """测试用可控时钟。"""

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 9, 24, 0, 0, 0, tzinfo=UTC)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        self._now = value
