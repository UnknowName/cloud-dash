from __future__ import annotations

import logging
import math
import time

from prometheus_client.core import GaugeMetricFamily

from .base import MetricCollector
from ..providers.base import CloudProvider

logger = logging.getLogger(__name__)

RESOURCE_PACKAGE_METRICS = {
    "remaining_percent": (
        "cloud_resource_package_remaining_percent",
        "Resource package remaining usage percentage",
    ),
    "total_amount": (
        "cloud_resource_package_total_amount",
        "Resource package total amount",
    ),
    "used_amount": (
        "cloud_resource_package_used_amount",
        "Resource package used amount (total - remaining)",
    ),
}

LABEL_NAMES = [
    "cloud", "provider_name", "package_name", "instance_id",
    "region", "status", "commodity_code", "unit",
]

DEFAULT_RESOURCE_PACKAGE_CACHE_TTL = 1800


class ResourcePackageCollector(MetricCollector):
    no_cache = True

    def __init__(
        self,
        providers: list[CloudProvider],
        cache_ttl_seconds: int = DEFAULT_RESOURCE_PACKAGE_CACHE_TTL,
    ) -> None:
        self._providers = providers
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cached_metrics: list[GaugeMetricFamily] = []
        self._cache_timestamp: float = 0.0

    def collect(self) -> list[GaugeMetricFamily]:
        now = time.monotonic()
        if self._cached_metrics and (now - self._cache_timestamp) < self._cache_ttl_seconds:
            return self._cached_metrics

        gauges = _build_gauges()
        has_any_data = False

        for provider in self._providers:
            try:
                packages = provider.get_resource_packages()
            except Exception as e:
                logger.error(
                    "资源包采集失败 (provider=%s)，跳过该提供商: %s",
                    provider.name, e,
                )
                continue

            for pkg in packages:
                if pkg.status != "Available":
                    continue
                if pkg.total_amount > 0 and pkg.remaining_amount <= 0:
                    continue

                labels = [
                    provider.provider_type,
                    provider.name,
                    pkg.name,
                    pkg.instance_id,
                    pkg.region,
                    pkg.status,
                    pkg.commodity_code,
                    pkg.unit,
                ]
                gauges["remaining_percent"].add_metric(labels, _safe_float(pkg.remaining_percent))
                gauges["total_amount"].add_metric(labels, _safe_float(pkg.total_amount))
                used = max(pkg.total_amount - pkg.remaining_amount, 0.0)
                gauges["used_amount"].add_metric(labels, _safe_float(used))
                has_any_data = True

        new_metrics = list(gauges.values())
        new_has_samples = any(len(g.samples) > 0 for g in new_metrics)
        old_has_samples = any(len(g.samples) > 0 for g in self._cached_metrics)

        # 仅当新采集无数据但旧缓存有数据时保留旧缓存，避免指标消失
        if not new_has_samples and old_has_samples:
            logger.warning("本轮资源包采集无数据，保留上次缓存")
            return self._cached_metrics

        self._cached_metrics = new_metrics
        self._cache_timestamp = now
        return self._cached_metrics


def _build_gauges() -> dict[str, GaugeMetricFamily]:
    gauges: dict[str, GaugeMetricFamily] = {}
    for attr_name, (metric_name, help_text) in RESOURCE_PACKAGE_METRICS.items():
        gauges[attr_name] = GaugeMetricFamily(metric_name, help_text, labels=LABEL_NAMES)
    return gauges


def _safe_float(value: object) -> float:
    if value is None:
        return 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isinf(f) or math.isnan(f):
        return 0.0
    return f
