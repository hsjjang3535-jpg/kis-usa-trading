"""
미국주식 시뮬 + 방식A 실전(옵션)

진입 우선순위:
  1) Gap & Go — 시가갭 + RVOL + ORB고 돌파 + VWAP 위
  2) ORB — 개장 N분 레인지 고가 돌파 + RVOL + VWAP 위
  3) S·RVOL — 일봉 모멘텀 + 시간보정 RVOL + VWAP 위

청산: -2% 손절 / +6% 익절 (하드, 왕복수수료~0.5% 반영) / 정규장 종료 강제청산
보유 포지션은 진입 스캔과 별도로 더 자주 점검 가능 (US_EXIT_POLL_SEC).
매수 시 원/달러 환율 기록 → 매도 알림에서 환전 보류 여부 안내.

방식 A: 같은 점검에서 조건 통과 종목 중 신호점수 순으로
세션 실주문(US_LIVE_MAX_POSITIONS, 기본 3회). 나머지는 병렬 시뮬 (US_PARALLEL_SIM).
동시 실전 보유는 US_MAX_TOTAL_USD(기본 $1500)로 약 2종.
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
# 실전 총 투자 한도 ≈200만원
MAX_TOTAL_USD = float(os.getenv("US_MAX_TOTAL_USD", "1500"))
# 세션당 실전 진입 최대 횟수 (손절 후 재진입 포함). 동시 보유는 총한도≈2종
LIVE_MAX_POSITIONS = int(os.getenv("US_LIVE_MAX_POSITIONS", "3"))
STOP_LOSS_PCT = float(os.getenv("US_SIM_STOP_LOSS_PCT", "2.0"))
# 왕복 수수료 ~0.5%(매수·매도 각 0.25%) 반영 → 기본 익절 +6%
TAKE_PROFIT_PCT = float(os.getenv("US_SIM_TAKE_PROFIT_PCT", "6.0"))
# 편도 수수료 %(매수·매도 각각). 순손익 표시·알림용
FEE_ONE_WAY_PCT = float(os.getenv("US_FEE_ONE_WAY_PCT", "0.25"))
MIN_DAY_PCT = float(os.getenv("US_SIM_MIN_DAY_PCT", "3.0"))
MIN_RVOL = float(os.getenv("US_SIM_MIN_RVOL", os.getenv("US_SIM_MIN_VOL_RATIO", "2.5")))
VOL_AVG_DAYS = int(os.getenv("US_SIM_VOL_AVG_DAYS", "20"))
MAX_RSI = float(os.getenv("US_SIM_MAX_RSI", "75"))

ENABLE_S = os.getenv("ENABLE_US_S_RULE", "true").lower() == "true"
ENABLE_ORB = os.getenv("ENABLE_US_ORB", "true").lower() == "true"
ENABLE_GAP_GO = os.getenv("ENABLE_US_GAP_GO", "true").lower() == "true"
REQUIRE_ABOVE_VWAP = os.getenv("US_REQUIRE_ABOVE_VWAP", "true").lower() == "true"
VWAP_NMIN = int(os.getenv("US_VWAP_NMIN", "5"))

ORB_MINUTES = int(os.getenv("US_ORB_MINUTES", "10"))
ORB_ENTRY_UNTIL = int(os.getenv("US_ORB_ENTRY_UNTIL_MIN", "120"))
ORB_MIN_RVOL = float(os.getenv("US_ORB_MIN_RVOL", "1.5"))
ORB_MIN_DAY_PCT = float(os.getenv("US_ORB_MIN_DAY_PCT", "1.0"))

GAP_MIN_PCT = float(os.getenv("US_GAP_MIN_PCT", "2.5"))
GAP_MAX_PCT = float(os.getenv("US_GAP_MAX_PCT", "15.0"))
GAP_MIN_RVOL = float(os.getenv("US_GAP_MIN_RVOL", "2.0"))
GAP_ENTRY_UNTIL = int(os.getenv("US_GAP_ENTRY_UNTIL_MIN", "90"))

PARALLEL_SIM = os.getenv("US_PARALLEL_SIM", "true").lower() == "true"
MAX_SIM_POSITIONS = int(os.getenv("US_MAX_SIM_POSITIONS", "5"))
SKIP_NOTIFY_INTERVAL_MIN = int(os.getenv("US_SKIP_NOTIFY_INTERVAL_MIN", "120"))

_open: dict | None = None  # 주포지션 (실전 가능)
_paper: dict[str, dict] = {}  # 병렬 시뮬 symbol -> pos
_trades_today: list[dict] = []
_bought_symbols_today: set[str] = set()
_live_entries_today = 0
_orb_ranges: dict[str, dict] = {}
_vwap_cache: dict[str, float] = {}
_last_skips: list[dict] = []
_last_skip_notify_at: datetime | None = None
_latest_skip_by_symbol: dict[str, str] = {}


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


def has_open_positions() -> bool:
    return _open is not None or bool(_paper)


def get_trades_today() -> list[dict]:
    return list(_trades_today)


def dump_state() -> dict:
    return {
        "open": dict(_open) if _open else None,
        "paper": {k: dict(v) for k, v in _paper.items()},
        "trades_today": list(_trades_today),
        "bought_symbols_today": sorted(_bought_symbols_today),
        "live_entries_today": _live_entries_today,
        "orb_ranges": dict(_orb_ranges),
        "date": market_hours.trading_day_ny(),
    }


def load_state(data: dict | None) -> None:
    global _open, _paper, _trades_today, _bought_symbols_today, _orb_ranges, _live_entries_today
    if not isinstance(data, dict):
        return
    saved_day = data.get("date")
    today = market_hours.trading_day_ny()
    last_done = market_hours.last_completed_session_day()
    keep = saved_day in (today, last_done)
    _open = data.get("open") if isinstance(data.get("open"), dict) else None
    raw_paper = data.get("paper") if isinstance(data.get("paper"), dict) else {}
    _paper = {k: dict(v) for k, v in raw_paper.items() if isinstance(v, dict)}
    if keep:
        _trades_today = list(data.get("trades_today") or [])
        _bought_symbols_today = set(data.get("bought_symbols_today") or [])
        raw_live = data.get("live_entries_today")
        if raw_live is not None:
            _live_entries_today = int(raw_live)
        elif data.get("live_used_today"):
            _live_entries_today = 1
        else:
            _live_entries_today = 0
        raw_orb = data.get("orb_ranges") or {}
        _orb_ranges = dict(raw_orb) if isinstance(raw_orb, dict) else {}
    else:
        _trades_today = []
        _bought_symbols_today = set()
        _live_entries_today = 0
        _orb_ranges = {}
        _paper = {}
        _open = None


def reset_daily() -> None:
    global _open, _paper, _trades_today, _bought_symbols_today, _orb_ranges
    global _vwap_cache, _live_entries_today, _last_skips, _last_skip_notify_at
    global _latest_skip_by_symbol
    _open = None
    _paper = {}
    _trades_today = []
    _bought_symbols_today = set()
    _live_entries_today = 0
    _orb_ranges = {}
    _vwap_cache = {}
    _last_skips = []
    _last_skip_notify_at = None
    _latest_skip_by_symbol = {}


def live_slots_remaining() -> int:
    """세션 남은 실전 진입 슬롯."""
    if not LIVE_ORDERS:
        return 0
    return max(0, LIVE_MAX_POSITIONS - _live_entries_today)


def live_slot_available() -> bool:
    """실전 슬롯 남음 + 총한도 여유."""
    if live_slots_remaining() <= 0:
        return False
    return _live_budget_remaining() > 0


def _want_live_for_price(price: float) -> bool:
    if not live_slot_available():
        return False
    return _live_budget_remaining() >= price


def mark_live_used() -> None:
    global _live_entries_today
    _live_entries_today += 1


def downgrade_live_to_sim(symbol: str) -> None:
    """실주문 실패 시 해당 포지션만 시뮬로 전환."""
    global _open
    if _open and _open.get("symbol") == symbol:
        _open["is_live"] = False
        return
    pos = _paper.get(symbol)
    if pos:
        pos["is_live"] = False


def _live_exposed() -> float:
    """현재 실전 포지션 평가액(USD) — 주포지션 + 병렬 실전."""
    total = 0.0
    if _open and _open.get("is_live"):
        total += float(_open["buy_price"]) * int(_open["quantity"])
    for pos in _paper.values():
        if pos.get("is_live"):
            total += float(pos["buy_price"]) * int(pos["quantity"])
    return total


def _signal_score(strategy: str, *, gap: float = 0.0, day_pct: float = 0.0, rvol: float = 0.0) -> float:
    """같은 점검 내 실전 후보 순위. 전략 베이스 + 모멘텀/거래량."""
    base = {"GapGo": 300.0, "ORB": 200.0, "S": 100.0}.get(strategy, 0.0)
    if strategy == "GapGo":
        return base + gap * 10.0 + rvol * 20.0
    if strategy == "ORB":
        return base + day_pct * 10.0 + rvol * 20.0
    return base + day_pct * 5.0 + rvol * 10.0


def _live_budget_remaining() -> float:
    return max(0.0, MAX_TOTAL_USD - _live_exposed())


def _record_skip(symbol: str, reason: str) -> None:
    _last_skips.append({
        "symbol": symbol,
        "reason": reason,
        "at": market_hours.now_ny().strftime("%H:%M"),
    })
    _latest_skip_by_symbol[symbol] = reason
    print(f"[US스킵] {symbol}: {reason}")


def _trade_pnl_usd(trade: dict) -> float:
    qty = int(trade["quantity"])
    return (float(trade["sell_price"]) - float(trade["buy_price"])) * qty


def compute_pnl_summary() -> dict:
    live_trades = [t for t in _trades_today if t.get("is_live")]
    sim_trades = [t for t in _trades_today if not t.get("is_live")]

    def _bucket(trades: list[dict]) -> dict:
        pnl = sum(_trade_pnl_usd(t) for t in trades)
        return {
            "count": len(trades),
            "pnl_usd": round(pnl, 2),
            "wins": sum(1 for t in trades if _trade_pnl_usd(t) > 0),
        }

    return {"live": _bucket(live_trades), "sim": _bucket(sim_trades)}


def get_latest_skip_lines(max_items: int = 8) -> list[str]:
    items = sorted(_latest_skip_by_symbol.items())[:max_items]
    lines = [f"  · {sym}: {why}" for sym, why in items]
    extra = len(_latest_skip_by_symbol) - max_items
    if extra > 0:
        lines.append(f"  … 외 {extra}종")
    return lines


def should_notify_skips() -> bool:
    """주기적으로만. 첫 점검은 타이머만 시작하고 바로 보내지 않음."""
    global _last_skip_notify_at
    if not _last_skips:
        return False
    now = datetime.now(KST)
    if _last_skip_notify_at is None:
        _last_skip_notify_at = now
        return False
    return (now - _last_skip_notify_at).total_seconds() >= SKIP_NOTIFY_INTERVAL_MIN * 60


def peek_skip_digest() -> list[str]:
    if not _last_skips:
        return []
    by_sym: dict[str, str] = {}
    for s in _last_skips:
        by_sym[s["symbol"]] = s["reason"]
    return [f"· {sym}: {why}" for sym, why in sorted(by_sym.items())]


def mark_skips_consumed() -> None:
    global _last_skips, _last_skip_notify_at
    _last_skips = []
    _last_skip_notify_at = datetime.now(KST)


def consume_skip_digest() -> list[str]:
    """심볼별 마지막 스킵 사유 요약 후 버퍼 비움."""
    lines = peek_skip_digest()
    if lines:
        mark_skips_consumed()
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
) -> tuple[bool, str, float]:
    if not ENABLE_S:
        return False, "S오프", 0.0
    if len(daily) < max(60, VOL_AVG_DAYS + 2):
        return False, "S:일봉부족", 0.0
    closes = [current] + [b["close"] for b in daily[1:]]
    day_pct = _day_pct(daily, current)
    if day_pct < MIN_DAY_PCT:
        return False, f"S:당일{day_pct:+.1f}% 미만{MIN_DAY_PCT:g}%", 0.0
    ma20 = sum(closes[:20]) / 20
    if current < ma20:
        return False, f"S:MA20아래(${ma20:.2f})", 0.0
    rsi = _rsi(closes, 14)
    if rsi > MAX_RSI:
        return False, f"S:RSI{rsi:.0f} 초과{MAX_RSI:g}", 0.0
    check_vol = today_vol if today_vol > 0 else float(daily[0].get("volume") or 0)
    rvol = _rvol(daily, check_vol)
    if rvol < MIN_RVOL:
        return False, f"S:RVOL{rvol:.1f}x 미만{MIN_RVOL:g}", 0.0
    ok_vwap, vwap = _above_vwap(symbol, exchange, current)
    if not ok_vwap:
        return False, f"S:VWAP아래(${vwap:.2f})", 0.0
    vwap_txt = f" · VWAP ${vwap:.2f}" if vwap > 0 else ""
    score = _signal_score("S", day_pct=day_pct, rvol=rvol)
    return True, (
        f"S·RVOL · {day_pct:+.1f}% · RVOL {rvol:.1f}x · "
        f"MA20 ${ma20:.2f} · RSI {rsi:.0f}{vwap_txt}"
    ), score


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
) -> tuple[bool, str, float]:
    if not ENABLE_ORB:
        return False, "ORB오프", 0.0
    if not market_hours.is_orb_entry_window(ORB_MINUTES, ORB_ENTRY_UNTIL):
        return False, "ORB:시간외", 0.0
    st = _update_orb(symbol, price, day_high, day_low)
    if not st or not st.get("ready"):
        return False, "ORB:레인지미확정", 0.0
    or_high = float(st["high"])
    if or_high <= 0 or price < or_high:
        return False, f"ORB:미돌파(OR고${or_high:.2f})", 0.0
    day_pct = _day_pct(daily, price)
    if day_pct < ORB_MIN_DAY_PCT:
        return False, f"ORB:당일{day_pct:+.1f}% 미만{ORB_MIN_DAY_PCT:g}%", 0.0
    rvol = _rvol(daily, today_vol if today_vol > 0 else float(daily[0].get("volume") or 0))
    if rvol < ORB_MIN_RVOL:
        return False, f"ORB:RVOL{rvol:.1f}x 미만{ORB_MIN_RVOL:g}", 0.0
    ok_vwap, vwap = _above_vwap(symbol, exchange, price)
    if not ok_vwap:
        return False, f"ORB:VWAP아래(${vwap:.2f})", 0.0
    vwap_txt = f" · VWAP ${vwap:.2f}" if vwap > 0 else ""
    score = _signal_score("ORB", day_pct=day_pct, rvol=rvol)
    return True, (
        f"ORB {ORB_MINUTES}m돌파 · OR고 ${or_high:.2f} · "
        f"{day_pct:+.1f}% · RVOL {rvol:.1f}x{vwap_txt}"
    ), score


def _match_gap_and_go(
    symbol: str,
    exchange: str,
    daily: list[dict],
    price: float,
    open_px: float,
    today_vol: float,
    day_high: float,
    day_low: float,
) -> tuple[bool, str, float]:
    """
    Gap & Go: 시가갭 구간 + RVOL + 갭 유지(price≥open) + ORB고 돌파 + VWAP 위.
    """
    if not ENABLE_GAP_GO:
        return False, "GapGo오프", 0.0
    if not market_hours.is_orb_entry_window(ORB_MINUTES, GAP_ENTRY_UNTIL):
        return False, "GapGo:시간외", 0.0
    gap = _gap_pct(daily, open_px if open_px > 0 else price)
    if gap < GAP_MIN_PCT or gap > GAP_MAX_PCT:
        return False, f"GapGo:갭{gap:+.1f}%범위외", 0.0
    if open_px > 0 and price < open_px:
        return False, "GapGo:갭페이드", 0.0
    rvol = _rvol(daily, today_vol if today_vol > 0 else float(daily[0].get("volume") or 0))
    if rvol < GAP_MIN_RVOL:
        return False, f"GapGo:RVOL{rvol:.1f}x 미만{GAP_MIN_RVOL:g}", 0.0
    st = _update_orb(symbol, price, day_high, day_low)
    if not st or not st.get("ready"):
        return False, "GapGo:ORB미확정", 0.0
    or_high = float(st["high"])
    if or_high <= 0 or price < or_high:
        return False, f"GapGo:OR미돌파(${or_high:.2f})", 0.0
    ok_vwap, vwap = _above_vwap(symbol, exchange, price)
    if not ok_vwap:
        return False, f"GapGo:VWAP아래(${vwap:.2f})", 0.0
    vwap_txt = f" · VWAP ${vwap:.2f}" if vwap > 0 else ""
    score = _signal_score("GapGo", gap=gap, rvol=rvol)
    return True, (
        f"Gap&Go · 갭 {gap:+.1f}% · OR고 ${or_high:.2f} · "
        f"RVOL {rvol:.1f}x{vwap_txt}"
    ), score


def _qty(price: float, *, live: bool) -> int:
    if price <= 0:
        return 0
    if live:
        budget = min(LIVE_AMOUNT_USD, _live_budget_remaining())
    else:
        budget = SIM_AMOUNT_USD
    if budget < price:
        return 0
    return max(int(budget // price), 1)


def _fee_round_trip_pct() -> float:
    return FEE_ONE_WAY_PCT * 2.0


def net_pct_after_fees(gross_pct: float) -> float:
    """주식 수익률에서 왕복 수수료를 뺀 순수익률(대략)."""
    return round(gross_pct - _fee_round_trip_pct(), 2)


def fx_hold_advice(buy_fx: float | None, sell_fx: float | None) -> str:
    """매수·매도 시점 환율 비교 → 원화 환전 보류 안내."""
    if not buy_fx or buy_fx <= 0 or not sell_fx or sell_fx <= 0:
        return (
            "💱 환율 미확인 — 매도 대금은 달러로 두고, "
            "매수 때보다 환율 낮으면 원화환전 보류"
        )
    diff_pct = (sell_fx - buy_fx) / buy_fx * 100
    if sell_fx < buy_fx:
        return (
            f"💱 매수환율 {buy_fx:,.1f} → 현재 {sell_fx:,.1f} "
            f"({diff_pct:+.2f}%)\n"
            f"⚠️ 매수 때보다 환율 낮음 → <b>원화 환전 보류, 달러 보유</b> "
            f"(다음 매수에 재사용)"
        )
    if sell_fx > buy_fx * 1.001:
        return (
            f"💱 매수환율 {buy_fx:,.1f} → 현재 {sell_fx:,.1f} "
            f"({diff_pct:+.2f}%)\n"
            f"✅ 환율 유리 — 원화 필요 시 환전 검토 가능 "
            f"(다음 매매 예정이면 달러 유지 권장)"
        )
    return (
        f"💱 매수환율 {buy_fx:,.1f} → 현재 {sell_fx:,.1f} "
        f"({diff_pct:+.2f}%)\n"
        f"💵 환율 비슷 — <b>달러 유지</b> 권장 (환전·재환전 비용 절약)"
    )


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
    buy_fx = pos.get("buy_fx")
    try:
        buy_fx_f = float(buy_fx) if buy_fx else None
    except (TypeError, ValueError):
        buy_fx_f = None
    sell_fx = None
    try:
        sell_fx = kis_us_api.get_usd_krw_rate()
    except Exception as e:
        print(f"[환율] 매도 시 조회 실패: {e}")
    return {
        "action": "sell",
        "symbol": pos["symbol"],
        "exchange": pos["exchange"],
        "quantity": qty,
        "buy_price": buy,
        "sell_price": price,
        "profit_pct": round(pct, 2),
        "net_pct": net_pct_after_fees(pct),
        "buy_fx": buy_fx_f,
        "sell_fx": sell_fx,
        "fx_advice": fx_hold_advice(buy_fx_f, sell_fx),
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
) -> tuple[bool, str, str, float]:
    """(ok, reason, strategy, score). 실패 시 reason=스킵 사유."""
    building = market_hours.is_orb_building(ORB_MINUTES)
    skips: list[str] = []

    if not building:
        ok, reason, score = _match_gap_and_go(
            symbol, exchange, daily, price, open_px, today_vol, day_high, day_low,
        )
        if ok:
            return True, reason, "GapGo", score
        if reason:
            skips.append(reason)
        ok, reason, score = _match_orb(
            symbol, exchange, daily, price, today_vol, day_high, day_low,
        )
        if ok:
            return True, reason, "ORB", score
        if reason:
            skips.append(reason)
    else:
        skips.append("ORB형성중")

    ok, reason, score = _match_s_rule(
        daily, price, today_vol, symbol=symbol, exchange=exchange,
    )
    if ok:
        return True, reason, "S", score
    if reason:
        skips.append(reason)
    return False, " / ".join(skips) if skips else "조건미충족", "", 0.0


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
    qty = _qty(price, live=live)
    buy_fx = None
    try:
        buy_fx = kis_us_api.get_usd_krw_rate()
    except Exception as e:
        print(f"[환율] 매수 시 조회 실패: {e}")
    pos = {
        "symbol": symbol,
        "exchange": exchange,
        "quantity": qty,
        "buy_price": price,
        "peak_price": price,
        "buy_reason": reason,
        "strategy": strategy,
        "is_live": live,
        "paper": paper,
        "buy_fx": buy_fx,
    }
    _bought_symbols_today.add(symbol)
    return pos


def run_exit_check() -> list[dict]:
    """보유 포지션만 손절·익절 점검. 신규 진입 스캔 없음."""
    global _open, _paper
    if not ENABLED or not market_hours.is_us_regular_session():
        return []
    if not _open and not _paper:
        return []
    events: list[dict] = []

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
    return events


def run_check() -> list[dict]:
    """정규장 중 호출. 청산 + 신규 진입. 이벤트 리스트 반환."""
    global _open, _vwap_cache, _paper
    if not ENABLED or not market_hours.is_us_regular_session():
        return []
    events = run_exit_check()
    _vwap_cache = {}
    try:
        import api_server as _api
        _touch = _api.touch_loop
    except Exception:
        _touch = lambda: None

    # ── 신규 스캔: 전부 평가 후 점수순 배정 ─────────────────────
    held = set()
    if _open:
        held.add(_open["symbol"])
    held |= set(_paper.keys())

    primary_free = _open is None
    paper_slots = MAX_SIM_POSITIONS - len(_paper)
    candidates: list[dict] = []

    for symbol, exchange in _active_watchlist():
        _touch()
        if symbol in _bought_symbols_today or symbol in held:
            continue
        if us_screener.is_mega_cap(symbol):
            _record_skip(symbol, "메가캡제외")
            continue
        # 워치에 name이 있으면 ETP 이름 필터 적용
        wname = ""
        for w in us_screener.get_watchlist():
            if w.get("symbol") == symbol:
                wname = w.get("name") or ""
                break
        if us_screener.is_etp(symbol, wname):
            _record_skip(symbol, "ETP제외(일반주만)")
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

        ok, reason, strategy, score = _try_entry_signal(
            symbol, exchange, daily, price, open_px, today_vol, day_high, day_low,
        )
        if not ok:
            _record_skip(symbol, reason)
            continue
        candidates.append({
            "symbol": symbol,
            "exchange": exchange,
            "price": price,
            "reason": reason,
            "strategy": strategy,
            "score": score,
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)

    for c in candidates:
        symbol = c["symbol"]
        exchange = c["exchange"]
        price = c["price"]
        strategy = c["strategy"]
        score = c["score"]
        reason = f"{c['reason']} · 점수{score:.0f}"

        # 점수 최고(남은 후보 중) → 주포지션. 실전 슬롯 있으면 실주문.
        if primary_free:
            want_live = _want_live_for_price(price) and not us_screener.is_etp(symbol)
            pos = _open_position(
                symbol=symbol, exchange=exchange, price=price,
                reason=reason, strategy=strategy, live=want_live, paper=False,
            )
            if pos["quantity"] < 1:
                _record_skip(symbol, "수량0(한도부족)")
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
                "score": score,
                "is_live": want_live,
                "paper": False,
                "buy_fx": pos.get("buy_fx"),
            })
            continue

        # 주포지션 이미 있음 → 병렬 (실전 슬롯 있으면 실전, 아니면 시뮬)
        want_live = _want_live_for_price(price) and not us_screener.is_etp(symbol)
        if not PARALLEL_SIM and not want_live:
            _record_skip(
                symbol,
                f"신호OK·점수{score:.0f}·주포지션보유({_open and _open.get('symbol')})",
            )
            continue
        if not want_live and paper_slots <= 0:
            _record_skip(symbol, f"신호OK·점수{score:.0f}·시뮬한도초과")
            continue
        reason_suffix = " (병렬실전)" if want_live else " (병렬시뮬)"
        pos = _open_position(
            symbol=symbol, exchange=exchange, price=price,
            reason=reason + reason_suffix, strategy=strategy,
            live=want_live, paper=True,
        )
        if pos["quantity"] < 1:
            _record_skip(symbol, "수량0")
            continue
        _paper[symbol] = pos
        if not want_live:
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
            "score": score,
            "is_live": want_live,
            "paper": True,
            "buy_fx": pos.get("buy_fx"),
        })

    return events


def format_session_report(*, closing: bool = False, ny_day: str | None = None) -> list[str]:
    """중간/마감 보고 본문 (실시간 시세 제외)."""
    ny_day = ny_day or market_hours.trading_day_ny()
    now_ny = market_hours.now_ny()
    pnl = compute_pnl_summary()
    lines: list[str] = []

    if closing:
        lines.append(f"📋 <b>US 장마감 보고</b> ({ny_day} NY)")
    else:
        lines.append(f"📊 <b>US 중간 보고</b> (NY {now_ny.strftime('%H:%M')})")
    lines.append(
        f"NY {now_ny.strftime('%H:%M')} · 세션 {market_hours.session_label()}"
    )
    lines.append("")

    live = pnl["live"]
    sim = pnl["sim"]
    lines.append(
        f"💰 <b>실전 손익: ${live['pnl_usd']:+,.2f}</b> "
        f"({live['count']}건 · 승 {live['wins']})"
    )
    lines.append(
        f"🧪 <b>시뮬 손익: ${sim['pnl_usd']:+,.2f}</b> "
        f"({sim['count']}건 · 승 {sim['wins']})"
    )
    lines.append("")

    if _trades_today:
        lines.append(f"체결 {len(_trades_today)}건:")
        for t in _trades_today:
            s = "+" if t["profit_pct"] >= 0 else ""
            tag = t.get("strategy") or ""
            mode = "실전" if t.get("is_live") else "시뮬"
            pnl_usd = _trade_pnl_usd(t)
            lines.append(
                f"  [{mode}] {tag+' ' if tag else ''}{t['symbol']} "
                f"${t['buy_price']:.2f}→${t['sell_price']:.2f} "
                f"{s}{t['profit_pct']}% (${pnl_usd:+,.2f})"
            )
        lines.append("")
    elif closing:
        lines.append("매매 없음 (진입 조건 미충족)\n")

    open_n = (1 if _open else 0) + len(_paper)
    if open_n and closing:
        lines.append(f"마감 시점 미청산 {open_n}건")
        if _open:
            mode = "실전" if _open.get("is_live") else "시뮬"
            tag = _open.get("strategy") or ""
            lines.append(
                f"  [{mode}] {tag+' ' if tag else ''}{_open['symbol']} "
                f"${_open['buy_price']:.2f} × {_open['quantity']}"
            )
        for sym, pos in _paper.items():
            tag = pos.get("strategy") or ""
            mode = "실전" if pos.get("is_live") else "병렬시뮬"
            lines.append(
                f"  [{mode}] {tag+' ' if tag else ''}{sym} "
                f"${pos['buy_price']:.2f} × {pos['quantity']}"
            )
        lines.append("")

    if LIVE_ORDERS:
        rem = live_slots_remaining()
        slot = f"{_live_entries_today}/{LIVE_MAX_POSITIONS} ({'가능' if rem else '소진'})"
        lines.append(f"실전슬롯: {slot} · 총한도 ${MAX_TOTAL_USD:g}")
        lines.append("")

    skip_lines = get_latest_skip_lines()
    if skip_lines and (closing or not _trades_today):
        lines.append("미진입 사유 (최근):")
        lines.extend(skip_lines)
        lines.append("")

    return lines


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
    rules.append(f"수수료편도{FEE_ONE_WAY_PCT:g}%")
    if LIVE_ORDERS:
        rem = live_slots_remaining()
        rules.append(f"실전{rem}/{LIVE_MAX_POSITIONS}슬롯")
        rules.append(f"실전총한도${MAX_TOTAL_USD:g}")
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
        mode = "실전" if pos.get("is_live") else "병렬시뮬"
        lines.append(
            f"🇺🇸 US {mode} [{tag}] {sym} "
            f"${pos['buy_price']:.2f} × {pos['quantity']}"
        )
    return lines
