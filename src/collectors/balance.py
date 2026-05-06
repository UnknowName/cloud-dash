from __future__ import annotations

import time

from prometheus_client.core import GaugeMetricFamily

from .base import MetricCollector
from ..providers.base import CloudProvider

BALANCE_METRICS = {
    "available_amount": ("cloud_account_available_amount", "Available account balance in yuan"),
    "available_cash_amount": ("cloud_account_available_cash_amount", "Available cash balance in yuan"),
    "credit_amount": ("cloud_account_credit_amount", "Credit limit in yuan"),
}

LABEL_NAMES = ["cloud", "provider_name", "currency"]

DEFAULT_BALANCE_CACHE_TTL = 1800


class BalanceCollector(MetricCollector):
    no_cache = True

    def __init__(
        self,
        providers: list[CloudProvider],
        cache_ttl_seconds: int = DEFAULT_BALANCE_CACHE_TTL,
    ) -> None:
        self._providers = providers
        self._cache_ttl_seconds = cache_ttl_seconds
        # 余额指标缓存：避免每次 Prometheus 抓取都调用云 API
        self._cached_metrics: list[GaugeMetricFamily] = []
        self._cache_timestamp: float = 0.0

    def collect(self) -> list[GaugeMetricFamily]:
        now = time.monotonic()
        # 缓存有效期内直接返回缓存数据
        if self._cached_metrics and (now - self._cache_timestamp) < self._cache_ttl_seconds:
            return self._cached_metrics

        gauges = _build_gauges()
        for provider in self._providers:
            balance = provider.get_balance()
            if balance is None:
                continue

            labels = [provider.provider_type, provider.name, balance.currency]
            for attr_name, gauge in gauges.items():
                value = getattr(balance, attr_name, 0.0)
                gauge.add_metric(labels, _safe_float(value))

        self._cached_metrics = list(gauges.values())
        self._cache_timestamp = now
        return self._cached_metrics


def _build_gauges() -> dict[str, GaugeMetricFamily]:
    gauges: dict[str, GaugeMetricFamily] = {}
    for attr_name, (metric_name, help_text) in BALANCE_METRICS.items():
        gauges[attr_name] = GaugeMetricFamily(metric_name, help_text, labels=LABEL_NAMES)
    return gauges


def _safe_float(value: object) -> float:
    if value is None:
        return 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN check
        return 0.0
    return f
