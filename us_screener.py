"""
미국주식 동적 스크리너

1차: 거래량급증 + 상승률 + 거래량 순위
2차(비면): 거래대금 + 매수체결강도 + 신고가 순위
3차: 등락/거래량조건 완화 후 재시도
최후: 중형 고정 폴백 (거의 안 쓰이게)

유동 TOP N:
  - 상한 MAX_WATCH (기본 10)
  - 점수(등락·급증·체결강도)가 MIN_SCORE 이상인 종목만 채택
  -  qualifying 이 3개면 3개만 — 억지로 10개 채우지 않음
"""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import kis_us_api

KST = ZoneInfo("Asia/Seoul")

DYNAMIC = os.getenv("US_DYNAMIC_WATCHLIST", "true").lower() == "true"
MAX_WATCH = int(os.getenv("US_MAX_WATCHLIST", "10"))
MIN_WATCH = int(os.getenv("US_MIN_WATCHLIST", "1"))  # 유동 하한(정보용, 패딩 안 함)
MIN_RATE = float(os.getenv("US_SCREEN_MIN_RATE", "2.0"))
MIN_RATE_RELAXED = float(os.getenv("US_SCREEN_MIN_RATE_RELAXED", "0.5"))
MIN_SCORE = float(os.getenv("US_SCREEN_MIN_SCORE", "2.0"))  # 유동 TOP 점수 하한
MIN_SCORE_RELAXED = float(os.getenv("US_SCREEN_MIN_SCORE_RELAXED", "0.5"))
MIN_PRICE = float(os.getenv("US_SCREEN_MIN_PRICE", "5.0"))
VOL_RANG = os.getenv("US_SCREEN_VOL_RANG", "0")
VOL_RANG_RELAXED = os.getenv("US_SCREEN_VOL_RANG_RELAXED", "0")
INCLUDE_NYS = os.getenv("US_SCREEN_INCLUDE_NYS", "true").lower() == "true"
SCREEN_INTERVAL_MIN = int(os.getenv("US_SCREEN_INTERVAL_MIN", "30"))
EXCLUDE_MEGA = os.getenv("US_EXCLUDE_MEGA_CAP", "true").lower() == "true"
MEGA_RANK_CUTOFF = int(os.getenv("US_MEGA_CAP_RANK_CUTOFF", "50"))
# 해외 ETP(ETF/ETN/레버리지 등) — 미신청 계좌 실주문 거부(APBK1672) 방지
EXCLUDE_ETP = os.getenv("US_EXCLUDE_ETP", "true").lower() == "true"

_DEFAULT_MEGA = (
    "AAPL,MSFT,NVDA,GOOGL,GOOG,AMZN,META,TSLA,BRK.B,BRKB,"
    "AVGO,JPM,V,UNH,XOM,MA,LLY,JNJ,WMT,PG"
)
MEGA_BLOCKLIST = {
    s.strip().upper()
    for s in os.getenv("US_MEGA_CAP_BLOCKLIST", _DEFAULT_MEGA).split(",")
    if s.strip()
}
_DEFAULT_MID_FALLBACK = (
    "SOFI:NAS,PLTR:NAS,RIVN:NAS,MARA:NAS,COIN:NAS,"
    "HOOD:NAS,UBER:NAS,SNAP:NAS,DKNG:NAS,PATH:NAS"
)

# 이름/심볼에 포함되면 ETP로 간주 (한투 해외ETP 신청 대상)
_ETP_NAME_KEYWORDS = (
    "ETF", "ETN", "ETC", "ETP",
    "2X", "3X", "-2X", "-3X",
    "LEVERAGE", "LEVERAGED", "INVERSE", "ULTRA",
    "DIREXION", "PROSHARES", "GRANITESHARES",
    "DAILY LONG", "DAILY SHORT",
    "BULL 2", "BEAR 2", "BULL 3", "BEAR 3",
    "REIT",
)
_DEFAULT_ETP_BLOCK = (
    "TQQQ,SQQQ,UPRO,SPXU,TNA,TZA,SOXL,SOXS,TECL,TECS,"
    "LABU,LABD,FNGU,FNGD,TQQQ,QLD,QID,SPXL,SPXS,"
    "XRPT,CONL,BITX,ETHU,NVDL,TSLL,AMDL,AAPU,MSFU,"
    "NVDX,TSLG,METU,PTIR,MSTX"
)
ETP_BLOCKLIST = {
    s.strip().upper()
    for s in os.getenv("US_ETP_BLOCKLIST", _DEFAULT_ETP_BLOCK).split(",")
    if s.strip()
}

