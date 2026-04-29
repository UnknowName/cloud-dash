from __future__ import annotations

import json
import logging
import time
from concurrent.futures import Future

from alibabacloud_cms20190101 import models as cms_models
from alibabacloud_cms20190101.client import Client as CmsClient
from alibabacloud_ecs20140526 import models as ecs_models
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

from .base import CloudProvider, InstanceInfo, MetricData, DEFAULT_COLLECTION_INTERVAL_SECONDS
from ..pool import DEFAULT_MAX_WORKERS, DEFAULT_MAX_RETRIES, DEFAULT_RETRY_DELAY
from ..instance_cache import InstanceCache

logger = logging.getLogger(__name__)

ALIYUN_METRIC_NAMES = {
    "cpu_utilization_percent": "CPUUtilization",
    "memory_utilization_percent": "memory_usedutilization",
    "network_in_rate_bytes_per_second": "VPC_PublicIP_InternetInRate",
    "network_out_rate_bytes_per_second": "VPC_PublicIP_InternetOutRate",
}

METRIC_PRIORITIES = {
    "cpu_utilization_percent": 1,
    "memory_utilization_percent": 2,
    "disk_usage": 3,
    "network_in_rate_bytes_per_second": 4,
    "network_out_rate_bytes_per_second": 4,
}


class AliyunProvider(CloudProvider):
    def __init__(
        self,
        name: str,
        region: str,
        credentials: dict,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        collection_interval_seconds: int = DEFAULT_COLLECTION_INTERVAL_SECONDS,
        include_name: str = "",
        instance_cache: InstanceCache | None = None,
    ) -> None:
        super().__init__(
            name, region, credentials, max_workers, max_retries,
            retry_delay, collection_interval_seconds, include_name,
            instance_cache=instance_cache,
        )
        self._ecs_client = self._create_ecs_client()
        self._cms_client = self._create_cms_client()

    def _create_ecs_client(self) -> EcsClient:
        config = open_api_models.Config(
            access_key_id=self.credentials.get("access_key_id", ""),
            access_key_secret=self.credentials.get("access_key_secret", ""),
            region_id=self.region,
        )
        return EcsClient(config)

    def _create_cms_client(self) -> CmsClient:
        config = open_api_models.Config(
            access_key_id=self.credentials.get("access_key_id", ""),
            access_key_secret=self.credentials.get("access_key_secret", ""),
            region_id=self.region,
            endpoint=f"cms.{self.region}.aliyuncs.com",
        )
        return CmsClient(config)

    def list_instances(self) -> list[InstanceInfo]:
        page_size = 100
        page_number = 1
        all_instances: list[InstanceInfo] = []
        runtime = util_models.RuntimeOptions()

        # 阿里云 instance_name 参数支持 * 通配符模糊匹配
        instance_name_filter = f"*{self.include_name}*" if self.include_name else None

        try:
            while True:
                request = ecs_models.DescribeInstancesRequest(
                    region_id=self.region,
                    page_number=page_number,
                    page_size=page_size,
                    instance_name=instance_name_filter,
                )
                response = self._ecs_client.describe_instances_with_options(request, runtime)
                page_instances = response.body.instances.instance
                for ins in page_instances:
                    all_instances.append(InstanceInfo(
                        instance_id=ins.instance_id,
                        instance_name=ins.instance_name,
                        region=self.region,
                    ))
                # 当前页数据量小于 page_size，说明已是最后一页
                if len(page_instances) < page_size:
                    break
                page_number += 1

            logger.info("阿里云获取实例列表完成，共 %d 个实例", len(all_instances))
            return all_instances
        except Exception as e:
            logger.error("阿里云获取实例列表失败: %s", e)
            return []

    def get_metrics(self, instances: list[InstanceInfo]) -> list[MetricData]:
        end_time = int(time.time() * 1000)
        start_time = end_time - self.collection_interval_seconds * 1000

        cycle_id = self.begin_collection_cycle()

        # 非磁盘指标：每个实例每个指标提交一个查询任务
        futures: dict[tuple[str, str], Future] = {}
        for instance in instances:
            for attr_name, metric_name in ALIYUN_METRIC_NAMES.items():
                priority = METRIC_PRIORITIES.get(attr_name, 5)
                future = self._pool.submit(
                    self._query_metric,
                    instance.instance_id, metric_name, start_time, end_time,
                    priority=priority,
                    cycle_id=cycle_id,
                )
                futures[(instance.instance_id, attr_name)] = future

        # 磁盘指标：每个实例提交一个查询任务，返回每块磁盘的使用率
        disk_futures: dict[str, Future] = {}
        for instance in instances:
            priority = METRIC_PRIORITIES.get("disk_usage", 5)
            future = self._pool.submit(
                self._query_disk_usage,
                instance.instance_id, start_time, end_time,
                priority=priority,
                cycle_id=cycle_id,
            )
            disk_futures[instance.instance_id] = future

        result = []
        for instance in instances:
            metric_values: dict[str, float] = {}
            for attr_name in ALIYUN_METRIC_NAMES:
                future = futures.get((instance.instance_id, attr_name))
                if not future:
                    continue
                try:
                    value = future.result()
                    if value is not None:
                        metric_values[attr_name] = value
                except Exception as e:
                    logger.error(
                        "阿里云查询指标 %s 最终失败 (instance=%s): %s",
                        attr_name, instance.instance_id, e,
                    )

            # 获取每块磁盘的使用率
            disk_usage: dict[str, float] = {}
            disk_future = disk_futures.get(instance.instance_id)
            if disk_future:
                try:
                    disk_result = disk_future.result()
                    if disk_result is not None:
                        disk_usage = disk_result
                except Exception as e:
                    logger.error(
                        "阿里云查询磁盘使用率最终失败 (instance=%s): %s",
                        instance.instance_id, e,
                    )

            if not metric_values and not disk_usage:
                continue

            result.append(MetricData(
                instance=instance,
                cpu_utilization_percent=metric_values.get("cpu_utilization_percent", 0.0),
                memory_utilization_percent=metric_values.get("memory_utilization_percent", 0.0),
                disk_usage=disk_usage,
                network_in_rate_bytes_per_second=metric_values.get("network_in_rate_bytes_per_second", 0.0),
                network_out_rate_bytes_per_second=metric_values.get("network_out_rate_bytes_per_second", 0.0),
            ))

        return result

    def _query_metric(
        self,
        instance_id: str,
        metric_name: str,
        start_time: int,
        end_time: int,
    ) -> float | None:
        request = cms_models.DescribeMetricListRequest(
            namespace="acs_ecs_dashboard",
            metric_name=metric_name,
            dimensions=f"[{{\"instanceId\":\"{instance_id}\"}}]",
            start_time=str(start_time),
            end_time=str(end_time),
            period="60",
        )
        runtime = util_models.RuntimeOptions()
        try:
            response = self._cms_client.describe_metric_list_with_options(request, runtime)
        except Exception as e:
            logger.warning(
                "阿里云 CMS 指标查询失败 (instance=%s, metric=%s): %s",
                instance_id, metric_name, e,
            )
            raise
        datapoints_str = response.body.datapoints
        if not datapoints_str:
            return None
        try:
            datapoints = json.loads(datapoints_str)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                "阿里云 CMS 指标数据解析失败 (instance=%s, metric=%s): %s",
                instance_id, metric_name, e,
            )
            return None
        if not datapoints:
            return None
        last_point = datapoints[-1]
        return float(last_point.get("Value", last_point.get("Average", 0)))

    def _query_disk_usage(
        self,
        instance_id: str,
        start_time: int,
        end_time: int,
    ) -> dict[str, float]:
        """查询每块磁盘的使用率，返回 {磁盘标识符: 使用率} 的映射

        阿里云 diskusage_utilization 指标包含 device 维度：
        - Linux: device 值为挂载路径（如 /, /data, /home）
        - Windows: device 值为驱动器号（如 C:, D:）
        """
        request = cms_models.DescribeMetricListRequest(
            namespace="acs_ecs_dashboard",
            metric_name="diskusage_utilization",
            dimensions=f"[{{\"instanceId\":\"{instance_id}\"}}]",
            start_time=str(start_time),
            end_time=str(end_time),
            period="60",
        )
        runtime = util_models.RuntimeOptions()
        try:
            response = self._cms_client.describe_metric_list_with_options(request, runtime)
        except Exception as e:
            logger.warning(
                "阿里云 CMS 磁盘指标查询失败 (instance=%s): %s",
                instance_id, e,
            )
            raise
        datapoints_str = response.body.datapoints
        if not datapoints_str:
            return {}
        try:
            datapoints = json.loads(datapoints_str)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                "阿里云 CMS 磁盘指标数据解析失败 (instance=%s): %s",
                instance_id, e,
            )
            return {}
        if not datapoints:
            return {}

        # 按 device 维度分组，取每个磁盘最后一个数据点的值
        disk_usage: dict[str, float] = {}
        for point in datapoints:
            device = point.get("device", "")
            value = float(point.get("Value", point.get("Average", 0)))
            if device:
                # 同一磁盘可能有多条数据点，后出现的覆盖前面的（时间更晚）
                disk_usage[device] = value

        # 若无 device 维度（聚合数据），用 "total" 作为标识符
        if not disk_usage and datapoints:
            last_point = datapoints[-1]
            value = float(last_point.get("Value", last_point.get("Average", 0)))
            disk_usage["total"] = value

        return disk_usage
