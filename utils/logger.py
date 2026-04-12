import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logger(cfg: dict) -> logging.Logger:
    log_cfg = cfg.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = log_cfg.get("file", "logs/grid_bot.log")
    max_bytes = log_cfg.get("max_bytes", 10_485_760)
    backup_count = log_cfg.get("backup_count", 5)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logger = logging.getLogger("grid_bot")
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger
