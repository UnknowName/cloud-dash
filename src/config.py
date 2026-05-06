from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# 采集间隔支持的时间单位及其对应的秒数换算
_VALID_UNITS = {"seconds": 1, "minutes": 60, "hours": 3600}
_DEFAULT_INTERVAL_SECONDS = 300
MIN_COLLECTION_INTERVAL_SECONDS = 60


@dataclass
class ServerConfig:
    port: int = 9100


@dataclass
class CacheConfig:
    ttl_seconds: int = 60
    balance_cache_ttl_seconds: int = 1800


@dataclass
class InstanceCacheConfig:
    enabled: bool = True
    ttl_seconds: int = 86400
    dir: str = "./cache/instances"


@dataclass
class CollectionConfig:
    # 采集周期数值，需配合 unit 一起使用
    interval: int = 5
    # 时间单位，支持: seconds, minutes, hours
    unit: str = "minutes"

    def to_seconds(self) -> int:
        """将配置的采集间隔转换为秒，无效配置回退为默认5分钟，最低不低于60秒"""
        multiplier = _VALID_UNITS.get(self.unit)
        if multiplier is None:
            logger.warning(
                "不支持的采集间隔单位 '%s'，支持: %s，回退为默认5分钟",
                self.unit, list(_VALID_UNITS.keys()),
            )
            return _DEFAULT_INTERVAL_SECONDS
        if self.interval <= 0:
            logger.warning("采集间隔必须为正数，当前值: %d，回退为默认5分钟", self.interval)
            return _DEFAULT_INTERVAL_SECONDS
        result = self.interval * multiplier
        if result < MIN_COLLECTION_INTERVAL_SECONDS:
            logger.warning(
                "采集间隔 %d 秒低于最小限制 %d 秒，已自动调整为最小间隔以避免云平台限流",
                result, MIN_COLLECTION_INTERVAL_SECONDS,
            )
            return MIN_COLLECTION_INTERVAL_SECONDS
        return result


@dataclass
class CredentialsConfig:
    access_key_id: str = ""
    access_key_secret: str = ""
    project_id: str = ""


@dataclass
class ProviderConfig:
    type: str = ""
    name: str = ""
    region: str = ""
    include_name: list[str] = field(default_factory=list)
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)


@dataclass
class ThreadPoolConfig:
    max_workers: int = 5
    max_retries: int = 3
    retry_delay: float = 1.0


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    instance_cache: InstanceCacheConfig = field(default_factory=InstanceCacheConfig)
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    thread_pool: ThreadPoolConfig = field(default_factory=ThreadPoolConfig)
    providers: list[ProviderConfig] = field(default_factory=list)
    collectors: list[str] = field(default_factory=lambda: ["ecs"])


def _validate_provider(provider: ProviderConfig, index: int) -> None:
    required = {"type": provider.type, "name": provider.name, "region": provider.region}
    for field_name, value in required.items():
        if not value:
            raise ValueError(f"providers[{index}].{field_name} 不能为空")
    if not provider.credentials.access_key_id:
        raise ValueError(f"providers[{index}].credentials.access_key_id 不能为空")
    if not provider.credentials.access_key_secret:
        raise ValueError(f"providers[{index}].credentials.access_key_secret 不能为空")


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path)
    if not path.exists():
        return AppConfig()

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    server_cfg = ServerConfig(**raw.get("server", {}))
    cache_cfg = CacheConfig(**raw.get("cache", {}))
    instance_cache_cfg = InstanceCacheConfig(**raw.get("instance_cache", {}))
    collection_cfg = CollectionConfig(**raw.get("collection", {}))
    thread_pool_cfg = ThreadPoolConfig(**raw.get("thread_pool", {}))

    providers = []
    for i, p in enumerate(raw.get("providers", [])):
        # 兼容旧配置：include_name 为字符串时自动转为单元素列表
        raw_include = p.get("include_name")
        if isinstance(raw_include, str):
            p["include_name"] = [raw_include] if raw_include else []
        elif raw_include is None:
            p["include_name"] = []
        creds_data = p.pop("credentials", {})
        creds = CredentialsConfig(**creds_data)
        provider_cfg = ProviderConfig(credentials=creds, **p)
        _validate_provider(provider_cfg, i)
        providers.append(provider_cfg)

    collectors = raw.get("collectors", ["ecs"])

    return AppConfig(
        server=server_cfg,
        cache=cache_cfg,
        instance_cache=instance_cache_cfg,
        collection=collection_cfg,
        thread_pool=thread_pool_cfg,
        providers=providers,
        collectors=collectors,
    )
