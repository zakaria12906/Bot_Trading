"""TradingView webhook receiver.

Runs a lightweight Flask server that accepts POST alerts from TradingView
and feeds them into the bot's handle_webhook_signal() method.

Expected JSON payload from TradingView:
    {
        "secret": "<WEBHOOK_SECRET>",
        "symbol": "EURUSD",
        "direction": "BUY"      // or "SELL"
    }
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

from flask import Flask, request, jsonify

if TYPE_CHECKING:
    from core.bot import GridRecoveryBot

log = logging.getLogger("grid_bot.webhook")

app = Flask(__name__)

_bot_ref: GridRecoveryBot | None = None
_secret: str = ""


def configure(bot: "GridRecoveryBot", secret: str) -> None:
    global _bot_ref, _secret
    _bot_ref = bot
    _secret = secret


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid JSON"}), 400

    if _secret and data.get("secret") != _secret:
        log.warning("Webhook auth failed from %s", request.remote_addr)
        return jsonify({"error": "unauthorized"}), 403

    symbol = data.get("symbol", "").upper()
    direction = data.get("direction", "").upper()

    if not symbol or direction not in ("BUY", "SELL", "LONG", "SHORT"):
        return jsonify({"error": "missing symbol or direction"}), 400

    if direction == "LONG":
        direction = "BUY"
    elif direction == "SHORT":
        direction = "SELL"

    log.info("Webhook signal: %s %s", symbol, direction)

    if _bot_ref is None:
        return jsonify({"error": "bot not initialized"}), 503

    result = _bot_ref.handle_webhook_signal(symbol, direction)
    status_code = 200 if "error" not in result else 400
    return jsonify(result), status_code


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/status", methods=["GET"])
def status():
    if _bot_ref is None:
        return jsonify({"error": "bot not initialized"}), 503

    engines_status = {}
    for sym, eng in _bot_ref.engines.items():
        engines_status[sym] = {
            "has_basket": eng.basket.has_active_basket,
            "trade_count": eng.basket.basket_trade_count() if eng.basket.has_active_basket else 0,
            "net_pnl": round(eng.basket.basket_net_pnl(), 2) if eng.basket.has_active_basket else 0,
            "risk_mode": eng.risk.evaluate_mode(sym).value,
        }
    return jsonify({
        "running": _bot_ref._running,
        "balance": round(_bot_ref.broker.account_balance(), 2),
        "equity": round(_bot_ref.broker.account_equity(), 2),
        "engines": engines_status,
    }), 200


def start_server(host: str, port: int) -> threading.Thread:
    """Launch Flask in a daemon thread so the main bot loop isn't blocked."""
    def _run():
        app.run(host=host, port=port, debug=False, use_reloader=False)

    thread = threading.Thread(target=_run, daemon=True, name="webhook-server")
    thread.start()
    log.info("Webhook server started on %s:%d", host, port)
    return thread
