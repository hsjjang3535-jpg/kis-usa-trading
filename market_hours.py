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


def minutes_since_open(dt: datetime | None = None) -> float:
    """정규장 개장(09:30 ET) 이후 경과 분. 장전이면 음수."""
    d = dt or now_ny()
    open_dt = d.replace(hour=9, minute=30, second=0, microsecond=0)
    return (d - open_dt).total_seconds() / 60.0


def session_elapsed_fraction(dt: datetime | None = None) -> float:
    """정규장 진행률 0~1 (RVOL 시간보정용). 장전이면 아주 작은 값."""
    m = minutes_since_open(dt)
    total = 6.5 * 60  # 09:30~16:00
    if m <= 0:
        return 1 / 60  # 장전 보정 최소치
    return min(max(m / total, 1 / 60), 1.0)


def is_orb_building(orb_minutes: int = 15, dt: datetime | None = None) -> bool:
    """오프닝 레인지 형성 구간."""
    if not is_us_regular_session(dt):
        return False
    m = minutes_since_open(dt)
    return 0 <= m < orb_minutes


def is_orb_entry_window(
    orb_minutes: int = 15,
    entry_until_minutes: int = 120,
    dt: datetime | None = None,
) -> bool:
    """ORB 돌파 진입 허용 구간 (레인지 확정 후 ~ entry_until)."""
    if not is_us_regular_session(dt):
        return False
    m = minutes_since_open(dt)
    return orb_minutes <= m <= entry_until_minutes


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
