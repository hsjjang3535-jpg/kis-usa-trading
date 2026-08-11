"""
미국주식 시뮬 + 방식A 실전(옵션)

진입 우선순위:
  1) Gap & Go — 시가갭 + RVOL + ORB고 돌파 + VWAP 위
  2) ORB — 개장 N분 레인지 고가 돌파 + RVOL + VWAP 위
  3) S·RVOL — 일봉 모멘텀 + 시간보정 RVOL + VWAP 위

청산: -2% 손절 / +5% 익절 (하드) / 정규장 종료 강제청산

방식 A: 세션당 첫 진입만 실주문. 주포지션 보유 중에도
다른 워치 종목은 병렬 시뮬로 진입·알림 가능 (US_PARALLEL_SIM).
"""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import kis_us_api
import market_hours
import us_screener

KST = ZoneInfo("Asia/Seoul")

ENABLED = os.getenv("ENABLE_US_SIM", "true").lower() == "true"
LIVE_ORDERS = os.getenv("ENABLE_US_LIVE_ORDERS", "false").lower() == "true"
# 1종목 예산 ≈100만원 (USD, 환율에 따라 변동)
SIM_AMOUNT_USD = float(os.getenv("US_SIM_AMOUNT_USD", "750"))
LIVE_AMOUNT_USD = float(os.getenv("US_LIVE_AMOUNT_USD", os.getenv("US_SIM_AMOUNT_USD", "750")))
STOP_LOSS_PCT = float(os.getenv("US_SIM_STOP_LOSS_PCT", "2.0"))
TAKE_PROFIT_PCT = float(os.getenv("US_SIM_TAKE_PROFIT_PCT", "5.0"))
MIN_DAY_PCT = float(os.getenv("US_SIM_MIN_DAY_PCT", "3.0"))
MIN_RVOL = float(os.getenv("US_SIM_MIN_RVOL", os.getenv("US_SIM_MIN_VOL_RATIO", "2.5")))
VOL_AVG_DAYS = int(os.getenv("US_SIM_VOL_AVG_DAYS", "20"))
MAX_RSI = float(os.getenv("US_SIM_MAX_RSI", "75"))

ENABLE_S = os.getenv("ENABLE_US_S_RULE", "true").lower() == "true"
ENABLE_ORB = os.getenv("ENABLE_US_ORB", "true").lower() == "true"
ENABLE_GAP_GO = os.getenv("ENABLE_US_GAP_GO", "true").lower() == "true"
REQUIRE_ABOVE_VWAP = os.getenv("US_REQUIRE_ABOVE_VWAP", "true").lower() == "true"
VWAP_NMIN = int(os.getenv("US_VWAP_NMIN", "5"))

ORB_MINUTES = int(os.getenv("US_ORB_MINUTES", "15"))
ORB_ENTRY_UNTIL = int(os.getenv("US_ORB_ENTRY_UNTIL_MIN", "120"))
ORB_MIN_RVOL = float(os.getenv("US_ORB_MIN_RVOL", "1.5"))
ORB_MIN_DAY_PCT = float(os.getenv("US_ORB_MIN_DAY_PCT", "1.0"))

GAP_MIN_PCT = float(os.getenv("US_GAP_MIN_PCT", "2.5"))
GAP_MAX_PCT = float(os.getenv("US_GAP_MAX_PCT", "15.0"))
GAP_MIN_RVOL = float(os.getenv("US_GAP_MIN_RVOL", "2.0"))
GAP_ENTRY_UNTIL = int(os.getenv("US_GAP_ENTRY_UNTIL_MIN", "90"))

PARALLEL_SIM = os.getenv("US_PARALLEL_SIM", "true").lower() == "true"
MAX_SIM_POSITIONS = int(os.getenv("US_MAX_SIM_POSITIONS", "5"))
SKIP_NOTIFY_INTERVAL_MIN = int(os.getenv("US_SKIP_NOTIFY_INTERVAL_MIN", "30"))

