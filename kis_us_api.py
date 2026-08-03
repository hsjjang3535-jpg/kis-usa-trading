"""
한국투자증권 해외주식(미국) OpenAPI 래퍼.

시세는 실전 서버, 주문/잔고는 KIS_MODE에 따름.
통합증거금 계좌 기준 — 주문은 ENABLE_US_LIVE_ORDERS=true 일 때만.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

KST = ZoneInfo("Asia/Seoul")
load_dotenv()

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")


def _normalize_mode(raw: str) -> str:
    v = (raw or "모의").strip().lower()
    if v in ("실전", "prod", "real", "production"):
        return "실전"
    return "모의"


MODE = _normalize_mode(os.getenv("KIS_MODE", "모의"))
MARKET_URL = "https://openapi.koreainvestment.com:9443"
TRADE_URL = (
    "https://openapi.koreainvestment.com:9443"
    if MODE == "실전"
    else "https://openapivts.koreainvestment.com:29443"
)

_token_cache = {
    "market": {"token": None, "expires_at": None},
    "trade": {"token": None, "expires_at": None},
}
_last_call = 0.0


def get_account_parts() -> tuple[str, str]:
    raw = os.getenv("KIS_ACCOUNT_NO", "").strip().replace("-", "").replace(" ", "")
    if raw:
        if len(raw) >= 10:
            return raw[:8], raw[8:10]
        if len(raw) == 8:
            prod = os.getenv("KIS_ACNT_PRDT_CD", "").strip() or "01"
            return raw.zfill(8)[-8:], prod.zfill(2)[-2:]
        raise ValueError(f"계좌번호 형식 오류 ({raw})")
    cano = os.getenv("KIS_CANO", "").strip()
    prod = os.getenv("KIS_ACNT_PRDT_CD", "").strip()
    if cano and prod:
        return cano.zfill(8)[-8:], prod.zfill(2)[-2:]
    raise ValueError("KIS_ACCOUNT_NO 또는 KIS_CANO/KIS_ACNT_PRDT_CD 필요")


def get_account_info() -> dict:
    cano, prod = get_account_parts()
    return {"masked": f"{cano[:4]}****{prod}", "mode": MODE}


def _fetch_token(base_url: str) -> str:
    if not APP_KEY or not APP_SECRET:
        raise RuntimeError("KIS_APP_KEY / KIS_APP_SECRET 미설정")
    res = requests.post(
        f"{base_url}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
        },
        timeout=15,
    )
    res.raise_for_status()
    data = res.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"토큰 발급 실패: {data}")
    return token


def _get_token(kind: str) -> str:
    cache = _token_cache[kind]
    now = datetime.now(KST)
    if cache["token"] and cache["expires_at"] and cache["expires_at"] > now:
        return cache["token"]
    url = MARKET_URL if kind == "market" else TRADE_URL
    cache["token"] = _fetch_token(url)
    cache["expires_at"] = now + timedelta(hours=12)
    return cache["token"]


def _throttle() -> None:
    global _last_call
    gap = 0.2 if MODE == "실전" else 1.0
    elapsed = time.time() - _last_call
    if elapsed < gap:
        time.sleep(gap - elapsed)
    _last_call = time.time()


def _headers(tr_id: str, kind: str = "market") -> dict:
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {_get_token(kind)}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


def _get(path: str, tr_id: str, params: dict, kind: str = "market") -> dict:
    _throttle()
    url = MARKET_URL if kind == "market" else TRADE_URL
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            res = requests.get(
                f"{url}{path}",
                headers=_headers(tr_id, kind),
                params=params,
                timeout=20,
            )
            if res.status_code == 500 and attempt < 2:
                time.sleep(1 + attempt)
                continue
            res.raise_for_status()
            data = res.json()
            if str(data.get("rt_cd", "0")) not in ("0", ""):
                raise RuntimeError(
                    f"KIS 오류 [{data.get('msg_cd')}] {data.get('msg1')} ({path})"
                )
            return data
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise
    raise last_err or RuntimeError(path)


def _post(path: str, tr_id: str, body: dict) -> dict:
    _throttle()
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            res = requests.post(
                f"{TRADE_URL}{path}",
                headers=_headers(tr_id, "trade"),
                json=body,
                timeout=20,
            )
            if res.status_code == 500 and attempt < 2:
                time.sleep(1 + attempt)
                continue
            res.raise_for_status()
            return res.json()
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise
    raise last_err or RuntimeError(path)


def get_us_price(symbol: str, exchange: str = "NAS") -> dict:
    """해외주식 현재가. exchange: NAS / NYS / AMS"""
    data = _get(
        "/uapi/overseas-price/v1/quotations/price",
        "HHDFS00000300",
        {"AUTH": "", "EXCD": exchange, "SYMB": symbol.upper()},
        kind="market",
    )
    out = data.get("output") or {}
    try:
        last = float(out.get("last") or 0)
    except (TypeError, ValueError):
        last = 0.0
    try:
        rate = float(out.get("rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    return {
        "symbol": symbol.upper(),
        "exchange": exchange.upper(),
        "last": last,
        "rate": rate,
        "raw": out,
    }


def get_us_daily_prices(symbol: str, exchange: str = "NAS", days: int = 100) -> list[dict]:
    """해외주식 일별 시세 (최신 순)."""
    data = _get(
        "/uapi/overseas-price/v1/quotations/dailyprice",
        "HHDFS76240000",
        {
            "AUTH": "",
            "EXCD": exchange.upper(),
            "SYMB": symbol.upper(),
            "GUBN": "0",
            "BYMD": "",
            "MODP": "0",
        },
        kind="market",
    )
    rows = data.get("output2") or []
    parsed: list[dict] = []
    for r in rows[:days]:
        try:
            parsed.append({
                "date": str(r.get("xymd") or ""),
                "open": float(r.get("open") or 0),
                "high": float(r.get("high") or 0),
                "low": float(r.get("low") or 0),
                "close": float(r.get("clos") or 0),
                "volume": float(r.get("tvol") or 0),
            })
        except (TypeError, ValueError):
            continue
    return parsed


def _ovrs_excg_cd(exchange: str) -> str:
    """주문용 거래소 코드."""
    ex = exchange.upper()
    return {
        "NAS": "NASD",
        "NYS": "NYSE",
        "AMS": "AMEX",
        "NASD": "NASD",
        "NYSE": "NYSE",
        "AMEX": "AMEX",
    }.get(ex, "NASD")


def buy_us_stock(
    symbol: str,
    quantity: int,
    price: float,
    exchange: str = "NAS",
    *,
    order_type: str = "00",
) -> dict:
    """
    미국 주식 매수.
    order_type: 00=지정가, 32=LOC 등 (KIS 스펙 따름)
    모의: VTTT1002U / 실전: TTTT1002U
    """
    cano, prod = get_account_parts()
    tr_id = "TTTT1002U" if MODE == "실전" else "VTTT1002U"
    body = {
        "CANO": cano,
        "ACNT_PRDT_CD": prod,
        "OVRS_EXCG_CD": _ovrs_excg_cd(exchange),
        "PDNO": symbol.upper(),
        "ORD_QTY": str(int(quantity)),
        "OVRS_ORD_UNPR": f"{float(price):.2f}",
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN": order_type,
    }
    return _post("/uapi/overseas-stock/v1/trading/order", tr_id, body)


def sell_us_stock(
    symbol: str,
    quantity: int,
    price: float,
    exchange: str = "NAS",
    *,
    order_type: str = "00",
) -> dict:
    cano, prod = get_account_parts()
    tr_id = "TTTT1006U" if MODE == "실전" else "VTTT1006U"
    body = {
        "CANO": cano,
        "ACNT_PRDT_CD": prod,
        "OVRS_EXCG_CD": _ovrs_excg_cd(exchange),
        "PDNO": symbol.upper(),
        "ORD_QTY": str(int(quantity)),
        "OVRS_ORD_UNPR": f"{float(price):.2f}",
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN": order_type,
    }
    return _post("/uapi/overseas-stock/v1/trading/order", tr_id, body)