_watchlist: list[dict] = []
_last_screen_at: datetime | None = None
_last_stats: dict = {}
_mega_symbols: set[str] = set()
_etp_runtime: set[str] = set()  # 주문 거부로 학습한 ETP


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
        if is_etp(symbol):
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
    raw = os.getenv("US_FALLBACK_WATCHLIST") or os.getenv("US_WATCHLIST", "")
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


def is_mega_cap(symbol: str, *, rank: bool = True) -> bool:
    """rank=False면 초대형 블록리스트만 (시총상위 컷 생략)."""
    sym = symbol.upper()
    if EXCLUDE_MEGA and sym in MEGA_BLOCKLIST:
        return True
    if rank and EXCLUDE_MEGA and sym in _mega_symbols:
        return True
    return False


def is_etp(symbol: str, name: str = "") -> bool:
    """해외 ETP(ETF/ETN/레버리지 등). 일반주만 매매할 때 제외."""
    if not EXCLUDE_ETP:
        return False
    sym = (symbol or "").strip().upper()
    if not sym:
        return False
    if sym in ETP_BLOCKLIST or sym in _etp_runtime:
        return True
    text = f"{sym} {name or ''}".upper()
    return any(kw in text for kw in _ETP_NAME_KEYWORDS)


def mark_etp(symbol: str) -> None:
    """실주문 ETP 거부 등으로 학습."""
    sym = (symbol or "").strip().upper()
    if sym:
        _etp_runtime.add(sym)


def tradeable_count(watch: list[dict] | None = None) -> int:
    wl = watch if watch is not None else get_watchlist()
    return sum(
        1 for w in wl
        if w.get("symbol")
        and not is_mega_cap(w["symbol"])
        and not is_etp(w["symbol"], w.get("name") or "")
    )


def format_watchlist_preview(limit: int = 8) -> str:
    wl = get_watchlist()
    parts = []
    for w in wl[:limit]:
        rate = w.get("rate")
        src = w.get("source") or ""
        tag = f"/{src[:4]}" if src and src != "fallback" else ""
        if rate:
            parts.append(f"{w['symbol']}({float(rate):+.1f}%{tag})")
        else:
            parts.append(f"{w['symbol']}{tag}")
    extra = "…" if len(wl) > limit else ""
    return ", ".join(parts) + extra


def _score(item: dict) -> float:
    """등락 + 급증 + 체결강도 가점."""
    rate = float(item.get("rate") or 0)
    surge = min(float(item.get("surge_rate") or 0) / 10.0, 20.0)
    power = min(float(item.get("buy_power") or 0) / 20.0, 10.0)
    return rate + surge + power


def _refresh_mega_set(exchanges: list[str]) -> int:
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


def _merge_pool(rows: list[dict], source: str, *, min_rate: float, rank_mega: bool = False) -> dict[str, dict]:
    pool: dict[str, dict] = {}
    for r in rows:
        if float(r.get("last") or 0) < MIN_PRICE:
            continue
        if float(r.get("rate") or 0) < min_rate:
            continue
        if is_mega_cap(r["symbol"], rank=rank_mega):
            continue
        if is_etp(r["symbol"], r.get("name") or ""):
            continue
        key = f"{r['exchange']}:{r['symbol']}"
        item = {
            "symbol": r["symbol"],
            "exchange": r["exchange"],
            "name": r.get("name") or "",
            "last": r.get("last"),
            "rate": float(r.get("rate") or 0),
            "surge_rate": float(r.get("surge_rate") or 0),
            "buy_power": float(r.get("buy_power") or 0),
            "volume": r.get("volume"),
            "avg_volume": r.get("avg_volume"),
            "mktcap": r.get("mktcap"),
            "source": source,
        }
        prev = pool.get(key)
        if not prev or _score(item) > _score(prev):
            pool[key] = item
    return pool


