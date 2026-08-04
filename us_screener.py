"""
미국주식 동적 스크리너 (국내장 스크리너와 같은 역할)

후보 소스 (KIS 해외 순위 API — 거래소 전종목 순위표에서 상위권):
  1) 거래량급증 상위
  2) 상승률 상위
  3) 거래량 상위 (보조)
  ※ 티커 전수 스캔이 아니라, KIS가 주는 순위 리스트에서 필터 후 TOP N

필터:
  - 당일 등락률 하한 / 최소 가격 / 매매가능
  - 시가총액 상위(메가캡) 제외
최종:
  - 점수 순 TOP N
  - 비면 등락 하한 완화 재시도 → 그래도 없으면 중형 폴백
  ※ AAPL 등 메가캡 고정 폴백은 쓰지 않음 (메가제외와 충돌)
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
MIN_RATE_RELAXED = float(os.getenv("US_SCREEN_MIN_RATE_RELAXED", "0.5"))
MIN_PRICE = float(os.getenv("US_SCREEN_MIN_PRICE", "5.0"))
VOL_RANG = os.getenv("US_SCREEN_VOL_RANG", "3")  # 1만주 이상
INCLUDE_NYS = os.getenv("US_SCREEN_INCLUDE_NYS", "false").lower() == "true"
SCREEN_INTERVAL_MIN = int(os.getenv("US_SCREEN_INTERVAL_MIN", "30"))
EXCLUDE_MEGA = os.getenv("US_EXCLUDE_MEGA_CAP", "true").lower() == "true"
MEGA_RANK_CUTOFF = int(os.getenv("US_MEGA_CAP_RANK_CUTOFF", "50"))
# 시총 순위 API 실패 시 하드 블록 (초대형)
_DEFAULT_MEGA = (
    "AAPL,MSFT,NVDA,GOOGL,GOOG,AMZN,META,TSLA,BRK.B,BRKB,"
    "AVGO,JPM,V,UNH,XOM,MA,LLY,JNJ,WMT,PG"
)
MEGA_BLOCKLIST = {
    s.strip().upper()
    for s in os.getenv("US_MEGA_CAP_BLOCKLIST", _DEFAULT_MEGA).split(",")
    if s.strip()
}
# 메가캡 제외 시 쓰는 중형 폴백 (초대형 금지)
_DEFAULT_MID_FALLBACK = (
    "SOFI:NAS,PLTR:NAS,RIVN:NAS,MARA:NAS,COIN:NAS,"
    "HOOD:NAS,UBER:NAS,SNAP:NAS,DKNG:NAS,PATH:NAS"
)

_watchlist: list[dict] = []
_last_screen_at: datetime | None = None
_last_stats: dict = {}
_mega_symbols: set[str] = set()


def _parse_watch_env(raw: str) -> list[dict]:
    items: list[dict] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            sym, ex = part.split(":", 1)
            symbol = sym.strip().upper()
            exchange = ex.strip().upper() or "NAS"
        else:
            symbol = part.upper()
            exchange = "NAS"
        if EXCLUDE_MEGA and (symbol in MEGA_BLOCKLIST or symbol in _mega_symbols):
            continue
        items.append({
            "symbol": symbol,
            "exchange": exchange,
            "name": "",
            "rate": 0.0,
            "source": "fallback",
        })
    return items[:MAX_WATCH]


def _fallback_watchlist() -> list[dict]:
    """메가캡과 충돌하지 않는 폴백."""
    raw = os.getenv("US_FALLBACK_WATCHLIST") or os.getenv("US_WATCHLIST", "")
    # 구 US_WATCHLIST가 AAPL 등이면 메가 필터 후 비게 됨 → 중형 기본
    items = _parse_watch_env(raw) if raw else []
    if not items:
        items = _parse_watch_env(
            os.getenv("US_MID_FALLBACK_WATCHLIST", _DEFAULT_MID_FALLBACK)
        )
    return items[:MAX_WATCH]


def get_watchlist() -> list[dict]:
    if _watchlist:
        return list(_watchlist)
    return _fallback_watchlist()


def get_last_stats() -> dict:
    return dict(_last_stats)


def is_mega_cap(symbol: str) -> bool:
    sym = symbol.upper()
    if EXCLUDE_MEGA and sym in MEGA_BLOCKLIST:
        return True
    return EXCLUDE_MEGA and sym in _mega_symbols


def tradeable_count(watch: list[dict] | None = None) -> int:
    wl = watch if watch is not None else get_watchlist()
    return sum(1 for w in wl if w.get("symbol") and not is_mega_cap(w["symbol"]))


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


def _refresh_mega_set(exchanges: list[str]) -> int:
    """시가총액 순위 상위 → 메가캡 제외 집합."""
    global _mega_symbols
    mega: set[str] = set(MEGA_BLOCKLIST) if EXCLUDE_MEGA else set()
    if not EXCLUDE_MEGA:
        _mega_symbols = set()
        return 0
    for ex in exchanges:
        try:
            rows = kis_us_api.get_market_cap(ex, vol_rang="0")
            for i, r in enumerate(rows):
                rank = int(r.get("rank") or (i + 1))
                if rank <= MEGA_RANK_CUTOFF:
                    mega.add(r["symbol"])
        except Exception as e:
            print(f"[US스크리너] 시총순위 실패 {ex}: {e}")
    _mega_symbols = mega
    return len(mega)


def _merge_pool(rows: list[dict], source: str, *, min_rate: float) -> dict[str, dict]:
    pool: dict[str, dict] = {}
    for r in rows:
        if not r.get("tradable", True):
            continue
        if float(r.get("last") or 0) < MIN_PRICE:
            continue
        if float(r.get("rate") or 0) < min_rate:
            continue
        if is_mega_cap(r["symbol"]):
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
            "avg_volume": r.get("avg_volume"),
            "mktcap": r.get("mktcap"),
            "source": source,
        }
        prev = pool.get(key)
        if not prev or _score(item) > _score(prev):
            pool[key] = item
    return pool


def _collect_raw(exchanges: list[str]) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    surge_all: list[dict] = []
    up_all: list[dict] = []
    vol_all: list[dict] = []
    errors: list[str] = []
    for ex in exchanges:
        try:
            surge_all.extend(kis_us_api.get_volume_surge(ex, mixn="3", vol_rang=VOL_RANG))
        except Exception as e:
            errors.append(f"surge:{ex}:{e}")
        try:
            up_all.extend(kis_us_api.get_updown_rate(ex, gubn="1", vol_rang=VOL_RANG))
        except Exception as e:
            errors.append(f"updown:{ex}:{e}")
        try:
            vol_all.extend(kis_us_api.get_trade_vol(ex, vol_rang=VOL_RANG))
        except Exception as e:
            errors.append(f"tradevol:{ex}:{e}")
    return surge_all, up_all, vol_all, errors


def run_screening(force: bool = False) -> list[dict]:
    """동적 워치리스트 갱신. 반환: 최종 후보."""
    global _watchlist, _last_screen_at, _last_stats

    if not DYNAMIC:
        _watchlist = _fallback_watchlist()
        _last_stats = {
            "mode": "fixed",
            "count": len(_watchlist),
            "tradeable": tradeable_count(_watchlist),
        }
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

    mega_n = _refresh_mega_set(exchanges)
    surge_all, up_all, vol_all, errors = _collect_raw(exchanges)
    surge_n, up_n, vol_n = len(surge_all), len(up_all), len(vol_all)

    used_rate = MIN_RATE
    pool: dict[str, dict] = {}
    pool.update(_merge_pool(surge_all, "volume_surge", min_rate=used_rate))
    pool.update(_merge_pool(up_all, "updown", min_rate=used_rate))
    pool.update(_merge_pool(vol_all, "trade_vol", min_rate=used_rate))

    # 개장 직후 등락 부족·메가 위주면 하한 완화 재시도
    if not pool and MIN_RATE_RELAXED < MIN_RATE:
        used_rate = MIN_RATE_RELAXED
        pool.update(_merge_pool(surge_all, "volume_surge", min_rate=used_rate))
        pool.update(_merge_pool(up_all, "updown", min_rate=used_rate))
        pool.update(_merge_pool(vol_all, "trade_vol", min_rate=used_rate))

    ranked = sorted(pool.values(), key=_score, reverse=True)[:MAX_WATCH]
    if ranked:
        _watchlist = ranked
        mode = "dynamic" if used_rate >= MIN_RATE else "dynamic_relaxed"
    else:
        _watchlist = _fallback_watchlist()
        mode = "fallback"

    _last_screen_at = now
    tcount = tradeable_count(_watchlist)
    _last_stats = {
        "mode": mode,
        "count": len(_watchlist),
        "tradeable": tcount,
        "surge_raw": surge_n,
        "updown_raw": up_n,
        "tradevol_raw": vol_n,
        "pool": len(pool),
        "mega_excluded": mega_n,
        "min_rate": used_rate,
        "errors": errors[:3],
        "at": now.strftime("%H:%M"),
    }
    print(
        f"[US스크리너] {mode} TOP{MAX_WATCH}→{len(_watchlist)}종 "
        f"(급증 {surge_n}/상승 {up_n}/거래량 {vol_n}/풀 {len(pool)}/"
        f"매매가능 {tcount}/메가집합 {mega_n})"
    )
    return list(_watchlist)