_open: dict | None = None  # 주포지션 (실전 가능)
_paper: dict[str, dict] = {}  # 병렬 시뮬 symbol -> pos
_trades_today: list[dict] = []
_bought_symbols_today: set[str] = set()
_live_used_today = False
_orb_ranges: dict[str, dict] = {}
_vwap_cache: dict[str, float] = {}
_last_skips: list[dict] = []
_last_skip_notify_at: datetime | None = None


def _active_watchlist() -> list[tuple[str, str]]:
    return [
        (w["symbol"], w.get("exchange") or "NAS")
        for w in us_screener.get_watchlist()
        if w.get("symbol")
    ]


def is_enabled() -> bool:
    return ENABLED


def get_open() -> dict | None:
    return _open


def get_paper_positions() -> dict[str, dict]:
    return dict(_paper)


def get_trades_today() -> list[dict]:
    return list(_trades_today)


def dump_state() -> dict:
    return {
        "open": dict(_open) if _open else None,
        "paper": {k: dict(v) for k, v in _paper.items()},
        "trades_today": list(_trades_today),
        "bought_symbols_today": sorted(_bought_symbols_today),
        "live_used_today": _live_used_today,
        "orb_ranges": dict(_orb_ranges),
        "date": market_hours.trading_day_ny(),
    }


def load_state(data: dict | None) -> None:
    global _open, _paper, _trades_today, _bought_symbols_today, _orb_ranges, _live_used_today
    if not isinstance(data, dict):
        return
    today = market_hours.trading_day_ny()
    _open = data.get("open") if isinstance(data.get("open"), dict) else None
    raw_paper = data.get("paper") if isinstance(data.get("paper"), dict) else {}
    _paper = {k: dict(v) for k, v in raw_paper.items() if isinstance(v, dict)}
    if data.get("date") == today:
        _trades_today = list(data.get("trades_today") or [])
        _bought_symbols_today = set(data.get("bought_symbols_today") or [])
        _live_used_today = bool(data.get("live_used_today"))
        raw_orb = data.get("orb_ranges") or {}
        _orb_ranges = dict(raw_orb) if isinstance(raw_orb, dict) else {}
    else:
        _trades_today = []
        _bought_symbols_today = set()
        _live_used_today = False
        _orb_ranges = {}
        _paper = {}


def reset_daily() -> None:
    global _open, _paper, _trades_today, _bought_symbols_today, _orb_ranges
    global _vwap_cache, _live_used_today, _last_skips, _last_skip_notify_at
    _open = None
    _paper = {}
    _trades_today = []
    _bought_symbols_today = set()
    _live_used_today = False
    _orb_ranges = {}
    _vwap_cache = {}
    _last_skips = []
    _last_skip_notify_at = None


def live_slot_available() -> bool:
    """방식 A: 아직 세션 실전 슬롯 남음."""
    return LIVE_ORDERS and not _live_used_today


def mark_live_used() -> None:
    global _live_used_today
    _live_used_today = True


def _record_skip(symbol: str, reason: str) -> None:
    _last_skips.append({
        "symbol": symbol,
        "reason": reason,
        "at": market_hours.now_ny().strftime("%H:%M"),
    })
    print(f"[US스킵] {symbol}: {reason}")


def should_notify_skips() -> bool:
    if not _last_skips:
        return False
    now = datetime.now(KST)
    if _last_skip_notify_at is None:
        return True
    return (now - _last_skip_notify_at).total_seconds() >= SKIP_NOTIFY_INTERVAL_MIN * 60


def consume_skip_digest() -> list[str]:
    """심볼별 마지막 스킵 사유 요약 후 버퍼 비움."""
    global _last_skips, _last_skip_notify_at
    if not _last_skips:
        return []
    by_sym: dict[str, str] = {}
    for s in _last_skips:
        by_sym[s["symbol"]] = s["reason"]
    lines = [f"· {sym}: {why}" for sym, why in sorted(by_sym.items())]
    _last_skips = []
    _last_skip_notify_at = datetime.now(KST)
    return lines