def _select_fluid(pool: dict[str, dict], *, min_score: float) -> list[dict]:
    """점수 통과 종목만 최대 MAX_WATCH — 부족해도 억지로 채우지 않음."""
    ranked = sorted(pool.values(), key=_score, reverse=True)
    picked = [x for x in ranked if _score(x) >= min_score][:MAX_WATCH]
    if len(picked) < MIN_WATCH and ranked:
        # 하한만 살짝: 점수순으로 MIN_WATCH까지 (그래도 MAX 초과 금지)
        need = min(MIN_WATCH, MAX_WATCH, len(ranked))
        if len(picked) < need:
            seen = {f"{p['exchange']}:{p['symbol']}" for p in picked}
            for x in ranked:
                key = f"{x['exchange']}:{x['symbol']}"
                if key in seen:
                    continue
                picked.append(x)
                seen.add(key)
                if len(picked) >= need:
                    break
    return picked


def _collect_primary(exchanges: list[str], vol_rang: str) -> tuple[dict, dict, list[str]]:
    """1차: 급증·상승·거래량."""
    counts = {"surge": 0, "updown": 0, "tradevol": 0}
    errors: list[str] = []
    pool: dict[str, dict] = {}
    for ex in exchanges:
        try:
            rows = kis_us_api.get_volume_surge(ex, mixn="0", vol_rang=vol_rang)
            if not rows:
                rows = kis_us_api.get_volume_surge(ex, mixn="3", vol_rang=vol_rang)
            counts["surge"] += len(rows)
            pool.update(_merge_pool(rows, "volume_surge", min_rate=-999))  # rate는 나중에
        except Exception as e:
            errors.append(f"surge:{ex}:{e}")
        try:
            rows = kis_us_api.get_updown_rate(ex, gubn="1", vol_rang=vol_rang)
            counts["updown"] += len(rows)
            pool.update(_merge_pool(rows, "updown", min_rate=-999))
        except Exception as e:
            errors.append(f"updown:{ex}:{e}")
        try:
            rows = kis_us_api.get_trade_vol(ex, vol_rang=vol_rang)
            counts["tradevol"] += len(rows)
            pool.update(_merge_pool(rows, "trade_vol", min_rate=-999))
        except Exception as e:
            errors.append(f"tradevol:{ex}:{e}")
    return pool, counts, errors


def _collect_secondary(exchanges: list[str], vol_rang: str) -> tuple[dict, dict, list[str]]:
    """2차: 거래대금·체결강도·신고가."""
    counts = {"pbmn": 0, "power": 0, "highlow": 0}
    errors: list[str] = []
    pool: dict[str, dict] = {}
    for ex in exchanges:
        try:
            rows = kis_us_api.get_trade_pbmn(ex, vol_rang=vol_rang)
            counts["pbmn"] += len(rows)
            pool.update(_merge_pool(rows, "trade_pbmn", min_rate=-999))
        except Exception as e:
            errors.append(f"pbmn:{ex}:{e}")
        try:
            rows = kis_us_api.get_volume_power(ex, nday="3", vol_rang=vol_rang)
            counts["power"] += len(rows)
            pool.update(_merge_pool(rows, "volume_power", min_rate=-999))
        except Exception as e:
            errors.append(f"power:{ex}:{e}")
        try:
            rows = kis_us_api.get_new_highlow(
                ex, gubn="1", gubn2="1", nday="0", vol_rang=vol_rang,
            )
            counts["highlow"] += len(rows)
            pool.update(_merge_pool(rows, "new_high", min_rate=-999))
        except Exception as e:
            errors.append(f"highlow:{ex}:{e}")
    return pool, counts, errors


def _filter_pool(raw_pool: dict[str, dict], *, min_rate: float, rank_mega: bool = True) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key, item in raw_pool.items():
        if float(item.get("rate") or 0) < min_rate:
            continue
        if float(item.get("last") or 0) < MIN_PRICE:
            continue
        if is_mega_cap(item["symbol"], rank=rank_mega):
            continue
        if is_etp(item["symbol"], item.get("name") or ""):
            continue
        out[key] = item
    return out


