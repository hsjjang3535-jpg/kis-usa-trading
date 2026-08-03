"""
미국주식 일봉 시뮬 (실제 주문 없음)

S규칙(거래량 급증):
  전일대비 +3%↑ · 거래량 ≥ 20일평균×2 · 종가 ≥ MA20 · RSI ≤ 75
청산: -2% 손절 / +3% 후 트레일 -0.6% (장중 현재가 기준)
"""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import kis_us_api
import market_hours

KST = ZoneInfo("Asia/Seoul")

ENABLED = os.getenv("ENABLE_US_SIM", "true").lower() == "true"
SIM_AMOUNT_USD = float(os.getenv("US_SIM_AMOUNT_USD", "500"))
STOP_LOSS_PCT = float(os.getenv("US_SIM_STOP_LOSS_PCT", "2.0"))
TAKE_PROFIT_PCT = float(os.getenv("US_SIM_TAKE_PROFIT_PCT", "3.0"))
TRAILING_STOP_PCT = float(os.getenv("US_SIM_TRAILING_STOP_PCT", "0.6"))
MIN_DAY_PCT = float(os.getenv("US_SIM_MIN_DAY_PCT", "3.0"))
MIN_VOL_RATIO = float(os.getenv("US_SIM_MIN_VOL_RATIO", "2.0"))
VOL_AVG_DAYS = int(os.getenv("US_SIM_VOL_AVG_DAYS", "20"))
MAX_RSI = float(os.getenv("US_SIM_MAX_RSI", "75"))

_open: dict | None = None
_trades_today: list[dict] = []
_bought_symbols_today: set[str] = set()


def _env_watchlist() -> list[tuple[str, str]]:
    raw = os.getenv("US_WATCHLIST", "AAPL:NAS,MSFT:NAS,NVDA:NAS")
    items: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            sym, ex = part.split(":", 1)
            items.append((sym.strip().upper(), ex.strip().upper() or "NAS"))
        else:
            items.append((part.upper(), "NAS"))
    return items


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
        "date": datetime.now(KST).strftime("%Y-%m-%d"),
    }


def load_state(data: dict | None) -> None:
    global _open, _trades_today, _bought_symbols_today
    if not isinstance(data, dict):
        return
    today = datetime.now(KST).strftime("%Y-%m-%d")
    _open = data.get("open") if isinstance(data.get("open"), dict) else None
    if data.get("date") == today:
        _trades_today = list(data.get("trades_today") or [])
        _bought_symbols_today = set(data.get("bought_symbols_today") or [])
    else:
        _trades_today = []
        _bought_symbols_today = set()


def reset_daily() -> None:
    global _open, _trades_today, _bought_symbols_today
    _open = None
    _trades_today = []
    _bought_symbols_today = set()


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


def _match_s_rule(daily: list[dict], current: float) -> tuple[bool, str]:
    if len(daily) < max(60, VOL_AVG_DAYS + 2):
        return False, ""
    # daily is latest-first from API
    closes = [current] + [b["close"] for b in daily[1:]]
    volumes = [float(daily[0].get("volume") or 0)] + [b["volume"] for b in daily[1:]]
    # Prefer live volume from today's bar if present; else use current day proxy from last bar
    today_vol = volumes[0] if volumes[0] > 0 else (daily[0]["volume"] if daily else 0)
    prev_close = daily[1]["close"] if len(daily) > 1 else 0
    if prev_close <= 0:
        return False, ""
    day_pct = (current - prev_close) / prev_close * 100
    if day_pct < MIN_DAY_PCT:
        return False, ""
    ma20 = sum(closes[:20]) / 20
    if current < ma20:
        return False, ""
    rsi = _rsi(closes, 14)
    if rsi > MAX_RSI:
        return False, ""
    prior_vols = [b["volume"] for b in daily[1 : VOL_AVG_DAYS + 1]]
    vol_avg = sum(prior_vols) / len(prior_vols) if prior_vols else 0
    # During session today_vol may still be building — use last completed day vol ratio
    # against prior average as fallback when today's volume incomplete
    check_vol = today_vol if today_vol > 0 else daily[0]["volume"]
    vol_ratio = check_vol / vol_avg if vol_avg > 0 else 0
    if vol_ratio < MIN_VOL_RATIO:
        return False, ""
    return True, (
        f"S 거래량급증 · {day_pct:+.1f}% · 거래량 {vol_ratio:.1f}배 · "
        f"MA20 ${ma20:.2f} · RSI {rsi:.0f}"
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
    # 정규장 종료 직전 청산은 trader에서 세션 종료 시 호출
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

    for symbol, exchange in _env_watchlist():
        if symbol in _bought_symbols_today:
            continue
        try:
            px = kis_us_api.get_us_price(symbol, exchange)
            price = float(px["last"])
            daily = kis_us_api.get_us_daily_prices(symbol, exchange, days=80)
        except Exception as e:
            print(f"[US시뮬] {symbol} 조회 실패: {e}")
            continue
        if price <= 0 or not daily:
            continue
        ok, reason = _match_s_rule(daily, price)
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
        }
        _bought_symbols_today.add(symbol)
        events.append({
            "action": "buy",
            "symbol": symbol,
            "exchange": exchange,
            "quantity": qty,
            "price": price,
            "reason": reason,
        })
        break  # 1포지션
    return events


def format_summary() -> list[str]:
    lines: list[str] = []
    if _trades_today:
        lines.append(f"🇺🇸 US 시뮬 오늘 체결 {len(_trades_today)}건")
        for t in _trades_today:
            s = "+" if t["profit_pct"] >= 0 else ""
            lines.append(
                f"  {t['symbol']} ${t['buy_price']:.2f}→${t['sell_price']:.2f} "
                f"{s}{t['profit_pct']}%"
            )
    if _open:
        lines.append(
            f"🇺🇸 US 시뮬 보유 {_open['symbol']} "
            f"${_open['buy_price']:.2f} × {_open['quantity']}"
        )
    return lines
