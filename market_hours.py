"""미국 정규장 시간 (America/New_York 기준, 서머타임 자동).

KST 자정과 NY 세션일은 어긋난다.
  금 22:30 KST = NY 금 09:30 개장
  토 00:00 KST = NY 금 11:00 (아직 정규장)
  토 05:00 KST = NY 금 16:00 마감
  토 22:00 KST = NY 토 09:00 주말 휴장
  월 22:30 KST = NY 월 09:30 다음 개장
주말 여부는 반드시 NY 달력 요일로 판정한다 (KST 토요일이어도 금 세션은 유지).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
_WD = "월화수목금토일"

# 정규장 09:30~16:00 ET
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


def now_ny() -> datetime:
    return datetime.now(NY)


def now_kst() -> datetime:
    return datetime.now(KST)


def _as_ny(dt: datetime | None = None) -> datetime:
    """어떤 tz/naive가 들어와도 America/New_York 벽시계로 변환."""
    d = dt if dt is not None else now_ny()
    if d.tzinfo is None:
        d = d.replace(tzinfo=NY)
    return d.astimezone(NY)


def ny_weekday_label(dt: datetime | None = None) -> str:
    d = _as_ny(dt)
    return f"{d.strftime('%m/%d')}({_WD[d.weekday()]})"


def is_weekday_ny(dt: datetime | None = None) -> bool:
    return _as_ny(dt).weekday() < 5


def is_us_regular_session(dt: datetime | None = None) -> bool:
    """미국 정규장 여부 (공휴일은 미반영 — 필요 시 목록 추가)."""
    d = _as_ny(dt)
    if d.weekday() >= 5:
        return False
    t = d.time()
    return REGULAR_OPEN <= t <= REGULAR_CLOSE


def minutes_since_open(dt: datetime | None = None) -> float:
    """정규장 개장(09:30 ET) 이후 경과 분. 장전이면 음수."""
    d = _as_ny(dt)
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


def session_label(dt: datetime | None = None) -> str:
    d = _as_ny(dt)
    tag = f"NY {ny_weekday_label(d)}"
    if d.weekday() >= 5:
        return f"휴장(주말 · {tag})"
    if is_us_regular_session(d):
        return f"정규장 ({tag})"
    t = d.time()
    if t < REGULAR_OPEN:
        return f"장전 ({tag})"
    return f"장후 ({tag})"


def trading_day_ny(dt: datetime | None = None) -> str:
    """미국 세션일 (America/New_York 달력일). KST 자정과 무관."""
    return _as_ny(dt).strftime("%Y-%m-%d")


def last_completed_session_day(dt: datetime | None = None) -> str:
    """가장 최근 정규장이 끝난 NY 세션일.

    금 16:00 이후·주말 → 그 주 금요일. 월 장전 → 이전 금요일.
    """
    d = _as_ny(dt)
    wd = d.weekday()
    if wd >= 5:
        d = d - timedelta(days=wd - 4)
        return d.strftime("%Y-%m-%d")
    if d.time() >= REGULAR_CLOSE:
        return d.strftime("%Y-%m-%d")
    d = d - timedelta(days=3 if wd == 0 else 1)
    return d.strftime("%Y-%m-%d")
