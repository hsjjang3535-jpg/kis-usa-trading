"""
미국주식 동적 스크리너 (국내장 스크리너와 같은 역할)

후보 풀:
  1) 나스닥(+옵션 뉴욕) 거래량급증 상위
  2) 상승률 상위
필터:
  - 당일 등락률 하한
  - 최소 가격
  - 매매가능 종목
최종:
  - 점수(등락률·급증율) 순으로 TOP N → 워치리스트
실패 시 US_WATCHLIST 고정 폴백
"""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import kis_us_api

KST = ZoneInfo("Asia/Seoul")

DYNAMIC = os.getenv("US_DYNAMIC_WATCHLIST", "true").lower() == "true"
MAX_WATCH = int(os.getenv("US_MAX_WATCHLIST", "10"))
MIN_RATE = float(os.getenv("US_SCREEN_MIN_RATE", "2.0"))
MIN_PRICE = float(os.getenv("US_SCREEN_MIN_PRICE", "5.0"))
VOL_RANG = os.getenv("US_SCREEN_VOL_RANG", "3")  # 1만주 이상
INCLUDE_NYS = os.getenv("US_SCREEN_INCLUDE_NYS", "false").lower() == "true"
SCREEN_INTERVAL_MIN = int(os.getenv("US_SCREEN_INTERVAL_MIN", "30"))

_watchlist: list[dict] = []
_last_screen_at: datetime | None = None
_last_stats: dict = {}


def _fallback_watchlist() -> list[dict]:
    raw = os.getenv("US_WATCHLIST", "AAPL:NAS,MSFT:NAS,NVDA:NAS,TSLA:NAS")
    items: list[dict] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            sym, ex = part.split(":", 1)
            items.append({
                "symbol": sym.strip().upper(),
                "exchange": ex.strip().upper() or "NAS",
                "name": "",
                "rate": 0.0,
                "source": "fallback",
            })
        else:
            items.append({
                "symbol": part.upper(),
                "exchange": "NAS",
                "name": "",
                "rate": 0.0,
                "source": "fallback",
            })
    return items[:MAX_WATCH]


def get_watchlist() -> list[dict]:
    if _watchlist:
        return list(_watchlist)
    return _fallback_watchlist()


def get_last_stats() -> dict:
    return dict(_last_stats)


def format_watchlist_preview(limit: int = 8) -> str:
    wl = get_watchlist()
    parts = []
    for w in wl[:limit]:
        rate = w.get("rate")
        if rate:
            parts.append(f"{w['symbol']}({float(rate):+.1f}%)")
        else:
            parts.append(str(w["symbol"]))
    extra = "…" if len(wl) > limit else ""
    return ", ".join(parts) + extra


def _score(item: dict) -> float:
    return float(item.get("rate") or 0) + min(float(item.get("surge_rate") or 0) / 10.0, 20.0)


def _merge_pool(rows: list[dict], source: str) -> dict[str, dict]:
    pool: dict[str, dict] = {}
    for r in rows:
        if not r.get("tradable", True):
            continue
        if float(r.get("last") or 0) < MIN_PRICE:
            continue
        if float(r.get("rate") or 0) < MIN_RATE:
            continue
        key = f"{r['exchange']}:{r['symbol']}"
        item = {
            "symbol": r["symbol"],
            "exchange": r["exchange"],
            "name": r.get("name") or "",
            "last": r.get("last"),
            "rate": float(r.get("rate") or 0),
            "surge_rate": float(r.get("surge_rate") or 0),
            "volume": r.get("volume"),
            "source": source,
        }
        prev = pool.get(key)
        if not prev or _score(item) > _score(prev):
            pool[key] = item
    return pool


def run_screening(force: bool = False) -> list[dict]:
    """동적 워치리스트 갱신. 반환: 최종 후보."""
    global _watchlist, _last_screen_at, _last_stats

    if not DYNAMIC:
        _watchlist = _fallback_watchlist()
        _last_stats = {"mode": "fixed", "count": len(_watchlist)}
        return list(_watchlist)

    now = datetime.now(KST)
    if (
        not force
        and _last_screen_at is not None
        and (now - _last_screen_at).total_seconds() < SCREEN_INTERVAL_MIN * 60
        and _watchlist
    ):
        return list(_watchlist)

    exchanges = ["NAS"]
    if INCLUDE_NYS:
        exchanges.append("NYS")

    pool: dict[str, dict] = {}
    surge_n = up_n = 0
    errors: list[str] = []

    for ex in exchanges:
        try:
            surge = kis_us_api.get_volume_surge(ex, mixn="3", vol_rang=VOL_RANG)
            surge_n += len(surge)
            pool.update(_merge_pool(surge, "volume_surge"))
        except Exception as e:
            errors.append(f"surge:{ex}:{e}")
        try:
            ups = kis_us_api.get_updown_rate(ex, gubn="1", vol_rang=VOL_RANG)
            up_n += len(ups)
            pool.update(_merge_pool(ups, "updown"))
        except Exception as e:
            errors.append(f"updown:{ex}:{e}")

    ranked = sorted(pool.values(), key=_score, reverse=True)[:MAX_WATCH]
    if ranked:
        _watchlist = ranked
        mode = "dynamic"
    else:
        _watchlist = _fallback_watchlist()
        mode = "fallback"

    _last_screen_at = now
    _last_stats = {
        "mode": mode,
        "count": len(_watchlist),
        "surge_raw": surge_n,
        "updown_raw": up_n,
        "pool": len(pool),
        "min_rate": MIN_RATE,
        "errors": errors[:3],
        "at": now.strftime("%H:%M"),
    }
    print(
        f"[US스크리너] {mode} {len(_watchlist)}종 "
        f"(급증원본 {surge_n} / 상승원본 {up_n} / 풀 {len(pool)})"
    )
    return list(_watchlist)
