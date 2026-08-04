"""
미국주식 시뮬 (실제 주문 없음)

진입 우선순위:
  1) Gap & Go — 시가갭 + RVOL + (ORB고 돌파 또는 갭 유지) + VWAP 위
  2) ORB — 개장 N분 레인지 고가 돌파 + RVOL + VWAP 위
  3) S·RVOL — 일봉 모멘텀 + 시간보정 RVOL + VWAP 위

청산: -2% 손절 / +5% 후 트레일 -0.6% / 정규장 종료 강제청산
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
SIM_AMOUNT_USD = float(os.getenv("US_SIM_AMOUNT_USD", "500"))
STOP_LOSS_PCT = float(os.getenv("US_SIM_STOP_LOSS_PCT", "2.0"))
TAKE_PROFIT_PCT = float(os.getenv("US_SIM_TAKE_PROFIT_PCT", "5.0"))
TRAILING_STOP_PCT = float(os.getenv("US_SIM_TRAILING_STOP_PCT", "0.6"))
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

_open: dict | None = None
_trades_today: list[dict] = []
_bought_symbols_today: set[str] = set()
# symbol -> {high, low, ready, date}
_orb_ranges: dict[str, dict] = {}
# 폴링당 VWAP 캐시
_vwap_cache: dict[str, float] = {}


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


def get_trades_today() -> list[dict]:
    return list(_trades_today)


def dump_state() -> dict:
    return {
        "open": dict(_open) if _open else None,
        "trades_today": list(_trades_today),
        "bought_symbols_today": sorted(_bought_symbols_today),
        "orb_ranges": dict(_orb_ranges),
        "date": datetime.now(KST).strftime("%Y-%m-%d"),
    }


def load_state(data: dict | None) -> None:
    global _open, _trades_today, _bought_symbols_today, _orb_ranges
    if not isinstance(data, dict):
        return
    today = datetime.now(KST).strftime("%Y-%m-%d")
    _open = data.get("open") if isinstance(data.get("open"), dict) else None
    if data.get("date") == today:
        _trades_today = list(data.get("trades_today") or [])
        _bought_symbols_today = set(data.get("bought_symbols_today") or [])
        raw_orb = data.get("orb_ranges") or {}
        _orb_ranges = dict(raw_orb) if isinstance(raw_orb, dict) else {}
    else:
        _trades_today = []
        _bought_symbols_today = set()
        _orb_ranges = {}


def reset_daily() -> None:
    global _open, _trades_today, _bought_symbols_today, _orb_ranges, _vwap_cache
    _open = None
    _trades_today = []
    _bought_symbols_today = set()
    _orb_ranges = {}
    _vwap_cache = {}


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
        return False, ""
    if len(daily) < max(60, VOL_AVG_DAYS + 2):
        return False, ""
    closes = [current] + [b["close"] for b in daily[1:]]
    day_pct = _day_pct(daily, current)
    if day_pct < MIN_DAY_PCT:
        return False, ""
    ma20 = sum(closes[:20]) / 20
    if current < ma20:
        return False, ""
    rsi = _rsi(closes, 14)
    if rsi > MAX_RSI:
        return False, ""
    check_vol = today_vol if today_vol > 0 else float(daily[0].get("volume") or 0)
    rvol = _rvol(daily, check_vol)
    if rvol < MIN_RVOL:
        return False, ""
    ok_vwap, vwap = _above_vwap(symbol, exchange, current)
    if not ok_vwap:
        return False, ""
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
        return False, ""
    if not market_hours.is_orb_entry_window(ORB_MINUTES, ORB_ENTRY_UNTIL):
        return False, ""
    st = _update_orb(symbol, price, day_high, day_low)
    if not st or not st.get("ready"):
        return False, ""
    or_high = float(st["high"])
    if or_high <= 0 or price < or_high:
        return False, ""
    day_pct = _day_pct(daily, price)
    if day_pct < ORB_MIN_DAY_PCT:
        return False, ""
    rvol = _rvol(daily, today_vol if today_vol > 0 else float(daily[0].get("volume") or 0))
    if rvol < ORB_MIN_RVOL:
        return False, ""
    ok_vwap, vwap = _above_vwap(symbol, exchange, price)
    if not ok_vwap:
        return False, ""
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
        return False, ""
    if not market_hours.is_orb_entry_window(ORB_MINUTES, GAP_ENTRY_UNTIL):
        return False, ""
    gap = _gap_pct(daily, open_px if open_px > 0 else price)
    if gap < GAP_MIN_PCT or gap > GAP_MAX_PCT:
        return False, ""
    if open_px > 0 and price < open_px:
        return False, ""  # 갭 페이드
    rvol = _rvol(daily, today_vol if today_vol > 0 else float(daily[0].get("volume") or 0))
    if rvol < GAP_MIN_RVOL:
        return False, ""
    st = _update_orb(symbol, price, day_high, day_low)
    if not st or not st.get("ready"):
        return False, ""
    or_high = float(st["high"])
    if or_high <= 0 or price < or_high:
        return False, ""
    ok_vwap, vwap = _above_vwap(symbol, exchange, price)
    if not ok_vwap:
        return False, ""
    vwap_txt = f" · VWAP ${vwap:.2f}" if vwap > 0 else ""
    return True, (
        f"Gap&Go · 갭 {gap:+.1f}% · OR고 ${or_high:.2f} · "
        f"RVOL {rvol:.1f}x{vwap_txt}"
    )


def _qty(price: float) -> int:
    if price <= 0:
        return 0
    return max(int(SIM_AMOUNT_USD // price), 1)


def _evaluate_exit(pos: dict, price: float) -> tuple[bool, str]:
    buy = float(pos["buy_price"])
    pct = (price - buy) / buy * 100 if buy > 0 else 0
    if pct <= -STOP_LOSS_PCT:
        return True, f"손절 ({pct:.1f}%)"
    peak = float(pos.get("peak_price", buy))
    peak_pct = (peak - buy) / buy * 100 if buy > 0 else 0
    drop = (peak - price) / peak * 100 if peak > 0 else 0
    if peak_pct >= TAKE_PROFIT_PCT and drop >= TRAILING_STOP_PCT:
        return True, f"트레일링 (+{pct:.1f}% / 고점 {peak_pct:.1f}%에서 -{drop:.1f}%)"
    return False, ""


def force_close(price: float, reason: str) -> dict | None:
    global _open
    if not _open:
        return None
    pos = _open
    buy = float(pos["buy_price"])
    qty = int(pos["quantity"])
    pct = (price - buy) / buy * 100 if buy > 0 else 0
    trade = {
        "action": "sell",
        "symbol": pos["symbol"],
        "exchange": pos["exchange"],
        "quantity": qty,
        "buy_price": buy,
        "sell_price": price,
        "profit_pct": round(pct, 2),
        "sell_reason": reason,
        "strategy": pos.get("strategy", ""),
    }
    _trades_today.append(trade)
    _open = None
    return trade


def run_check() -> list[dict]:
    """정규장 중 호출. 이벤트 리스트 반환."""
    global _open, _vwap_cache
    if not ENABLED or not market_hours.is_us_regular_session():
        return []
    events: list[dict] = []
    _vwap_cache = {}  # 폴링마다 갱신

    if _open:
        try:
            px = kis_us_api.get_us_price(_open["symbol"], _open["exchange"])
            price = float(px["last"])
        except Exception as e:
            print(f"[US시뮬] 보유 시세 실패: {e}")
            return events
        if price <= 0:
            return events
        if price > float(_open.get("peak_price", _open["buy_price"])):
            _open["peak_price"] = price
        should, reason = _evaluate_exit(_open, price)
        if should:
            t = force_close(price, reason)
            if t:
                events.append(t)
        return events

    for symbol, exchange in _active_watchlist():
        if symbol in _bought_symbols_today:
            continue
        if us_screener.is_mega_cap(symbol):
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
            print(f"[US시뮬] {symbol} 조회 실패: {e}")
            continue
        if price <= 0 or not daily:
            continue
        if open_px <= 0:
            open_px = float(daily[0].get("open") or 0) or price

        if ENABLE_ORB or ENABLE_GAP_GO:
            _update_orb(symbol, price, day_high, day_low)

        ok, reason, strategy = False, "", ""
        building = market_hours.is_orb_building(ORB_MINUTES)

        if not building:
            ok, reason = _match_gap_and_go(
                symbol, exchange, daily, price, open_px, today_vol, day_high, day_low,
            )
            strategy = "GapGo"
        if not ok and not building:
            ok, reason = _match_orb(
                symbol, exchange, daily, price, today_vol, day_high, day_low,
            )
            strategy = "ORB"
        if not ok:
            ok, reason = _match_s_rule(
                daily, price, today_vol, symbol=symbol, exchange=exchange,
            )
            strategy = "S"
        if not ok:
            continue

        qty = _qty(price)
        if qty < 1:
            continue
        _open = {
            "symbol": symbol,
            "exchange": exchange,
            "quantity": qty,
            "buy_price": price,
            "peak_price": price,
            "buy_reason": reason,
            "strategy": strategy,
        }
        _bought_symbols_today.add(symbol)
        events.append({
            "action": "buy",
            "symbol": symbol,
            "exchange": exchange,
            "quantity": qty,
            "price": price,
            "reason": reason,
            "strategy": strategy,
        })
        break  # 1포지션
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
    if rules:
        lines.append("🇺🇸 규칙: " + " + ".join(rules))
    if _trades_today:
        lines.append(f"🇺🇸 US 시뮬 오늘 체결 {len(_trades_today)}건")
        for t in _trades_today:
            s = "+" if t["profit_pct"] >= 0 else ""
            tag = t.get("strategy") or ""
            lines.append(
                f"  {tag+' ' if tag else ''}{t['symbol']} "
                f"${t['buy_price']:.2f}→${t['sell_price']:.2f} {s}{t['profit_pct']}%"
            )
    if _open:
        tag = _open.get("strategy") or ""
        lines.append(
            f"🇺🇸 US 시뮬 보유 {tag+' ' if tag else ''}{_open['symbol']} "
            f"${_open['buy_price']:.2f} × {_open['quantity']}"
        )
    return lines
