"""Top-level bot — manages HedgedGridEngine instances for all symbols.

Spawns one thread per symbol engine so they run independently.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict

from broker.base_broker import BaseBroker
from core.engine import HedgedGridEngine

log = logging.getLogger("hedged_grid.bot")


class HedgedGridBot:
    """Instantiates and manages one engine per enabled symbol."""

    def __init__(self, broker: BaseBroker, cfg: dict):
        self.broker = broker
        self.cfg = cfg
        self.engines: Dict[str, HedgedGridEngine] = {}
        self._threads: Dict[str, threading.Thread] = {}

        general = cfg.get("general", {})
        base_magic = general.get("magic_number", 888001)

        for i, (sym, sym_cfg) in enumerate(cfg.get("symbols", {}).items()):
            if not sym_cfg.get("enabled", False):
                continue
            magic = base_magic + i
            engine = HedgedGridEngine(sym, broker, sym_cfg, magic)
            self.engines[sym] = engine
            log.info("Engine registered: %s (magic %d)", sym, magic)

    def start(self) -> None:
        """Start all engines in separate threads."""
        if not self.engines:
            log.warning("No symbols enabled — nothing to do")
            return

        for sym, engine in self.engines.items():
            t = threading.Thread(
                target=engine.start,
                name=f"engine-{sym}",
                daemon=True,
            )
            self._threads[sym] = t
            t.start()
            log.info("Started thread for %s", sym)

        log.info("Bot running — %d symbol(s)", len(self.engines))

        # Block main thread until interrupted
        try:
            for t in self._threads.values():
                t.join()
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        log.info("Stop requested — closing all engines")
        for engine in self.engines.values():
            engine.stop()

    def status(self) -> dict:
        return {sym: e.status() for sym, e in self.engines.items()}
