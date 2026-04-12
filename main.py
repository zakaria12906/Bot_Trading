#!/usr/bin/env python3
"""Entry point for the Fixed-Lot Grid Recovery Bot.

Usage:
    python main.py                      # default config.yaml
    python main.py --config my.yaml     # custom config path
"""

from __future__ import annotations

import argparse
import os
import signal
import sys

import yaml
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed-Lot Grid Recovery Bot")
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to the YAML configuration file",
    )
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    # ------------------------------------------------------------------
    # Logger
    # ------------------------------------------------------------------
    from utils.logger import setup_logger
    log = setup_logger(cfg)
    log.info("=" * 60)
    log.info("Fixed-Lot Grid Recovery Bot starting")
    log.info("=" * 60)

    # ------------------------------------------------------------------
    # Broker
    # ------------------------------------------------------------------
    from broker.mt5_connector import MT5Connector

    mt5_login = int(os.getenv("MT5_LOGIN", "0"))
    mt5_password = os.getenv("MT5_PASSWORD", "")
    mt5_server = os.getenv("MT5_SERVER", "")
    mt5_path = os.getenv("MT5_PATH", "")

    broker = MT5Connector(mt5_login, mt5_password, mt5_server, mt5_path)
    if not broker.connect():
        log.critical("Cannot connect to MT5 — exiting")
        sys.exit(1)

    # ------------------------------------------------------------------
    # News filter (shared across all symbol engines)
    # ------------------------------------------------------------------
    from filters.news_filter import NewsFilter
    news_filter = NewsFilter(cfg.get("news", {}))
    news_filter.refresh()

    # Background news refresh
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler(daemon=True)
    refresh_sec = cfg.get("news", {}).get("refresh_interval_sec", 3600)
    scheduler.add_job(news_filter.refresh, "interval", seconds=refresh_sec)
    scheduler.start()

    # ------------------------------------------------------------------
    # Bot
    # ------------------------------------------------------------------
    from core.bot import GridRecoveryBot
    bot = GridRecoveryBot(broker, cfg, news_filter)

    # ------------------------------------------------------------------
    # Webhook server (optional)
    # ------------------------------------------------------------------
    wh_cfg = cfg.get("webhook", {})
    if wh_cfg.get("enabled", False):
        from webhook.server import configure, start_server
        secret = os.getenv("WEBHOOK_SECRET", wh_cfg.get("secret", ""))
        configure(bot, secret)
        start_server(
            host=os.getenv("WEBHOOK_HOST", wh_cfg.get("host", "0.0.0.0")),
            port=int(os.getenv("WEBHOOK_PORT", wh_cfg.get("port", 5000))),
        )

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    def _shutdown(signum, frame):
        log.info("Signal %d received — shutting down", signum)
        bot.stop()
        broker.shutdown()
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    try:
        bot.start()
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
