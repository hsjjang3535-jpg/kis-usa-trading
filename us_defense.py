"""US 방어모드 — 실전 연속손실·세션/주간 한도 초과 시 실전 신규만 중단.

시뮬 진입·보유 청산은 그대로.
ENABLE_US_DEFENSE_MODE=false 이면 항상 통과.
날짜는 NY 세션일 기준.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import market_hours

NY = ZoneInfo("America/New_York")

ENABLED = os.getenv("ENABLE_US_DEFENSE_MODE", "true").lower() == "true"
MAX_CONSEC_LOSSES = int(os.getenv("US_DEFENSE_MAX_CONSEC_LOSSES", "2"))
SESSION_LOSS_LIMIT = float(os.getenv("US_DEFENSE_SESSION_LOSS_LIMIT", "-40"))
WEEKLY_LOSS_LIMIT = float(os.getenv("US_DEFENSE_WEEKLY_LOSS_LIMIT", "-80"))

_pause_until: str = ""  # YYYY-MM-DD NY inclusive
_pause_reason: str = ""
_notified_key: str = ""
# 세션별 실전 손익 장부 (주간 합산용)
_session_ledger: list[dict] = []


def is_enabled() -> bool:
    return ENABLED


def dump_state() -> dict:
    return {
        "pause_until": _pause_until,
        "pause_reason": _pause_reason,
        "notified_key": _notified_key,
        "session_ledger": list(_session_ledger),
    }


def load_state(data: dict | None) -> None:
    global _pause_until, _pause_reason, _notified_key, _session_ledger
    if not isinstance(data, dict):
        return
    _pause_until = str(data.get("pause_until") or "")
    _pause_reason = str(data.get("pause_reason") or "")
    _notified_key = str(data.get("notified_key") or "")
    raw = data.get("session_ledger")
    if isinstance(raw, list):
        _session_ledger = [
            e for e in raw
            if isinstance(e, dict) and e.get("date") is not None
        ][-40:]
    else:
        _session_ledger = []


def clear_pause() -> None:
    global _pause_until, _pause_reason
    _pause_until = ""
    _pause_reason = ""


def _session_day() -> str:
    return market_hours.trading_day_ny()


def _week_mon_fri(ref: datetime | None = None) -> list[str]:
    d = (ref or market_hours.now_ny()).astimezone(NY)
    monday = (d - timedelta(days=d.weekday())).date()
    return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]


def _week_friday() -> str:
    return _week_mon_fri()[4]


def _trade_pnl_usd(t: dict) -> float:
    try:
        qty = int(t.get("quantity") or 0)
        buy = float(t.get("buy_price") or 0)
        sell = float(t.get("sell_price") or 0)
        return (sell - buy) * qty
    except (TypeError, ValueError):
        return 0.0


def _live_trades(trades: list[dict]) -> list[dict]:
    return [t for t in (trades or []) if t.get("is_live")]


def _consec_losses(trades: list[dict]) -> int:
    n = 0
    for t in reversed(_live_trades(trades)):
        if _trade_pnl_usd(t) <= 0:
            n += 1
        else:
            break
    return n


def _session_pnl(trades: list[dict]) -> float:
    return round(sum(_trade_pnl_usd(t) for t in _live_trades(trades)), 2)


def record_session_pnl(date_str: str, pnl_usd: float, trades: int) -> None:
    """장마감·갱신 시 세션 장부 반영."""
    global _session_ledger
    entry = {
        "date": date_str,
        "pnl_usd": round(float(pnl_usd), 2),
        "trades": int(trades),
    }
    for i, row in enumerate(_session_ledger):
        if row.get("date") == date_str:
            _session_ledger[i] = entry
            _session_ledger = _session_ledger[-40:]
            return
    _session_ledger.append(entry)
    _session_ledger = _session_ledger[-40:]


def _weekly_pnl(trades_today: list[dict], today: str) -> float:
    week = set(_week_mon_fri())
    total = 0.0
    for row in _session_ledger:
        d = str(row.get("date") or "")
        if d in week and d != today:
            try:
                total += float(row.get("pnl_usd") or 0)
            except (TypeError, ValueError):
                pass
    total += _session_pnl(trades_today)
    return round(total, 2)


def _set_pause(until: str, reason: str) -> bool:
    global _pause_until, _pause_reason
    if not _pause_until or until > _pause_until:
        _pause_until = until
        _pause_reason = reason
        return True
    if until == _pause_until and reason != _pause_reason:
        _pause_reason = reason
        return True
    return False


def refresh(*, trades_today: list[dict]) -> dict:
    if not ENABLED:
        return {"blocked": False, "reason": "방어OFF", "changed": False}

    today = _session_day()
    global _pause_until, _pause_reason
    if _pause_until and today > _pause_until:
        _pause_until = ""
        _pause_reason = ""

    live = _live_trades(trades_today)
    consec = _consec_losses(trades_today)
    session_pnl = _session_pnl(trades_today)
    week_pnl = _weekly_pnl(trades_today, today)
    # 장부에도 오늘분 반영 (재시작 대비)
    if live:
        record_session_pnl(today, session_pnl, len(live))

    changed = False
    if WEEKLY_LOSS_LIMIT < 0 and week_pnl <= WEEKLY_LOSS_LIMIT:
        until = _week_friday()
        reason = (
            f"주간실전 ${week_pnl:+.2f} ≤ ${WEEKLY_LOSS_LIMIT:g} "
            f"→ {until}까지 실전 중단"
        )
        changed = _set_pause(until, reason) or changed

    if SESSION_LOSS_LIMIT < 0 and session_pnl <= SESSION_LOSS_LIMIT:
        reason = (
            f"세션실전 ${session_pnl:+.2f} ≤ ${SESSION_LOSS_LIMIT:g} "
            f"→ 오늘 실전 중단"
        )
        changed = _set_pause(today, reason) or changed

    if MAX_CONSEC_LOSSES > 0 and consec >= MAX_CONSEC_LOSSES:
        reason = (
            f"연속실전손실 {consec}건 ≥ {MAX_CONSEC_LOSSES}건 "
            f"→ 오늘 실전 중단"
        )
        changed = _set_pause(today, reason) or changed

    blocked = bool(_pause_until and today <= _pause_until)
    return {
        "blocked": blocked,
        "reason": _pause_reason if blocked else "방어OK",
        "consec": consec,
        "session_pnl": session_pnl,
        "week_pnl": week_pnl,
        "pause_until": _pause_until,
        "changed": changed,
    }


def allow_live(*, trades_today: list[dict] | None = None) -> tuple[bool, str]:
    if not ENABLED:
        return True, "방어OFF"
    status = refresh(trades_today=trades_today or [])
    if status["blocked"]:
        return False, status["reason"]
    return True, status["reason"]


def notify_if_triggered(send_fn, status: dict | None = None) -> None:
    global _notified_key
    if not ENABLED or not callable(send_fn) or not status:
        return
    if not status.get("blocked") or not status.get("changed"):
        return
    key = f"{_pause_until}|{_pause_reason}"
    if key == _notified_key:
        return
    _notified_key = key
    send_fn(
        "🛡️ <b>US 방어모드 — 실전 신규 일시중단</b>\n"
        f"{status.get('reason', _pause_reason)}\n"
        "대상: 실전 신규매수만 (시뮬은 계속)\n"
        "보유 청산(손절·익절·장종료)은 정상 동작"
    )


def format_status_line() -> str:
    if not ENABLED:
        return "US방어: OFF"
    today = _session_day()
    active = bool(_pause_until and today <= _pause_until)
    base = (
        f"US방어: ON (연속≥{MAX_CONSEC_LOSSES} · "
        f"세션≤${SESSION_LOSS_LIMIT:g} · 주≤${WEEKLY_LOSS_LIMIT:g})"
    )
    if active:
        return f"{base} · ⛔{_pause_until}까지"
    return f"{base} · 가동중"
