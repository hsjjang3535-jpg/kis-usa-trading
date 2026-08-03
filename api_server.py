"""HTTP health API (Railway)."""
from __future__ import annotations

import os

from flask import Flask, abort, jsonify, request

import kis_us_api
import market_hours
import us_sim

API_SECRET = os.getenv("API_SECRET", "")
app = Flask(__name__)


def _auth() -> None:
    if API_SECRET and request.headers.get("X-API-Secret") != API_SECRET:
        abort(403)


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
