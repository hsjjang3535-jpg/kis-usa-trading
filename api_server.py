"""HTTP health API (Railway)."""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, request

import kis_us_api
import market_hours
import us_sim

KST = ZoneInfo("Asia/Seoul")
API_SECRET = os.getenv("API_SECRET", "")
app = Flask(__name__)
_started_at = datetime.now(KST).isoformat()
_last_loop_at: str | None = None


def touch_loop() -> None:
    """메인 루프 생존 신호 (헬스체크·프로세스 감시용)."""
    global _last_loop_at
    _last_loop_at = datetime.now(KST).isoformat()


def _loop_age_sec() -> float | None:
    if not _last_loop_at:
        return None
    try:
        ts = datetime.fromisoformat(_last_loop_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=KST)
        return (datetime.now(KST) - ts).total_seconds()
    except (TypeError, ValueError):
        return None


def _auth() -> None:
    if API_SECRET and request.headers.get("X-API-Secret") != API_SECRET:
        abort(403)


@app.get("/healthz")
def healthz():
    """Railway 헬스체크 — 인증 없음."""
    age = _loop_age_sec()
    stale_limit = int(os.getenv("US_LOOP_STALE_SEC", "300"))
    stale = age is not None and age > stale_limit
    if stale and market_hours.is_us_regular_session():
        return jsonify({
            "ok": False,
            "service": "kis-us-trading-bot",
            "stale": True,
            "loop_age_sec": age,
            "last_loop": _last_loop_at,
        }), 503
    return jsonify({
        "ok": True,
        "service": "kis-us-trading-bot",
        "started_at": _started_at,
        "last_loop": _last_loop_at,
        "loop_age_sec": age,
        "session": market_hours.session_label(),
    })


@app.get("/health")
def health():
    _auth()
    try:
        acct = kis_us_api.get_account_info()
    except Exception as e:
        acct = {"error": str(e)}
    return jsonify({
        "ok": True,
        "service": "kis-us-trading-bot",
        "account": acct,
        "session": market_hours.session_label(),
        "us_regular": market_hours.is_us_regular_session(),
        "sim_enabled": us_sim.is_enabled(),
        "sim_open": us_sim.get_open(),
        "live_orders": os.getenv("ENABLE_US_LIVE_ORDERS", "false").lower() == "true",
    })


def start_api_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
