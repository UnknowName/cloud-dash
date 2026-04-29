from __future__ import annotations

import math

from prometheus_client.core import GaugeMetricFamily

from .base import MetricCollector
from ..providers.base import CloudProvider, MetricResult

ECS_METRICS = {
    "cpu_utilization_percent": ("cloud_ecs_cpu_utilization_percent", "CPU utilization percentage"),
    "memory_utilization_percent": ("cloud_ecs_memory_utilization_percent", "Memory utilization percentage"),
    "network_in_rate_bytes_per_second": ("cloud_ecs_network_in_rate_bytes_per_second", "Network inbound rate in bytes per second"),
    "network_out_rate_bytes_per_second": ("cloud_ecs_network_out_rate_bytes_per_second", "Network outbound rate in bytes per second"),
}

DISK_METRIC = ("cloud_ecs_disk_utilization_percent", "Disk utilization percentage per disk device")

LABEL_NAMES = ["cloud", "instance_id", "instance_name", "region"]

DISK_LABEL_NAMES = ["cloud", "instance_id", "instance_name", "region", "disk"]


def _safe_float(value: object) -> float:
    """将指标值安全转换为有效浮点数，确保符合 Prometheus 规范"""
    if value is None:
        return 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isinf(f) or math.isnan(f):
        return f
    return f


class EcsCollector(MetricCollector):
    def __init__(self, providers: list[CloudProvider]) -> None:
        self._providers = providers

    def collect(self) -> list[GaugeMetricFamily]:
        gauges: dict[str, GaugeMetricFamily] = {}
        for attr_name, (metric_name, help_text) in ECS_METRICS.items():
            gauges[attr_name] = GaugeMetricFamily(metric_name, help_text, labels=LABEL_NAMES)

        # 磁盘指标使用独立的 Gauge，包含 disk 标签
        disk_metric_name, disk_help_text = DISK_METRIC
        disk_gauge = GaugeMetricFamily(disk_metric_name, disk_help_text, labels=DISK_LABEL_NAMES)

        for provider in self._providers:
            result = provider.collect()
            for metric_data in result.metrics:
                base_labels = [
                    result.provider_type,
                    metric_data.instance.instance_id,
                    metric_data.instance.instance_name,
                    metric_data.instance.region,
                ]
                # 非磁盘指标：直接取标量值
                for attr_name, gauge in gauges.items():
                    raw_value = getattr(metric_data, attr_name, 0.0)
                    gauge.add_metric(base_labels, _safe_float(raw_value))

                # 磁盘指标：遍历每块磁盘，添加 disk 标签
                for disk_id, usage_percent in metric_data.disk_usage.items():
                    disk_labels = base_labels + [disk_id]
                    disk_gauge.add_metric(disk_labels, _safe_float(usage_percent))

        result_gauges = list(gauges.values())
        result_gauges.append(disk_gauge)
        return result_gauges