def _rsi(closes_latest_first: list[float], period: int = 14) -> float:
    if len(closes_latest_first) < period + 1:
        return 50.0
    prices = list(reversed(closes_latest_first[: period + 1]))
    gains = losses = 0.0
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return round(100 - (100 / (1 + rs)), 2)


def _rvol(daily: list[dict], today_vol: float) -> float:
    """시간 보정 RVOL = (당일누적/진행률) / 20일평균."""
    if len(daily) < VOL_AVG_DAYS + 1:
        return 0.0
    prior = [float(b["volume"]) for b in daily[1 : VOL_AVG_DAYS + 1] if float(b.get("volume") or 0) > 0]
    if not prior:
        return 0.0
    avg = sum(prior) / len(prior)
    if avg <= 0:
        return 0.0
    frac = market_hours.session_elapsed_fraction()
    projected = today_vol / frac if frac > 0 else today_vol
    return projected / avg


def _day_pct(daily: list[dict], current: float) -> float:
    prev = daily[1]["close"] if len(daily) > 1 else 0
    if prev <= 0:
        return 0.0
    return (current - prev) / prev * 100


def _gap_pct(daily: list[dict], open_px: float) -> float:
    """시가 갭% = (시가 - 전일종가) / 전일종가."""
    prev = daily[1]["close"] if len(daily) > 1 else 0
    if prev <= 0 or open_px <= 0:
        return 0.0
    return (open_px - prev) / prev * 100


def _get_vwap(symbol: str, exchange: str) -> float:
    key = f"{exchange}:{symbol}"
    if key in _vwap_cache:
        return _vwap_cache[key]
    try:
        vwap = kis_us_api.get_approx_vwap(symbol, exchange, nmin=VWAP_NMIN)
    except Exception as e:
        print(f"[US시뮬] VWAP 실패 {symbol}: {e}")
        vwap = 0.0
    _vwap_cache[key] = vwap
    return vwap


def _above_vwap(symbol: str, exchange: str, price: float) -> tuple[bool, float]:
    """VWAP 필터. 조회 실패(0)면 통과(차단하지 않음)."""
    if not REQUIRE_ABOVE_VWAP:
        return True, 0.0
    vwap = _get_vwap(symbol, exchange)
    if vwap <= 0:
        return True, 0.0
    return price >= vwap, vwap


def _match_s_rule(
    daily: list[dict],
    current: float,
    today_vol: float,
    *,
    symbol: str,
    exchange: str,
) -> tuple[bool, str]:
    if not ENABLE_S:
        return False, "S오프"
    if len(daily) < max(60, VOL_AVG_DAYS + 2):
        return False, "S:일봉부족"
    closes = [current] + [b["close"] for b in daily[1:]]
    day_pct = _day_pct(daily, current)
    if day_pct < MIN_DAY_PCT:
        return False, f"S:당일{day_pct:+.1f}%<{MIN_DAY_PCT:g}%"
    ma20 = sum(closes[:20]) / 20
    if current < ma20:
        return False, f"S:MA20아래(${ma20:.2f})"
    rsi = _rsi(closes, 14)
    if rsi > MAX_RSI:
        return False, f"S:RSI{rsi:.0f}>{MAX_RSI:g}"
    check_vol = today_vol if today_vol > 0 else float(daily[0].get("volume") or 0)
    rvol = _rvol(daily, check_vol)
    if rvol < MIN_RVOL:
        return False, f"S:RVOL{rvol:.1f}x<{MIN_RVOL:g}"
    ok_vwap, vwap = _above_vwap(symbol, exchange, current)
    if not ok_vwap:
        return False, f"S:VWAP아래(${vwap:.2f})"
    vwap_txt = f" · VWAP ${vwap:.2f}" if vwap > 0 else ""
    return True, (
        f"S·RVOL · {day_pct:+.1f}% · RVOL {rvol:.1f}x · "
        f"MA20 ${ma20:.2f} · RSI {rsi:.0f}{vwap_txt}"
    )


