"""Strategy plugin system.

Built-in strategy: consensus 4/5 (already in strategy/consensus.py).
User strategies: drop *.py file into strategies/user/ directory or upload
via Mini App. Each must export `generate_signals(candles, params)` with
the same signature as strategy.consensus.generate_signals.

The active strategy is stored in journal kv_store and can be switched
live via API.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Default user-strategy directory (created if absent)
USER_DIR = Path("strategy/user")


class Strategy:
    def __init__(self, name: str, module: Any, source: str = "builtin"):
        self.name = name
        self.module = module
        self.source = source            # "builtin" | "user"
        self.default_params = getattr(module, "DEFAULT_PARAMS", {})
        # callable: (candles, params) -> (signals, diag)
        gen = getattr(module, "generate_signals", None)
        if not callable(gen):
            raise ValueError(f"Strategy {name!r} missing generate_signals")
        self.generate_signals: Callable = gen


class StrategyRegistry:
    def __init__(self, journal=None):
        self.journal = journal
        self.strategies: dict[str, Strategy] = {}
        self.active_name: str = "consensus"
        self._load_builtins()
        self._load_user_dir()
        if journal:
            saved = journal.get("active_strategy")
            if saved and saved in self.strategies:
                self.active_name = saved

    def _load_builtins(self):
        try:
            from strategy import consensus
            self.strategies["consensus"] = Strategy("consensus", consensus, "builtin")
            logger.info("loaded builtin strategy: consensus")
        except Exception:
            logger.exception("failed to load builtin consensus")

    def _load_user_dir(self):
        USER_DIR.mkdir(parents=True, exist_ok=True)
        for path in USER_DIR.glob("*.py"):
            if path.name.startswith("_"):
                continue
            try:
                self.load_from_file(path)
            except Exception:
                logger.exception("user strategy %s failed to load", path.name)

    def load_from_file(self, path: Path) -> Strategy:
        name = path.stem
        spec = importlib.util.spec_from_file_location(f"strategy.user.{name}", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)   # type: ignore[union-attr]
        strat = Strategy(name, module, "user")
        self.strategies[name] = strat
        logger.info("loaded user strategy: %s", name)
        return strat

    def add_user_code(self, name: str, code: str) -> Strategy:
        """Save Python code to strategy/user/<name>.py and load it."""
        if not name.replace("_", "").isalnum():
            raise ValueError("name must be alphanumeric / underscores")
        path = USER_DIR / f"{name}.py"
        path.write_text(code, encoding="utf-8")
        # If module already loaded, reload
        existing = self.strategies.get(name)
        if existing and existing.source == "user":
            try:
                importlib.reload(existing.module)
            except Exception:
                logger.exception("reload failed, falling back to fresh load")
        return self.load_from_file(path)

    def remove_user_strategy(self, name: str) -> bool:
        s = self.strategies.get(name)
        if not s or s.source != "user":
            return False
        try:
            path = USER_DIR / f"{name}.py"
            if path.exists():
                path.unlink()
            del self.strategies[name]
            if self.active_name == name:
                self.active_name = "consensus"
                if self.journal:
                    self.journal.set("active_strategy", "consensus")
            return True
        except Exception:
            logger.exception("remove failed")
            return False

    def set_active(self, name: str) -> bool:
        if name not in self.strategies:
            return False
        self.active_name = name
        if self.journal:
            self.journal.set("active_strategy", name)
        logger.info("active strategy → %s", name)
        return True

    def get_active(self) -> Strategy:
        return self.strategies.get(self.active_name) or self.strategies["consensus"]

    def list(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "source": s.source,
                "active": s.name == self.active_name,
                "default_params": s.default_params,
            }
            for s in self.strategies.values()
        ]
