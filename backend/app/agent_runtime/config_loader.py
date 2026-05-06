"""配置加载工具，提供带缓存的 JSON 读取能力。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .core.config_validator import validate_intent_rules, validate_plan_strategy

logger = logging.getLogger(__name__)


class ConfigLoader:
    """加载和缓存配置文件。"""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        self._config_dir = config_dir or Path(__file__).parent / "configs"
        self._cache: Dict[str, Any] = {}
        self._mtimes: Dict[str, float] = {}

    def load_intent_rules(self) -> Dict[str, Any]:
        """加载意图识别配置。"""
        return self._load("intent_rules.json", validator=validate_intent_rules)

    def load_plan_strategy(self) -> Dict[str, Any]:
        """加载任务计划配置。"""
        return self._load("plan_strategy.json", validator=validate_plan_strategy)

    def _load(self, filename: str, validator: Optional[Callable[[Dict[str, Any]], Any]] = None) -> Dict[str, Any]:
        path = self._config_dir / filename
        if not path.exists():
            logger.error("Config file not found: %s", path)
            return {}

        try:
            mtime = path.stat().st_mtime
        except OSError as error:
            logger.error("Failed to stat config file %s: %s", path, error)
            return {}

        cached = self._cache.get(filename)
        if cached is not None and self._mtimes.get(filename) == mtime:
            return cached

        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except json.JSONDecodeError as error:
            logger.error("Failed to parse config file %s: %s", path, error)
            return {}

        try:
            if validator:
                validator(data)
        except ValueError as error:
            logger.error("Config file %s failed validation: %s", path, error)
            return {}

        self._cache[filename] = data
        self._mtimes[filename] = mtime
        logger.info("Loaded config %s (version=%s)", filename, data.get("version", "unknown"))
        return data


# 全局实例，供各模块复用
config_loader = ConfigLoader()


