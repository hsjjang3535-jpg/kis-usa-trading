"""텔레그램 알림."""
from __future__ import annotations

import os

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send(message: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[텔레그램 미설정] {message}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[텔레그램 전송 오류] {e}")


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
