"""텔레그램 알림."""
from __future__ import annotations

import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
_HTML_TAG_RE = re.compile(r"</?b>")


def send(message: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[텔레그램 미설정] {message}")
        return False
    if _post(message, html=True):
        return True
    # 스킵 사유의 '<' 등 때문에 HTML이 깨지면 평문으로 재시도
    plain = _HTML_TAG_RE.sub("", message)
    print("[텔레그램] HTML 실패 → 평문 재시도")
    return _post(plain, html=False)


def _post(message: str, *, html: bool) -> bool:
    body: dict = {"chat_id": CHAT_ID, "text": message}
    if html:
        body["parse_mode"] = "HTML"
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=body,
            timeout=10,
        )
        data = res.json() if res.content else {}
        if not data.get("ok"):
            print(f"[텔레그램 거부] {data}")
            return False
        return True
    except Exception as e:
        print(f"[텔레그램 전송 오류] {e}")
        return False


def notify_error(msg: str) -> None:
    send(f"⚠️ <b>[US봇 오류]</b>\n{msg}")


def notify_sim_buy(symbol: str, exchange: str, qty: int, price: float, reason: str) -> None:
    send(
        f"🇺🇸🟢 <b>[US 시뮬] 매수</b>\n"
        f"{symbol} ({exchange})\n"
        f"{qty}주 @ ${price:.2f}\n"
        f"{reason}\n"
        f"⚠️ 시뮬만 — 실제 주문 없음"
    )


def notify_sim_sell(
    symbol: str,
    exchange: str,
    qty: int,
    buy: float,
    sell: float,
    pct: float,
    reason: str,
) -> None:
    emoji = "📈" if pct >= 0 else "📉"
    sign = "+" if pct >= 0 else ""
    send(
        f"🇺🇸{emoji} <b>[US 시뮬] 매도</b>\n"
        f"{symbol} ({exchange})\n"
        f"{qty}주 ${buy:.2f} → ${sell:.2f}\n"
        f"{sign}{pct:.2f}%\n"
        f"{reason}\n"
        f"⚠️ 시뮬만 — 실제 주문 없음"
    )


def notify_skip_digest(lines: list[str]) -> bool:
    if not lines:
        return False
    body = "\n".join(lines[:20])
    more = f"\n… 외 {len(lines) - 20}종" if len(lines) > 20 else ""
    return send(
        f"🇺🇸📋 <b>US 워치 스킵 요약</b>\n"
        f"(미진입 사유 · 최근 점검)\n"
        f"{body}{more}"
    )


def notify_live_buy(symbol: str, exchange: str, qty: int, price: float, reason: str) -> None:
    send(
        f"🇺🇸🔴 <b>[US 실전] 매수</b>\n"
        f"{symbol} ({exchange})\n"
        f"{qty}주 @ ${price:.2f}\n"
        f"{reason}\n"
        f"⚠️ 실주문 — 방식A 점수순 "
        f"(세션 최대 {os.getenv('US_LIVE_MAX_POSITIONS', '2')}종)"
    )


def notify_live_sell(
    symbol: str,
    exchange: str,
    qty: int,
    buy: float,
    sell: float,
    pct: float,
    reason: str,
) -> None:
    emoji = "📈" if pct >= 0 else "📉"
    sign = "+" if pct >= 0 else ""
    send(
        f"🇺🇸{emoji} <b>[US 실전] 매도</b>\n"
        f"{symbol} ({exchange})\n"
        f"{qty}주 ${buy:.2f} → ${sell:.2f}\n"
        f"{sign}{pct:.2f}%\n"
        f"{reason}\n"
        f"⚠️ 실주문 청산"
    )
