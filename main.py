#!/usr/bin/env python3
"""Entry point for the Hedged Grid Bot.

Usage:
    python main.py                      # default config.yaml
    python main.py --config my.yaml     # custom config
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
    parser = argparse.ArgumentParser(description="Hedged Grid Bot")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    # Logger
    from utils.logger import setup_logger
    log = setup_logger(cfg)
    log.info("=" * 60)
    log.info("Hedged Grid Bot starting")
    log.info("=" * 60)

    # Broker
    from broker.mt5_connector import MT5Connector
    broker = MT5Connector(
        login=int(os.getenv("MT5_LOGIN", "0")),
        password=os.getenv("MT5_PASSWORD", ""),
        server=os.getenv("MT5_SERVER", ""),
        path=os.getenv("MT5_PATH", ""),
    )
    if not broker.connect():
        log.critical("Cannot connect to MT5 — exiting")
        sys.exit(1)

    # Bot
    from core.bot import HedgedGridBot
    bot = HedgedGridBot(broker, cfg)

    # Graceful shutdown
    def _shutdown(signum, frame):
        log.info("Signal %d — shutting down", signum)
        bot.stop()
        broker.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Run
    try:
        bot.start()
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
