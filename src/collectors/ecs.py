from __future__ import annotations

from prometheus_client.core import GaugeMetricFamily

from .base import MetricCollector
from .utils import safe_float
from ..providers.base import CloudProvider, MetricResult

ECS_METRICS = {
    "cpu_utilization_percent": ("cloud_ecs_cpu_utilization_percent", "CPU utilization percentage"),
    "memory_utilization_percent": ("cloud_ecs_memory_utilization_percent", "Memory utilization percentage"),
    "network_in_rate_bytes_per_second": ("cloud_ecs_network_in_rate_bytes_per_second", "Network inbound rate in bytes per second"),
    "network_out_rate_bytes_per_second": ("cloud_ecs_network_out_rate_bytes_per_second", "Network outbound rate in bytes per second"),
}

DISK_METRIC = ("cloud_ecs_disk_utilization_percent", "Disk utilization percentage per disk device")

DISK_IO_METRICS = {
    "disk_read_bps": ("cloud_ecs_disk_read_bps_bytes_per_second", "Disk read throughput in bytes per second"),
    "disk_write_bps": ("cloud_ecs_disk_write_bps_bytes_per_second", "Disk write throughput in bytes per second"),
    "disk_read_iops": ("cloud_ecs_disk_read_iops_per_second", "Disk read IOPS"),
    "disk_write_iops": ("cloud_ecs_disk_write_iops_per_second", "Disk write IOPS"),
}

LABEL_NAMES = ["cloud", "instance_id", "instance_name", "region"]

DISK_LABEL_NAMES = ["cloud", "instance_id", "instance_name", "region", "disk"]


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

        # 磁盘IO指标使用独立的 Gauge，包含 disk 标签
        disk_io_gauges: dict[str, GaugeMetricFamily] = {}
        for attr_name, (metric_name, help_text) in DISK_IO_METRICS.items():
            disk_io_gauges[attr_name] = GaugeMetricFamily(metric_name, help_text, labels=DISK_LABEL_NAMES)

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
                    gauge.add_metric(base_labels, safe_float(raw_value))

                # 磁盘指标：遍历每块磁盘，添加 disk 标签
                for disk_id, usage_percent in metric_data.disk_usage.items():
                    disk_labels = base_labels + [disk_id]
                    disk_gauge.add_metric(disk_labels, safe_float(usage_percent))

                # 磁盘IO指标：遍历每块磁盘，添加 disk 标签
                for attr_name, gauge in disk_io_gauges.items():
                    disk_io_data = getattr(metric_data, attr_name, {})
                    for disk_id, value in disk_io_data.items():
                        disk_labels = base_labels + [disk_id]
                        gauge.add_metric(disk_labels, safe_float(value))

        result_gauges = list(gauges.values())
        result_gauges.append(disk_gauge)
        result_gauges.extend(disk_io_gauges.values())
        return result_gauges
