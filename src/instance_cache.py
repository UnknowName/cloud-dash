from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from .config import InstanceCacheConfig
from .providers.base import InstanceInfo

logger = logging.getLogger(__name__)


class InstanceCache:
    def __init__(self, config: InstanceCacheConfig) -> None:
        self._enabled = config.enabled
        self._ttl_seconds = config.ttl_seconds
        self._cache_dir = Path(config.dir)

    def load(self, provider_name: str) -> list[InstanceInfo] | None:
        if not self._enabled:
            return None

        path = self._cache_path(provider_name)
        if not path.exists():
            logger.debug("实例缓存文件不存在: %s", path)
            return None

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("实例缓存文件损坏，将重新获取: %s, 原因: %s", path, e)
            return None

        cached_at = raw.get("timestamp", 0.0)
        if time.time() - cached_at > self._ttl_seconds:
            logger.info("实例缓存已过期（缓存时间: %.0f, TTL: %d秒）", cached_at, self._ttl_seconds)
            self._remove_file(path)
            return None

        instances_data = raw.get("instances", [])
        instances = [InstanceInfo(**item) for item in instances_data]
        logger.info(
            "从本地缓存加载实例列表成功: provider=%s, 实例数=%d, 缓存时间=%.0f",
            provider_name, len(instances), cached_at,
        )
        return instances

    def save(self, provider_name: str, instances: list[InstanceInfo]) -> None:
        if not self._enabled:
            return

        self._ensure_dir()
        path = self._cache_path(provider_name)
        payload = {
            "timestamp": time.time(),
            "instances": [asdict(ins) for ins in instances],
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("实例列表已写入本地缓存: provider=%s, 实例数=%d", provider_name, len(instances))
        except OSError as e:
            logger.error("写入实例缓存失败: %s, 原因: %s", path, e)

    def _cache_path(self, provider_name: str) -> Path:
        return self._cache_dir / f"{provider_name}.json"

    def _ensure_dir(self) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _remove_file(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("删除过期缓存文件失败: %s, 原因: %s", path, e)
