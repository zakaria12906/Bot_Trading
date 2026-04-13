"""Logging setup — console + rotating file handler."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logger(cfg: dict) -> logging.Logger:
    log_cfg = cfg.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = log_cfg.get("file", "logs/hedged_grid.log")
    max_bytes = log_cfg.get("max_bytes", 10_485_760)
    backup = log_cfg.get("backup_count", 5)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("hedged_grid")
    root.setLevel(level)

    # Console
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # File
    fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    return root
