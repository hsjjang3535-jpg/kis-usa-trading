"""
미국주식 전용 자동매매 봇 (국내 kis-trading-bot과 분리)

스케줄 (KST / America/New_York):
  - 미국 정규장(ET 09:30~16:00) 중 N분마다 시뮬 체크
  - 장 종료 시 미청산 시뮬 강제청산
  - 실전 주문: ENABLE_US_LIVE_ORDERS=true 일 때만 (기본 OFF)
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import api_server
import kis_us_api
import market_hours
import notifier
import us_screener
import us_sim

load_dotenv()

KST = ZoneInfo("Asia/Seoul")
STATE_FILE = Path(__file__).resolve().parent / "trading_state.json"
POLL_MIN = int(os.getenv("US_POLL_INTERVAL_MIN", "5"))
LIVE_ORDERS = os.getenv("ENABLE_US_LIVE_ORDERS", "false").lower() == "true"

_last_ran: dict[str, str] = {}
_was_in_session = False
_last_screen_key = ""


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _save_state() -> None:
    state = {
        "date": _today_kst(),
        "us_sim": us_sim.dump_state(),
    }
    try:
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[상태 저장 오류] {e}")


def _load_state() -> None:
    if not STATE_FILE.exists():
        return
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        us_sim.load_state(state.get("us_sim"))
        if us_sim.get_open():
            pos = us_sim.get_open()
            print(f"[상태 복원] US 시뮬 보유 {pos['symbol']}")
    except Exception as e:
        print(f"[상태 복원 오류] {e}")


def _reset_if_new_day() -> None:
    today = _today_kst()
    if _last_ran.get("date") and _last_ran["date"] != today:
        us_sim.reset_daily()
        _save_state()
        print(f"[일별 초기화] {today}")
    _last_ran["date"] = today


def _run_screen(force: bool = False, *, notify: bool = False) -> None:
    """동적 후보 갱신 (국내 screener와 동일 역할)."""
    global _last_screen_key
    try:
        wl = us_screener.run_screening(force=force)
        stats = us_screener.get_last_stats()
        key = "|".join(f"{w['exchange']}:{w['symbol']}" for w in wl)
        changed = key != _last_screen_key
        _last_screen_key = key
        if notify and (changed or force):
            mode = stats.get("mode", "?")
            preview = us_screener.format_watchlist_preview(8)
            notifier.send(
                f"🇺🇸 <b>US 스크리너</b> ({mode})\n"
                f"순위API→필터→TOP{us_screener.MAX_WATCH} · "
                f"등락≥{stats.get('min_rate', '?')}% · "
                f"메가제외 {stats.get('mega_excluded', '?')}\n"
                f"{preview}"
            )
    except Exception as e:
        print(f"[US스크리너] 오류: {e}")
        notifier.notify_error(f"US 스크리너 오류: {e}")


def _check_sim() -> None:
    if not us_sim.is_enabled():
        return
    try:
        events = us_sim.run_check()
        for ev in events:
            if ev.get("action") == "buy":
                notifier.notify_sim_buy(
                    ev["symbol"], ev["exchange"], ev["quantity"],
                    ev["price"], ev["reason"],
                )
            elif ev.get("action") == "sell":
                notifier.notify_sim_sell(
                    ev["symbol"], ev["exchange"], ev["quantity"],
                    ev["buy_price"], ev["sell_price"],
                    ev["profit_pct"], ev["sell_reason"],
                )
        if events:
            _save_state()
    except Exception as e:
        print(f"[US시뮬] 오류: {e}")
        notifier.notify_error(f"US 시뮬 오류: {e}")


def _session_end_close() -> None:
    pos = us_sim.get_open()
    if not pos:
        return
    try:
        px = kis_us_api.get_us_price(pos["symbol"], pos["exchange"])
        price = float(px["last"]) or float(pos["buy_price"])
    except Exception:
        price = float(pos["buy_price"])
    trade = us_sim.force_close(price, "정규장 종료 청산")
    if trade:
        notifier.notify_sim_sell(
            trade["symbol"], trade["exchange"], trade["quantity"],
            trade["buy_price"], trade["sell_price"],
            trade["profit_pct"], trade["sell_reason"],
        )
        _save_state()


def main() -> None:
    print("=== KIS 미국주식 봇 시작 (국내 봇과 분리) ===")
    _load_state()

    api_thread = threading.Thread(target=api_server.start_api_server, daemon=True)
    api_thread.start()

    try:
        acct = kis_us_api.get_account_info()
        acct_line = f"✅ 계좌 {acct.get('masked')} / 모드 {acct.get('mode')}"
    except Exception as e:
        acct_line = f"⚠️ 계좌: {e}"

    session = market_hours.session_label()
    dyn = "동적" if us_screener.DYNAMIC else "고정"
    _run_screen(force=True, notify=False)
    preview = us_screener.format_watchlist_preview(8)
    stats = us_screener.get_last_stats()
    notifier.send(
        f"🇺🇸 <b>미국주식 봇 시작</b> — {market_hours.now_kst().strftime('%Y-%m-%d %H:%M')} KST\n"
        f"{acct_line}\n"
        f"세션: {session} (NY {market_hours.now_ny().strftime('%H:%M')})\n"
        f"워치({dyn}/{stats.get('mode', '?')} TOP{us_screener.MAX_WATCH}): {preview}\n"
        f"규칙: S(RVOL)={'ON' if us_sim.ENABLE_S else 'OFF'} / "
        f"ORB={'ON' if us_sim.ENABLE_ORB else 'OFF'}({us_sim.ORB_MINUTES}m)\n"
        f"시뮬: {'ON' if us_sim.is_enabled() else 'OFF'} / "
        f"실전주문: {'ON' if LIVE_ORDERS else 'OFF (기본)'}\n"
        f"점검 주기: {POLL_MIN}분 · 스크린 {us_screener.SCREEN_INTERVAL_MIN}분\n"
        f"⚠️ 국내 kis-trading-bot과 별도 Railway 서비스"
    )

    global _was_in_session
    _was_in_session = market_hours.is_us_regular_session()
    last_poll_min = -1
    session_open_notified = False

    while True:
        _reset_if_new_day()
        in_session = market_hours.is_us_regular_session()
        now = market_hours.now_kst()
        tmin = now.hour * 60 + now.minute

        # 정규장 종료 감지 → 강제청산
        if _was_in_session and not in_session:
            print("[세션] 정규장 종료 → 시뮬 청산 점검")
            _session_end_close()
            session_open_notified = False
        # 정규장 개시 → 강제 스크린 + 알림
        if in_session and not _was_in_session:
            _run_screen(force=True, notify=True)
            session_open_notified = True
        _was_in_session = in_session

        if in_session:
            if last_poll_min < 0 or tmin - last_poll_min >= POLL_MIN or tmin < last_poll_min:
                last_poll_min = tmin
                # 주기 스크린 — 후보 바뀌면만 텔레그램
                _run_screen(force=False, notify=True)
                session_open_notified = True
                _check_sim()
                if LIVE_ORDERS:
                    print("[실전주문] ENABLE_US_LIVE_ORDERS=true — 실주문 로직은 추후 연결")

        # 하트비트 (KST 지정 시각, 1회/일)
        hb = int(os.getenv("US_HEARTBEAT_HOUR_KST", "22"))
        if now.hour == hb and now.minute < 5 and _last_ran.get("heartbeat") != _today_kst():
            _last_ran["heartbeat"] = _today_kst()
            lines = [
                f"🇺🇸 US봇 하트비트 ({now.strftime('%H:%M')} KST)",
                f"세션: {market_hours.session_label()}",
                f"워치: {us_screener.format_watchlist_preview(8)}",
            ]
            lines.extend(us_sim.format_summary() or ["시뮬 포지션/체결 없음"])
            notifier.send("\n".join(lines))

        time.sleep(20)


if __name__ == "__main__":
    main()
