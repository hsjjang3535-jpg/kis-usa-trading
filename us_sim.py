"""
미국주식 시뮬 (실제 주문 없음)

S규칙 (미국형 RVOL + 시총 필터):
  전일대비 +MIN_DAY% · 시간보정 RVOL ≥ MIN · 종가 ≥ MA20 · RSI ≤ MAX
  메가캡(시총순위 상위·블록리스트) 제외

ORB (Opening Range Breakout) 병행:
  개장 후 N분 고저 레인지 형성 → 고가 돌파 시 진입 (진입 윈도우 내)
  동일 시총/RVOL 필터 적용

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
ORB_MINUTES = int(os.getenv("US_ORB_MINUTES", "15"))
ORB_ENTRY_UNTIL = int(os.getenv("US_ORB_ENTRY_UNTIL_MIN", "120"))
ORB_MIN_RVOL = float(os.getenv("US_ORB_MIN_RVOL", "1.5"))
ORB_MIN_DAY_PCT = float(os.getenv("US_ORB_MIN_DAY_PCT", "1.0"))

_open: dict | None = None
_trades_today: list[dict] = []
_bought_symbols_today: set[str] = set()
# symbol -> {high, low, ready, date}
_orb_ranges: dict[str, dict] = {}


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
    global _open, _trades_today, _bought_symbols_today, _orb_ranges
    _open = None
    _trades_today = []
    _bought_symbols_today = set()
    _orb_ranges = {}


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


def _match_s_rule(daily: list[dict], current: float, today_vol: float) -> tuple[bool, str]:
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
    return True, (
        f"S·RVOL · {day_pct:+.1f}% · RVOL {rvol:.1f}x · "
        f"MA20 ${ma20:.2f} · RSI {rsi:.0f}"
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
        if day_high > 0:
            hi = max(hi, day_high)
        if day_low > 0:
            lo = min(lo, day_low)
        st["high"] = hi
        st["low"] = lo
        st["ready"] = False
    else:
        # 레인지 확정
        if not st.get("ready") and float(st.get("high") or 0) > 0:
            st["ready"] = True
        # 형성 구간을 놓친 경우: 당일 high/low로 대략 보정하지 않음(과대 OR)
        if not st.get("ready") and float(st.get("high") or 0) <= 0 and price > 0:
            st["high"] = price
            st["low"] = price
            st["ready"] = True
    return st


def _match_orb(
    symbol: str,
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
    return True, (
        f"ORB {ORB_MINUTES}m돌파 · OR고 ${or_high:.2f} · "
        f"{day_pct:+.1f}% · RVOL {rvol:.1f}x"
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
    global _open
    if not ENABLED or not market_hours.is_us_regular_session():
        return []
    events: list[dict] = []

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
            daily = kis_us_api.get_us_daily_prices(symbol, exchange, days=80)
        except Exception as e:
            print(f"[US시뮬] {symbol} 조회 실패: {e}")
            continue
        if price <= 0 or not daily:
            continue

        # 형성 구간: ORB 레인지 축적 + S는 병행 허용
        if ENABLE_ORB:
            _update_orb(symbol, price, day_high, day_low)

        ok, reason, strategy = False, "", ""
        if not market_hours.is_orb_building(ORB_MINUTES):
            ok, reason = _match_orb(symbol, daily, price, today_vol, day_high, day_low)
            strategy = "ORB"
        if not ok:
            ok, reason = _match_s_rule(daily, price, today_vol)
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
    if ENABLE_S:
        rules.append(f"S(RVOL≥{MIN_RVOL:g})")
    if ENABLE_ORB:
        rules.append(f"ORB({ORB_MINUTES}m)")
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