def run_screening(force: bool = False) -> list[dict]:
    """동적·유동 워치리스트 갱신."""
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
    interval_min = SCREEN_INTERVAL_MIN
    if str(_last_stats.get("mode") or "").startswith("fallback"):
        interval_min = min(interval_min, 5)
    if (
        not force
        and _last_screen_at is not None
        and (now - _last_screen_at).total_seconds() < interval_min * 60
        and _watchlist
    ):
        return list(_watchlist)

    exchanges = ["NAS"]
    if INCLUDE_NYS:
        exchanges.append("NYS")

    mega_n = _refresh_mega_set(exchanges)
    errors: list[str] = []
    counts: dict[str, int] = {}

    # 1차
    raw1, c1, e1 = _collect_primary(exchanges, VOL_RANG)
    counts.update(c1)
    errors.extend(e1)
    if not raw1 and VOL_RANG != "0":
        raw1b, c1b, e1b = _collect_primary(exchanges, "0")
        for k, v in c1b.items():
            counts[k] = counts.get(k, 0) + v
        errors.extend(e1b)
        raw1 = raw1b
    pool = _filter_pool(raw1, min_rate=MIN_RATE)
    used_rate = MIN_RATE
    used_score = MIN_SCORE
    stage = "primary"

    # 1차 완화
    if not pool and MIN_RATE_RELAXED < MIN_RATE:
        pool = _filter_pool(raw1, min_rate=MIN_RATE_RELAXED)
        used_rate = MIN_RATE_RELAXED
        used_score = MIN_SCORE_RELAXED
        stage = "primary_relaxed"

    raw2: dict[str, dict] = {}
    # 2차 확장 순위
    if not pool:
        raw2, c2, e2 = _collect_secondary(exchanges, VOL_RANG)
        counts.update(c2)
        errors.extend(e2)
        pool = _filter_pool(raw2, min_rate=MIN_RATE_RELAXED)
        used_rate = MIN_RATE_RELAXED
        used_score = MIN_SCORE_RELAXED
        stage = "secondary"
        if not pool and VOL_RANG_RELAXED != VOL_RANG:
            raw2b, c2b, e2b = _collect_secondary(exchanges, VOL_RANG_RELAXED)
            for k, v in c2b.items():
                counts[k] = counts.get(k, 0) + v
            errors.extend(e2b)
            raw2.update(raw2b)
            pool = _filter_pool(raw2, min_rate=0.0)
            used_rate = 0.0
            used_score = MIN_SCORE_RELAXED
            stage = "secondary_relaxed"

    # 시총상위 컷 때문에 풀이 비면, 초대형 블록만 제외하고 중형 순위 사용
    if not pool:
        combined = dict(raw1)
        combined.update(raw2)
        pool = _filter_pool(combined, min_rate=0.0, rank_mega=False)
        if pool:
            used_rate = 0.0
            used_score = MIN_SCORE_RELAXED
            stage = "midcap_rank"

    picked = _select_fluid(pool, min_score=used_score)
    if not picked and pool:
        picked = _select_fluid(pool, min_score=MIN_SCORE_RELAXED)
        used_score = MIN_SCORE_RELAXED
        stage = f"{stage}_score_relaxed"
    if not picked and pool:
        # 점수 하한 포기, 상한만 적용 (여전히 그날 순위 기반 · 고정 리스트 아님)
        picked = sorted(pool.values(), key=_score, reverse=True)[:MAX_WATCH]
        stage = f"{stage}_topn"
    if picked:
        _watchlist = picked
        mode = f"dynamic_{stage}"
    else:
        _watchlist = _fallback_watchlist()
        mode = "fallback"

    _last_screen_at = now
    tcount = tradeable_count(_watchlist)
    _last_stats = {
        "mode": mode,
        "count": len(_watchlist),
        "tradeable": tcount,
        "max_watch": MAX_WATCH,
        "min_score": used_score,
        "pool": len(pool),
        "mega_excluded": mega_n,
        "min_rate": used_rate,
        "counts": counts,
        "errors": errors[:3],
        "at": now.strftime("%H:%M"),
    }
    print(
        f"[US스크리너] {mode} 유동{len(_watchlist)}/{MAX_WATCH} "
        f"(풀 {len(pool)} / 매매가능 {tcount} / 메가 {mega_n} / {counts})"
    )
    return list(_watchlist)
