"""
미국주식 전용 자동매매 봇 (국내 kis-trading-bot과 분리)

스케줄 (KST / America/New_York):
  - 미국 정규장(ET 09:30~16:00) 중 N분마다 시뮬 체크
  - NY 12:00 이후 중간 보고 (세션 중 1회, 창이 아니라 이후 재시도)
  - 장 종료 시 강제청산 + 마감 보고 (주말 넘어서도 미발송이면 재시도)
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
MID_REPORT_HOUR_NY = int(os.getenv("US_MID_REPORT_HOUR_NY", "12"))

_last_ran: dict[str, str] = {}
_was_in_session = False
_last_screen_key = ""
_loop_errors = 0
_last_error_notify_at: datetime | None = None
_last_report_try: dict[str, datetime] = {}
REPORT_RETRY_SEC = 600


def _notify_loop_error(exc: Exception) -> None:
    """루프 오류 알림 — 스팸 방지 (30분에 1회)."""
    global _loop_errors, _last_error_notify_at
    _loop_errors += 1
    print(f"[메인루프 오류 #{_loop_errors}] {exc}")
    now = datetime.now(KST)
    if _last_error_notify_at is None or (now - _last_error_notify_at).total_seconds() >= 1800:
        _last_error_notify_at = now
        notifier.notify_error(
            f"US봇 메인루프 오류 (#{_loop_errors}) — 자동 재시도 중\n{exc}"
        )


def _run_loop_tick(
    *,
    last_poll_min: int,
    session_open_notified: bool,
) -> tuple[int, bool]:
    """메인 루프 1회. (last_poll_min, session_open_notified) 반환."""
    api_server.touch_loop()
    _reset_if_new_day()
    in_session = market_hours.is_us_regular_session()
    now = market_hours.now_kst()
    tmin = now.hour * 60 + now.minute

    global _was_in_session
    if _was_in_session and not in_session:
        print("[세션] 정규장 종료 → 시뮬 청산 점검")
        _session_end_close()
        session_open_notified = False
    if in_session and not _was_in_session:
        notifier.send(
            f"🇺🇸 <b>정규장 시작</b> — 시뮬 점검 가동\n"
            f"NY {market_hours.now_ny().strftime('%H:%M')} / "
            f"KST {now.strftime('%H:%M')}"
        )
        _run_screen(force=True, notify=True)
        session_open_notified = True
        last_poll_min = tmin
        _check_sim()
    _was_in_session = in_session

    if not in_session:
        _maybe_closing_report_backup()

    if in_session:
        _maybe_mid_report()
        if last_poll_min < 0 or tmin - last_poll_min >= POLL_MIN or tmin < last_poll_min:
            last_poll_min = tmin
            _run_screen(force=False, notify=True)
            session_open_notified = True
            _check_sim()

    hb = int(os.getenv("US_HEARTBEAT_HOUR_KST", "22"))
    if now.hour == hb and now.minute < 5 and _last_ran.get("heartbeat") != _today_kst():
        _last_ran["heartbeat"] = _today_kst()
        ny = market_hours.now_ny()
        lines = [
            f"🇺🇸 US봇 하트비트 ({now.strftime('%H:%M')} KST)",
            f"세션: {market_hours.session_label(ny)}",
            f"NY {ny.strftime('%Y-%m-%d %H:%M')} ({market_hours.ny_weekday_label(ny)})",
            f"워치: {us_screener.format_watchlist_preview(8)}",
        ]
        lines.extend(us_sim.format_summary() or ["시뮬 포지션/체결 없음"])
        notifier.send("\n".join(lines))

    return last_poll_min, session_open_notified


def _report_sent(kind: str, ny_day: str | None = None) -> bool:
    day = ny_day or _trading_day()
    return _last_ran.get(f"{kind}_report") == day


def _mark_report_sent(kind: str, ny_day: str | None = None) -> None:
    _last_ran[f"{kind}_report"] = ny_day or _trading_day()


def _report_retry_ok(kind: str) -> bool:
    """전송 실패 시 10분에 1회만 재시도 (텔레그램·KIS 남용 방지)."""
    last = _last_report_try.get(kind)
    now = market_hours.now_ny()
    if last is not None and (now - last).total_seconds() < REPORT_RETRY_SEC:
        return False
    _last_report_try[kind] = now
    return True


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _trading_day() -> str:
    """미국 세션일 (NY). 매매·재매수 금지 일별 키."""
    return market_hours.trading_day_ny()


def _save_state() -> None:
    state = {
        "date": _trading_day(),
        "us_sim": us_sim.dump_state(),
        "reports": {
            "mid_report": _last_ran.get("mid_report", ""),
            "closing_report": _last_ran.get("closing_report", ""),
        },
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
        if isinstance(state.get("date"), str) and state["date"]:
            _last_ran["date"] = str(state["date"])
        reports = state.get("reports") if isinstance(state.get("reports"), dict) else {}
        if reports.get("mid_report"):
            _last_ran["mid_report"] = str(reports["mid_report"])
        if reports.get("closing_report"):
            _last_ran["closing_report"] = str(reports["closing_report"])
        if us_sim.get_open():
            pos = us_sim.get_open()
            print(f"[상태 복원] US 시뮬 보유 {pos['symbol']}")
    except Exception as e:
        print(f"[상태 복원 오류] {e}")


def _reset_if_new_day() -> None:
    today = _trading_day()
    prev = _last_ran.get("date")
    if prev and prev != today:
        last_done = market_hours.last_completed_session_day()
        if (
            not market_hours.is_us_regular_session()
            and last_done == prev
            and not _report_sent("closing", prev)
        ):
            print(f"[일별 초기화 보류] {prev} 마감보고 대기")
            _maybe_closing_report_backup()
            return
        us_sim.reset_daily()
        _save_state()
        print(f"[일별 초기화] US세션일 {today} (NY)")
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
            tcount = stats.get("tradeable", us_screener.tradeable_count(wl))
            warn = ""
            if int(tcount or 0) < 1:
                warn = "\n⚠️ 매매가능 후보 0 — 메가캡만이라 시뮬 스킵됨"
            elif mode == "fallback":
                warn = "\n⚠️ 순위 풀 비어 중형 폴백 사용"
            notifier.send(
                f"🇺🇸 <b>US 스크리너</b> ({mode})\n"
                f"유동 {len(wl)}/{us_screener.MAX_WATCH} · "
                f"풀 {stats.get('pool', '?')} · "
                f"등락≥{stats.get('min_rate', '?')}% · "
                f"점수≥{stats.get('min_score', '?')} · "
                f"메가제외 {stats.get('mega_excluded', '?')} · "
                f"매매가능 {tcount}\n"
                f"{preview}"
                f"{warn}"
            )
    except Exception as e:
        print(f"[US스크리너] 오류: {e}")
        notifier.notify_error(f"US 스크리너 오류: {e}")


def _limit_buy_price(last: float) -> float:
    """체결 유도용 지정가 (약간 위)."""
    return round(max(last * 1.003, last + 0.01), 2)


def _limit_sell_price(last: float) -> float:
    return round(max(last * 0.997, 0.01), 2)


def _downgrade_to_sim(symbol: str) -> None:
    us_sim.downgrade_live_to_sim(symbol)


def _check_sim() -> None:
    if not us_sim.is_enabled():
        return
    try:
        events = us_sim.run_check()
        for ev in events:
            if ev.get("action") == "buy":
                is_live = bool(ev.get("is_live"))
                if is_live:
                    try:
                        order_px = _limit_buy_price(float(ev["price"]))
                        kis_us_api.buy_us_stock(
                            ev["symbol"], ev["quantity"], order_px, ev["exchange"],
                        )
                        us_sim.mark_live_used()
                        notifier.notify_live_buy(
                            ev["symbol"], ev["exchange"], ev["quantity"],
                            ev["price"], ev["reason"],
                        )
                    except Exception as e:
                        print(f"[실전매수 실패→시뮬] {e}")
                        _downgrade_to_sim(ev["symbol"])
                        notifier.notify_error(f"실전 매수 실패 → 시뮬로 전환: {e}")
                        notifier.notify_sim_buy(
                            ev["symbol"], ev["exchange"], ev["quantity"],
                            ev["price"], ev["reason"] + " (실주문실패→시뮬)",
                        )
                else:
                    notifier.notify_sim_buy(
                        ev["symbol"], ev["exchange"], ev["quantity"],
                        ev["price"], ev["reason"],
                    )
            elif ev.get("action") == "sell":
                is_live = bool(ev.get("is_live"))
                if is_live:
                    try:
                        order_px = _limit_sell_price(float(ev["sell_price"]))
                        kis_us_api.sell_us_stock(
                            ev["symbol"], ev["quantity"], order_px, ev["exchange"],
                        )
                        notifier.notify_live_sell(
                            ev["symbol"], ev["exchange"], ev["quantity"],
                            ev["buy_price"], ev["sell_price"],
                            ev["profit_pct"], ev["sell_reason"],
                        )
                    except Exception as e:
                        print(f"[실전매도 실패] {e}")
                        notifier.notify_error(f"실전 매도 실패(재시도 필요): {e}")
                        notifier.notify_sim_sell(
                            ev["symbol"], ev["exchange"], ev["quantity"],
                            ev["buy_price"], ev["sell_price"],
                            ev["profit_pct"], ev["sell_reason"] + f" (실주문실패:{e})",
                        )
                else:
                    notifier.notify_sim_sell(
                        ev["symbol"], ev["exchange"], ev["quantity"],
                        ev["buy_price"], ev["sell_price"],
                        ev["profit_pct"], ev["sell_reason"],
                    )
        if events:
            _save_state()

        if us_sim.should_notify_skips():
            digest = us_sim.peek_skip_digest()
            if digest and notifier.notify_skip_digest(digest):
                us_sim.mark_skips_consumed()
    except Exception as e:
        print(f"[US시뮬] 오류: {e}")
        notifier.notify_error(f"US 시뮬 오류: {e}")


def _format_open_position_lines() -> list[str]:
    """보유 종목 현재가·미실현 손익."""
    lines: list[str] = []
    pos = us_sim.get_open()
    if pos:
        try:
            px = kis_us_api.get_us_price(pos["symbol"], pos["exchange"])
            price = float(px["last"])
            buy = float(pos["buy_price"])
            pct = (price - buy) / buy * 100 if buy > 0 else 0
            mode = "실전" if pos.get("is_live") else "시뮬"
            tag = pos.get("strategy") or ""
            lines.append(
                f"  [{mode}] {tag+' ' if tag else ''}{pos['symbol']} "
                f"{pct:+.1f}% @ ${price:.2f}"
            )
        except Exception as e:
            lines.append(f"  {pos['symbol']}: 시세 조회 실패 ({e})")
    for sym, ppos in us_sim.get_paper_positions().items():
        try:
            px = kis_us_api.get_us_price(sym, ppos["exchange"])
            price = float(px["last"])
            buy = float(ppos["buy_price"])
            pct = (price - buy) / buy * 100 if buy > 0 else 0
            tag = ppos.get("strategy") or ""
            mode = "실전" if ppos.get("is_live") else "병렬시뮬"
            lines.append(
                f"  [{mode}] {tag+' ' if tag else ''}{sym} "
                f"{pct:+.1f}% @ ${price:.2f}"
            )
        except Exception as e:
            lines.append(f"  {sym}: 시세 조회 실패 ({e})")
    return lines


def _append_watchlist_section(lines: list[str]) -> None:
    stats = us_screener.get_last_stats()
    mode = stats.get("mode", "?")
    preview = us_screener.format_watchlist_preview(8)
    tcount = stats.get("tradeable", us_screener.tradeable_count())
    lines.append(
        f"워치 ({mode}) {stats.get('count', '?')}/{us_screener.MAX_WATCH} · "
        f"매매가능 {tcount}"
    )
    lines.append(preview)
    if mode == "fallback":
        lines.append("⚠️ 순위 풀 비어 중형 폴백 사용")


def _maybe_mid_report() -> None:
    """NY 정오 이후 정규장 중이면 발송. 5분 창이 아니라 세션 끝날 때까지 재시도."""
    if not market_hours.is_us_regular_session():
        return
    if market_hours.now_ny().hour < MID_REPORT_HOUR_NY:
        return
    _run_mid_report()


def _run_mid_report() -> None:
    """정규장 중간 보고 (1회/세션)."""
    ny_day = _trading_day()
    if _report_sent("mid", ny_day):
        return
    if not _report_retry_ok("mid"):
        return
    print(f"[US보고] 중간 보고 (NY {market_hours.now_ny().strftime('%H:%M')})")

    lines = us_sim.format_session_report(closing=False)
    pos_lines = _format_open_position_lines()
    if pos_lines:
        lines.append("보유 현재가:")
        lines.extend(pos_lines)
        lines.append("")
    _append_watchlist_section(lines)
    if notifier.send("\n".join(lines)):
        _mark_report_sent("mid", ny_day)
        _save_state()


def _run_closing_report(ny_day: str | None = None) -> None:
    """미국 정규장 마감 — 최종 손익 보고 (1회/세션)."""
    ny_day = ny_day or market_hours.last_completed_session_day()
    if _report_sent("closing", ny_day):
        return
    if not _report_retry_ok("closing"):
        return
    print(f"[US보고] 장마감 보고 (NY {ny_day})")

    lines = us_sim.format_session_report(closing=True, ny_day=ny_day)
    _append_watchlist_section(lines)
    if notifier.send("\n".join(lines)):
        _mark_report_sent("closing", ny_day)
        _save_state()


def _in_closing_report_window() -> bool:
    """직전 세션 마감 이후 ~ 다음 정규장 개장 전 (주말 포함)."""
    if market_hours.is_us_regular_session():
        return False
    ny = market_hours.now_ny()
    if ny.weekday() < 5 and ny.time() < market_hours.REGULAR_OPEN:
        return True
    if ny.weekday() < 5 and ny.time() >= market_hours.REGULAR_CLOSE:
        return True
    return ny.weekday() >= 5


def _maybe_closing_report_backup() -> None:
    """세션 종료 edge를 놓친 경우(재시작·주말) 마감 보고 복구."""
    if market_hours.is_us_regular_session():
        return
    session_day = market_hours.last_completed_session_day()
    if _report_sent("closing", session_day):
        return
    if not _in_closing_report_window():
        return
    if not _report_retry_ok("closing_backup"):
        return
    print(f"[US보고] 마감 보고 백업 트리거 ({session_day})")
    _session_end_close()


def _session_end_close() -> None:
    def _px(symbol: str, exchange: str) -> float:
        try:
            quote = kis_us_api.get_us_price(symbol, exchange)
            return float(quote["last"]) or 0.0
        except Exception:
            return 0.0

    try:
        pos = us_sim.get_open()
        if pos:
            price = _px(pos["symbol"], pos["exchange"]) or float(pos["buy_price"])
            was_live = bool(pos.get("is_live"))
            trade = us_sim.force_close(price, "정규장 종료 청산")
            if trade:
                if was_live:
                    try:
                        order_px = _limit_sell_price(float(trade["sell_price"]))
                        kis_us_api.sell_us_stock(
                            trade["symbol"], trade["quantity"], order_px, trade["exchange"],
                        )
                        notifier.notify_live_sell(
                            trade["symbol"], trade["exchange"], trade["quantity"],
                            trade["buy_price"], trade["sell_price"],
                            trade["profit_pct"], trade["sell_reason"],
                        )
                    except Exception as e:
                        notifier.notify_error(f"정규장종료 실전매도 실패: {e}")
                        notifier.notify_sim_sell(
                            trade["symbol"], trade["exchange"], trade["quantity"],
                            trade["buy_price"], trade["sell_price"],
                            trade["profit_pct"], trade["sell_reason"] + f" (실주문실패:{e})",
                        )
                else:
                    notifier.notify_sim_sell(
                        trade["symbol"], trade["exchange"], trade["quantity"],
                        trade["buy_price"], trade["sell_price"],
                        trade["profit_pct"], trade["sell_reason"],
                    )

        for sym, ppos in list(us_sim.get_paper_positions().items()):
            price = _px(sym, ppos["exchange"]) or float(ppos["buy_price"])
            trade = us_sim.force_close_paper(sym, price, "정규장 종료 청산")
            if trade:
                notifier.notify_sim_sell(
                    trade["symbol"], trade["exchange"], trade["quantity"],
                    trade["buy_price"], trade["sell_price"],
                    trade["profit_pct"], trade["sell_reason"],
                )

        digest = us_sim.consume_skip_digest()
        if digest:
            notifier.notify_skip_digest(digest)
    except Exception as e:
        print(f"[US세션종료] 청산 오류: {e}")
        notifier.notify_error(f"US 정규장 종료 청산 오류: {e}")
    finally:
        _save_state()
        _run_closing_report(market_hours.last_completed_session_day())


def main() -> None:
    print("=== KIS 미국주식 봇 시작 (국내 봇과 분리) ===")
    _load_state()
    _maybe_closing_report_backup()

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
        f"규칙: GapGo={'ON' if us_sim.ENABLE_GAP_GO else 'OFF'} / "
        f"ORB={'ON' if us_sim.ENABLE_ORB else 'OFF'}({us_sim.ORB_MINUTES}m) / "
        f"S={'ON' if us_sim.ENABLE_S else 'OFF'} / "
        f"VWAP↑={'ON' if us_sim.REQUIRE_ABOVE_VWAP else 'OFF'}\n"
        f"청산: 익절 +{us_sim.TAKE_PROFIT_PCT:g}% / 손절 -{us_sim.STOP_LOSS_PCT:g}%\n"
        f"시뮬: {'ON' if us_sim.is_enabled() else 'OFF'} "
        f"(${us_sim.SIM_AMOUNT_USD:g}) / "
        f"실전: {'ON 방식A 점수순 최대 ' + str(us_sim.LIVE_MAX_POSITIONS) + '종' if LIVE_ORDERS else 'OFF'} "
        f"(${us_sim.LIVE_AMOUNT_USD:g}/회 · 총한도 ${us_sim.MAX_TOTAL_USD:g})\n"
        f"병렬시뮬: {'ON' if us_sim.PARALLEL_SIM else 'OFF'} "
        f"(최대 {us_sim.MAX_SIM_POSITIONS}종) · "
        f"스킵알림 {us_sim.SKIP_NOTIFY_INTERVAL_MIN}분\n"
        f"점검 주기: {POLL_MIN}분 · 스크린 {us_screener.SCREEN_INTERVAL_MIN}분\n"
        f"보고: 중간 NY {MID_REPORT_HOUR_NY:02d}:00 이후 · 마감 NY 종료(미발송 시 재시도)\n"
        f"⚠️ 국내 kis-trading-bot과 별도 Railway 서비스"
    )

    global _was_in_session
    _was_in_session = market_hours.is_us_regular_session()
    last_poll_min = -1
    session_open_notified = False

    while True:
        try:
            last_poll_min, session_open_notified = _run_loop_tick(
                last_poll_min=last_poll_min,
                session_open_notified=session_open_notified,
            )
        except Exception as e:
            _notify_loop_error(e)
            time.sleep(min(30 + _loop_errors * 10, 120))
            continue
        time.sleep(20)


def _run_forever() -> None:
    """프로세스 전체 감시 — main()이 죽어도 재기동."""
    restarts = 0
    while True:
        try:
            main()
        except KeyboardInterrupt:
            print("[종료] KeyboardInterrupt")
            break
        except Exception as e:
            restarts += 1
            print(f"[치명적 오류 · 재시작 #{restarts}] {e}")
            notifier.notify_error(f"⚠️ US봇 프로세스 재시작 (#{restarts})\n{e}")
            time.sleep(min(60 * restarts, 300))


if __name__ == "__main__":
    _run_forever()