def _update_orb(symbol: str, price: float, day_high: float, day_low: float) -> dict | None:
    """오프닝 레인지 갱신. 반환: 해당 심볼 ORB 상태."""
    today = market_hours.now_ny().strftime("%Y-%m-%d")
    st = _orb_ranges.get(symbol)
    if not st or st.get("date") != today:
        st = {"high": price, "low": price, "ready": False, "date": today}
        _orb_ranges[symbol] = st

    if market_hours.is_orb_building(ORB_MINUTES):
        hi = max(float(st["high"]), price)
        lo = min(float(st["low"]), price)
        # 형성 구간에는 폴링 시세만 반영 (당일 high는 프리장 포함 가능)
        st["high"] = hi
        st["low"] = lo
        st["ready"] = False
    else:
        if not st.get("ready") and float(st.get("high") or 0) > 0:
            st["ready"] = True
        if not st.get("ready") and float(st.get("high") or 0) <= 0 and price > 0:
            st["high"] = price
            st["low"] = price
            st["ready"] = True
    return st


def _match_orb(
    symbol: str,
    exchange: str,
    daily: list[dict],
    price: float,
    today_vol: float,
    day_high: float,
    day_low: float,
) -> tuple[bool, str]:
    if not ENABLE_ORB:
        return False, "ORB오프"
    if not market_hours.is_orb_entry_window(ORB_MINUTES, ORB_ENTRY_UNTIL):
        return False, "ORB:시간외"
    st = _update_orb(symbol, price, day_high, day_low)
    if not st or not st.get("ready"):
        return False, "ORB:레인지미확정"
    or_high = float(st["high"])
    if or_high <= 0 or price < or_high:
        return False, f"ORB:미돌파(OR고${or_high:.2f})"
    day_pct = _day_pct(daily, price)
    if day_pct < ORB_MIN_DAY_PCT:
        return False, f"ORB:당일{day_pct:+.1f}%<{ORB_MIN_DAY_PCT:g}%"
    rvol = _rvol(daily, today_vol if today_vol > 0 else float(daily[0].get("volume") or 0))
    if rvol < ORB_MIN_RVOL:
        return False, f"ORB:RVOL{rvol:.1f}x<{ORB_MIN_RVOL:g}"
    ok_vwap, vwap = _above_vwap(symbol, exchange, price)
    if not ok_vwap:
        return False, f"ORB:VWAP아래(${vwap:.2f})"
    vwap_txt = f" · VWAP ${vwap:.2f}" if vwap > 0 else ""
    return True, (
        f"ORB {ORB_MINUTES}m돌파 · OR고 ${or_high:.2f} · "
        f"{day_pct:+.1f}% · RVOL {rvol:.1f}x{vwap_txt}"
    )


def _match_gap_and_go(
    symbol: str,
    exchange: str,
    daily: list[dict],
    price: float,
    open_px: float,
    today_vol: float,
    day_high: float,
    day_low: float,
) -> tuple[bool, str]:
    """
    Gap & Go: 시가갭 구간 + RVOL + 갭 유지(price≥open) + ORB고 돌파 + VWAP 위.
    """
    if not ENABLE_GAP_GO:
        return False, "GapGo오프"
    if not market_hours.is_orb_entry_window(ORB_MINUTES, GAP_ENTRY_UNTIL):
        return False, "GapGo:시간외"
    gap = _gap_pct(daily, open_px if open_px > 0 else price)
    if gap < GAP_MIN_PCT or gap > GAP_MAX_PCT:
        return False, f"GapGo:갭{gap:+.1f}%범위외"
    if open_px > 0 and price < open_px:
        return False, "GapGo:갭페이드"
    rvol = _rvol(daily, today_vol if today_vol > 0 else float(daily[0].get("volume") or 0))
    if rvol < GAP_MIN_RVOL:
        return False, f"GapGo:RVOL{rvol:.1f}x<{GAP_MIN_RVOL:g}"
    st = _update_orb(symbol, price, day_high, day_low)
    if not st or not st.get("ready"):
        return False, "GapGo:ORB미확정"
    or_high = float(st["high"])
    if or_high <= 0 or price < or_high:
        return False, f"GapGo:OR미돌파(${or_high:.2f})"
    ok_vwap, vwap = _above_vwap(symbol, exchange, price)
    if not ok_vwap:
        return False, f"GapGo:VWAP아래(${vwap:.2f})"
    vwap_txt = f" · VWAP ${vwap:.2f}" if vwap > 0 else ""
    return True, (
        f"Gap&Go · 갭 {gap:+.1f}% · OR고 ${or_high:.2f} · "
        f"RVOL {rvol:.1f}x{vwap_txt}"
    )


