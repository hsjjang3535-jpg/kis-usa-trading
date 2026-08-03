"""미국 정규장 시간 (America/New_York 기준, 서머타임 자동)."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

# 정규장 09:30~16:00 ET
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


def now_ny() -> datetime:
    return datetime.now(NY)


def now_kst() -> datetime:
    return datetime.now(KST)


def is_weekday_ny(dt: datetime | None = None) -> bool:
    d = dt or now_ny()
    return d.weekday() < 5


def is_us_regular_session(dt: datetime | None = None) -> bool:
    """미국 정규장 여부 (공휴일은 미반영 — 필요 시 목록 추가)."""
    d = dt or now_ny()
    if d.weekday() >= 5:
        return False
    t = d.time()
    return REGULAR_OPEN <= t <= REGULAR_CLOSE


def session_label() -> str:
    d = now_ny()
    if not is_weekday_ny(d):
        return "휴장(주말)"
    if is_us_regular_session(d):
        return "정규장"
    t = d.time()
    if t < REGULAR_OPEN:
        return "장전"
    return "장후"
