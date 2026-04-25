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

# Default user-strategy directory: prefer persistent volume so uploads via
# Mini App survive container redeploys. Falls back to in-image path for local
# dev (volume not mounted).
import os as _os
_VOLUME_DIR = Path("/app/data/user_strategies")
_REPO_DIR = Path("strategy/user")
USER_DIR = _VOLUME_DIR if _os.path.isdir("/app/data") else _REPO_DIR


class Strategy:
    def __init__(self, name: str, module: Any, source: str = "builtin"):
        self.name = name
        self.module = module
        self.source = source            # "builtin" | "user"
        self.default_params = dict(getattr(module, "DEFAULT_PARAMS", {}))
        # Optional UI schema: {"key": {"type": "int", "min": ..., "max": ..., "label": ...}}
        self.param_schema = dict(getattr(module, "PARAM_SCHEMA", {}))
        gen = getattr(module, "generate_signals", None)
        if not callable(gen):
            raise ValueError(f"Strategy {name!r} missing generate_signals")
        self.generate_signals: Callable = gen
        # Live params (overrides default_params). Persisted in journal kv_store.
        self.params: dict = dict(self.default_params)

    def merged_params(self) -> dict:
        return {**self.default_params, **self.params}


class StrategyRegistry:
    def __init__(self, journal=None, global_indicator_cfg: Optional[dict] = None):
        self.journal = journal
        self.strategies: dict[str, Strategy] = {}
        self.active_name: str = "consensus"
        self._load_builtins()
        self._load_user_dir()
        if journal:
            saved = journal.get("active_strategy")
            if saved and saved in self.strategies:
                self.active_name = saved
        # Migration: if no params stored for consensus yet, seed from
        # config.yaml `indicator:` section (the user's existing settings).
        if global_indicator_cfg and "consensus" in self.strategies:
            stored = (journal.get("strategy_params:consensus") if journal else None)
            if not stored:
                self.strategies["consensus"].params.update(global_indicator_cfg)
                if journal:
                    journal.set("strategy_params:consensus",
                                self.strategies["consensus"].params)
        # Load any persisted per-strategy params
        if journal:
            for name, strat in self.strategies.items():
                p = journal.get(f"strategy_params:{name}")
                if isinstance(p, dict):
                    strat.params = dict(p)

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

    def get_params(self, name: str) -> dict:
        s = self.strategies.get(name)
        if not s:
            return {}
        return dict(s.params)

    def set_param(self, name: str, key: str, value: Any) -> bool:
        s = self.strategies.get(name)
        if not s:
            return False
        s.params[key] = value
        if self.journal:
            self.journal.set(f"strategy_params:{name}", s.params)
        return True

    def update_params(self, name: str, params: dict) -> bool:
        s = self.strategies.get(name)
        if not s:
            return False
        s.params.update(params)
        if self.journal:
            self.journal.set(f"strategy_params:{name}", s.params)
        return True

    def list(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "source": s.source,
                "active": s.name == self.active_name,
                "default_params": s.default_params,
                "params": s.params,
                "param_schema": s.param_schema,
            }
            for s in self.strategies.values()
        ]