def _qty(price: float, *, live: bool) -> int:
    if price <= 0:
        return 0
    budget = LIVE_AMOUNT_USD if live else SIM_AMOUNT_USD
    return max(int(budget // price), 1)


def _evaluate_exit(pos: dict, price: float) -> tuple[bool, str]:
    buy = float(pos["buy_price"])
    pct = (price - buy) / buy * 100 if buy > 0 else 0
    if pct <= -STOP_LOSS_PCT:
        return True, f"손절 ({pct:.1f}% ≤ -{STOP_LOSS_PCT:g}%)"
    if pct >= TAKE_PROFIT_PCT:
        return True, f"익절 ({pct:.1f}% ≥ +{TAKE_PROFIT_PCT:g}%)"
    return False, ""


def _make_sell_event(pos: dict, price: float, reason: str) -> dict:
    buy = float(pos["buy_price"])
    qty = int(pos["quantity"])
    pct = (price - buy) / buy * 100 if buy > 0 else 0
    return {
        "action": "sell",
        "symbol": pos["symbol"],
        "exchange": pos["exchange"],
        "quantity": qty,
        "buy_price": buy,
        "sell_price": price,
        "profit_pct": round(pct, 2),
        "sell_reason": reason,
        "strategy": pos.get("strategy", ""),
        "is_live": bool(pos.get("is_live")),
        "paper": bool(pos.get("paper")),
    }


def force_close(price: float, reason: str) -> dict | None:
    global _open
    if not _open:
        return None
    trade = _make_sell_event(_open, price, reason)
    _trades_today.append(trade)
    _open = None
    return trade


def force_close_paper(symbol: str, price: float, reason: str) -> dict | None:
    global _paper
    pos = _paper.pop(symbol, None)
    if not pos:
        return None
    trade = _make_sell_event(pos, price, reason)
    _trades_today.append(trade)
    return trade


def force_close_all(price_fn) -> list[dict]:
    """세션 종료 등 — 주포지션+병렬시뮬 전량. price_fn(symbol, exchange)->float"""
    events: list[dict] = []
    if _open:
        try:
            px = price_fn(_open["symbol"], _open["exchange"])
        except Exception:
            px = float(_open["buy_price"])
        t = force_close(px, "정규장 종료 청산")
        if t:
            events.append(t)
    for sym in list(_paper.keys()):
        pos = _paper[sym]
        try:
            px = price_fn(sym, pos["exchange"])
        except Exception:
            px = float(pos["buy_price"])
        t = force_close_paper(sym, px, "정규장 종료 청산")
        if t:
            events.append(t)
    return events


def _try_entry_signal(
    symbol: str,
    exchange: str,
    daily: list[dict],
    price: float,
    open_px: float,
    today_vol: float,
    day_high: float,
    day_low: float,
) -> tuple[bool, str, str]:
    """(ok, reason, strategy). 실패 시 reason=스킵 사유."""
    building = market_hours.is_orb_building(ORB_MINUTES)
    skips: list[str] = []

    if not building:
        ok, reason = _match_gap_and_go(
            symbol, exchange, daily, price, open_px, today_vol, day_high, day_low,
        )
        if ok:
            return True, reason, "GapGo"
        if reason:
            skips.append(reason)
        ok, reason = _match_orb(
            symbol, exchange, daily, price, today_vol, day_high, day_low,
        )
        if ok:
            return True, reason, "ORB"
        if reason:
            skips.append(reason)
    else:
        skips.append("ORB형성중")

    ok, reason = _match_s_rule(
        daily, price, today_vol, symbol=symbol, exchange=exchange,
    )
    if ok:
        return True, reason, "S"
    if reason:
        skips.append(reason)
    return False, " / ".join(skips) if skips else "조건미충족", ""


def _open_position(
    *,
    symbol: str,
    exchange: str,
    price: float,
    reason: str,
    strategy: str,
    live: bool,
    paper: bool,
) -> dict:
    qty = _qty(price, live=live and not paper)
    pos = {
        "symbol": symbol,
        "exchange": exchange,
        "quantity": qty,
        "buy_price": price,
        "peak_price": price,
        "buy_reason": reason,
        "strategy": strategy,
        "is_live": live and not paper,
        "paper": paper,
    }
    _bought_symbols_today.add(symbol)
    return pos


def run_check() -> list[dict]:
    """정규장 중 호출. 이벤트 리스트 반환."""
    global _open, _vwap_cache, _paper
    if not ENABLED or not market_hours.is_us_regular_session():
        return []
    events: list[dict] = []
    _vwap_cache = {}

    # ── 주포지션 청산 ──────────────────────────────────────────
    if _open:
        try:
            px = kis_us_api.get_us_price(_open["symbol"], _open["exchange"])
            price = float(px["last"])
        except Exception as e:
            print(f"[US시뮬] 보유 시세 실패: {e}")
            price = 0.0
        if price > 0:
            if price > float(_open.get("peak_price", _open["buy_price"])):
                _open["peak_price"] = price
            should, reason = _evaluate_exit(_open, price)
            if should:
                t = force_close(price, reason)
                if t:
                    events.append(t)

    # ── 병렬 시뮬 청산 ─────────────────────────────────────────
    for sym in list(_paper.keys()):
        pos = _paper[sym]
        try:
            px = kis_us_api.get_us_price(sym, pos["exchange"])
            price = float(px["last"])
        except Exception as e:
            print(f"[US시뮬] 병렬시세 실패 {sym}: {e}")
            continue
        if price <= 0:
            continue
        if price > float(pos.get("peak_price", pos["buy_price"])):
            pos["peak_price"] = price
        should, reason = _evaluate_exit(pos, price)
        if should:
            t = force_close_paper(sym, price, reason)
            if t:
                events.append(t)

    # ── 신규 스캔 ───────────────────────────────────────────────
    held = set()
    if _open:
        held.add(_open["symbol"])
    held |= set(_paper.keys())

    primary_free = _open is None
    paper_slots = MAX_SIM_POSITIONS - len(_paper)

    for symbol, exchange in _active_watchlist():
        if symbol in _bought_symbols_today or symbol in held:
            continue
        if us_screener.is_mega_cap(symbol):
            _record_skip(symbol, "메가캡제외")
            continue
        try:
            px = kis_us_api.get_us_price(symbol, exchange)
            price = float(px["last"])
            today_vol = float(px.get("volume") or 0)
            day_high = float(px.get("high") or 0)
            day_low = float(px.get("low") or 0)
            open_px = float(px.get("open") or 0)
            daily = kis_us_api.get_us_daily_prices(symbol, exchange, days=80)
        except Exception as e:
            _record_skip(symbol, f"조회실패:{e}")
            continue
        if price <= 0 or not daily:
            _record_skip(symbol, "시세/일봉없음")
            continue
        if open_px <= 0:
            open_px = float(daily[0].get("open") or 0) or price

        if ENABLE_ORB or ENABLE_GAP_GO:
            _update_orb(symbol, price, day_high, day_low)

        ok, reason, strategy = _try_entry_signal(
            symbol, exchange, daily, price, open_px, today_vol, day_high, day_low,
        )
        if not ok:
            _record_skip(symbol, reason)
            continue

        # 주포지션 슬롯 비어 있으면 실전/시뮬 주포지션
        if primary_free:
            want_live = live_slot_available()
            pos = _open_position(
                symbol=symbol, exchange=exchange, price=price,
                reason=reason, strategy=strategy, live=want_live, paper=False,
            )
            if pos["quantity"] < 1:
                _record_skip(symbol, "수량0")
                continue
            _open = pos
            primary_free = False
            held.add(symbol)
            events.append({
                "action": "buy",
                "symbol": symbol,
                "exchange": exchange,
                "quantity": pos["quantity"],
                "price": price,
                "reason": reason,
                "strategy": strategy,
                "is_live": want_live,
                "paper": False,
            })
            # 실전 슬롯 썼으면 이후는 병렬 시뮬만
            continue

        # 주포지션 있는 동안 → 병렬 시뮬
        if not PARALLEL_SIM:
            _record_skip(symbol, f"신호OK·주포지션보유({_open and _open.get('symbol')})")
            continue
        if paper_slots <= 0:
            _record_skip(symbol, "신호OK·시뮬한도초과")
            continue
        pos = _open_position(
            symbol=symbol, exchange=exchange, price=price,
            reason=reason + " (병렬시뮬)", strategy=strategy,
            live=False, paper=True,
        )
        if pos["quantity"] < 1:
            _record_skip(symbol, "수량0")
            continue
        _paper[symbol] = pos
        paper_slots -= 1
        held.add(symbol)
        events.append({
            "action": "buy",
            "symbol": symbol,
            "exchange": exchange,
            "quantity": pos["quantity"],
            "price": price,
            "reason": pos["buy_reason"],
            "strategy": strategy,
            "is_live": False,
            "paper": True,
        })

    return events


def format_summary() -> list[str]:
    lines: list[str] = []
    rules = []
    if ENABLE_GAP_GO:
        rules.append(f"Gap&Go(≥{GAP_MIN_PCT:g}%)")
    if ENABLE_ORB:
        rules.append(f"ORB({ORB_MINUTES}m)")
    if ENABLE_S:
        rules.append(f"S(RVOL≥{MIN_RVOL:g})")
    if REQUIRE_ABOVE_VWAP:
        rules.append("VWAP↑")
    rules.append(f"익절+{TAKE_PROFIT_PCT:g}%/손절-{STOP_LOSS_PCT:g}%")
    if LIVE_ORDERS:
        slot = "실전슬롯소진" if _live_used_today else "실전1회가능"
        rules.append(slot)
    if PARALLEL_SIM:
        rules.append(f"병렬시뮬≤{MAX_SIM_POSITIONS}")
    if rules:
        lines.append("🇺🇸 규칙: " + " + ".join(rules))
    if _trades_today:
        lines.append(f"🇺🇸 US 오늘 체결 {len(_trades_today)}건")
        for t in _trades_today:
            s = "+" if t["profit_pct"] >= 0 else ""
            tag = t.get("strategy") or ""
            mode = "실전" if t.get("is_live") else "시뮬"
            lines.append(
                f"  [{mode}] {tag+' ' if tag else ''}{t['symbol']} "
                f"${t['buy_price']:.2f}→${t['sell_price']:.2f} {s}{t['profit_pct']}%"
            )
    if _open:
        tag = _open.get("strategy") or ""
        mode = "실전" if _open.get("is_live") else "시뮬"
        lines.append(
            f"🇺🇸 US 보유 [{mode}] {tag+' ' if tag else ''}{_open['symbol']} "
            f"${_open['buy_price']:.2f} × {_open['quantity']}"
        )
    for sym, pos in _paper.items():
        tag = pos.get("strategy") or ""
        lines.append(
            f"🇺🇸 US 병렬시뮬 [{tag}] {sym} "
            f"${pos['buy_price']:.2f} × {pos['quantity']}"
        )
    return lines
