from __future__ import annotations

import logging
import sys
from pathlib import Path

from src.cache import MetricsCache
from src.collectors.base import MetricCollector
from src.collectors.ecs import EcsCollector
from src.collectors.balance import BalanceCollector
from src.config import load_config, ProviderConfig, ThreadPoolConfig, CollectionConfig, InstanceCacheConfig
from src.exporter import Exporter
from src.instance_cache import InstanceCache
from src.providers.aliyun import AliyunProvider
from src.providers.base import CloudProvider
from src.providers.huawei import HuaweiProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Provider 类型到类的映射表，新增云平台只需在此注册
PROVIDER_MAP: dict[str, type[CloudProvider]] = {
    "aliyun": AliyunProvider,
    "huawei": HuaweiProvider,
}

# Collector 类型到类的映射表，新增资源类型只需在此注册
COLLECTOR_MAP: dict[str, type[MetricCollector]] = {
    "ecs": EcsCollector,
    "balance": BalanceCollector,
}


def create_providers(
    provider_configs: list[ProviderConfig],
    pool_config: ThreadPoolConfig | None = None,
    collection_config: CollectionConfig | None = None,
    instance_cache: InstanceCache | None = None,
) -> list[CloudProvider]:
    pool_cfg = pool_config or ThreadPoolConfig()
    collection_cfg = collection_config or CollectionConfig()
    collection_interval_seconds = collection_cfg.to_seconds()
    providers = []
    for cfg in provider_configs:
        provider_cls = PROVIDER_MAP.get(cfg.type)
        if not provider_cls:
            logger.warning("未知的云平台类型: %s，跳过", cfg.type)
            continue
        provider = provider_cls(
            name=cfg.name,
            region=cfg.region,
            credentials={
                "access_key_id": cfg.credentials.access_key_id,
                "access_key_secret": cfg.credentials.access_key_secret,
                "project_id": cfg.credentials.project_id,
            },
            max_workers=pool_cfg.max_workers,
            max_retries=pool_cfg.max_retries,
            retry_delay=pool_cfg.retry_delay,
            collection_interval_seconds=collection_interval_seconds,
            include_name=cfg.include_name,
            instance_cache=instance_cache,
        )
        providers.append(provider)
    return providers


def main() -> None:
    # 支持通过命令行参数指定配置文件路径，默认为 ./config.yaml
    config_path = Path("./config.yaml")
    if len(sys.argv) > 1:
        config_path = Path(sys.argv[1])

    config = load_config(config_path)
    logger.info(
        "配置加载完成，端口=%d, 缓存TTL=%d秒, 采集间隔=%d秒",
        config.server.port, config.cache.ttl_seconds, config.collection.to_seconds(),
    )

    # 初始化实例列表文件缓存
    instance_cache = InstanceCache(config.instance_cache)
    logger.info(
        "实例列表缓存: enabled=%s, TTL=%d秒, 目录=%s",
        config.instance_cache.enabled, config.instance_cache.ttl_seconds, config.instance_cache.dir,
    )

    # 初始化所有 Provider
    providers = create_providers(
        config.providers,
        pool_config=config.thread_pool,
        collection_config=config.collection,
        instance_cache=instance_cache,
    )
    logger.info("已初始化 %d 个云平台 Provider", len(providers))

    # 根据配置创建采集器，通过 COLLECTOR_MAP 注册表匹配类型
    # 需要 providers 的采集器统一传入 providers 列表
    collectors = []
    for name in config.collectors:
        collector_cls = COLLECTOR_MAP.get(name)
        if not collector_cls:
            logger.warning("未知的采集器类型: %s，跳过", name)
            continue
        if name in ("ecs", "balance"):
            if name == "balance":
                collectors.append(collector_cls(providers, cache_ttl_seconds=config.cache.balance_cache_ttl_seconds))
            else:
                collectors.append(collector_cls(providers))
        else:
            collectors.append(collector_cls())
    logger.info("已初始化 %d 个指标采集器", len(collectors))

    # 初始化缓存（使用采集间隔作为刷新频率，而非独立的 TTL）
    collection_interval = config.collection.to_seconds()
    cache = MetricsCache(collectors, collection_interval_seconds=collection_interval)

    # 启动 Exporter 服务
    exporter = Exporter(cache, providers=providers, port=config.server.port)
    exporter.run()


if __name__ == "__main__":
    main()
