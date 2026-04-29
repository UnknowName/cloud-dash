from __future__ import annotations

import logging
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone

from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkecs.v2 import EcsClient as HwEcsClient
from huaweicloudsdkecs.v2 import ListServersDetailsRequest
from huaweicloudsdkces.v1 import CesClient as HwCesV1Client
from huaweicloudsdkces.v1 import ShowMetricDataRequest
from huaweicloudsdkces.v2 import CesClient as HwCesV2Client
from huaweicloudsdkces.v2 import ListAgentDimensionInfoRequest

from .base import CloudProvider, InstanceInfo, MetricData, DEFAULT_COLLECTION_INTERVAL_SECONDS
from ..pool import DEFAULT_MAX_WORKERS, DEFAULT_MAX_RETRIES, DEFAULT_RETRY_DELAY
from ..instance_cache import InstanceCache

logger = logging.getLogger(__name__)

HUAWEI_METRIC_DIMS = {
    "cpu_utilization_percent": ("cpu_util", "CPU使用率"),
    "memory_utilization_percent": ("mem_util", "内存使用率"),
    "network_in_rate_bytes_per_second": ("network_incoming_bytes_aggregate_rate", "网络流入速率"),
    "network_out_rate_bytes_per_second": ("network_outgoing_bytes_aggregate_rate", "网络流出速率"),
}

METRIC_PRIORITIES = {
    "cpu_utilization_percent": 1,
    "memory_utilization_percent": 2,
    "disk_usage": 3,
    "network_in_rate_bytes_per_second": 4,
    "network_out_rate_bytes_per_second": 4,
}


class HuaweiProvider(CloudProvider):
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
        self._credentials = BasicCredentials(
            ak=credentials.get("access_key_id", ""),
            sk=credentials.get("access_key_secret", ""),
            project_id=credentials.get("project_id", ""),
        )
        self._ecs_client = self._create_ecs_client()
        self._ces_v1_client = self._create_ces_v1_client()
        self._ces_v2_client = self._create_ces_v2_client()

    def _create_ecs_client(self) -> HwEcsClient:
        from huaweicloudsdkcore.http.http_config import HttpConfig
        from huaweicloudsdkecs.v2.region.ecs_region import EcsRegion

        config = HttpConfig.get_default_config()
        config.ignore_ssl_verification = True
        return (HwEcsClient.new_builder()
                .with_credentials(self._credentials)
                .with_region(EcsRegion.value_of(self.region))
                .with_http_config(config)
                .build())

    def _create_ces_v1_client(self) -> HwCesV1Client:
        from huaweicloudsdkcore.http.http_config import HttpConfig
        from huaweicloudsdkces.v1.region.ces_region import CesRegion

        config = HttpConfig.get_default_config()
        config.ignore_ssl_verification = True
        return (HwCesV1Client.new_builder()
                .with_credentials(self._credentials)
                .with_region(CesRegion.value_of(self.region))
                .with_http_config(config)
                .build())

    def _create_ces_v2_client(self) -> HwCesV2Client:
        from huaweicloudsdkcore.http.http_config import HttpConfig
        from huaweicloudsdkces.v2.region.ces_region import CesRegion

        config = HttpConfig.get_default_config()
        config.ignore_ssl_verification = True
        return (HwCesV2Client.new_builder()
                .with_credentials(self._credentials)
                .with_region(CesRegion.value_of(self.region))
                .with_http_config(config)
                .build())

    def list_instances(self) -> list[InstanceInfo]:
        limit = 100
        offset = 1
        all_instances: list[InstanceInfo] = []

        try:
            while True:
                request = ListServersDetailsRequest(
                    limit=limit,
                    offset=offset,
                    name=self.include_name or None,
                )
                response = self._ecs_client.list_servers_details(request)
                page_servers = response.servers
                for server in page_servers:
                    all_instances.append(InstanceInfo(
                        instance_id=server.id,
                        instance_name=server.name,
                        region=self.region,
                    ))
                # 当前页数据量小于 limit，说明已是最后一页
                if len(page_servers) < limit:
                    break
                offset += limit

            logger.info("华为云获取实例列表完成，共 %d 个实例", len(all_instances))
            return all_instances
        except Exception as e:
            logger.error("华为云获取实例列表失败: %s", e)
            return []

    def get_metrics(self, instances: list[InstanceInfo]) -> list[MetricData]:
        now = datetime.now(timezone.utc)
        from_time = (now - timedelta(seconds=self.collection_interval_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        to_time = now.strftime("%Y-%m-%d %H:%M:%S")

        cycle_id = self.begin_collection_cycle()

        # 非磁盘指标：每个实例每个指标提交一个查询任务
        futures: dict[tuple[str, str], Future] = {}
        for instance in instances:
            for attr_name, (metric_name, _) in HUAWEI_METRIC_DIMS.items():
                priority = METRIC_PRIORITIES.get(attr_name, 5)
                future = self._pool.submit(
                    self._query_metric,
                    instance.instance_id, metric_name, from_time, to_time,
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
                instance.instance_id, from_time, to_time,
                priority=priority,
                cycle_id=cycle_id,
            )
            disk_futures[instance.instance_id] = future

        result = []
        for instance in instances:
            metric_values: dict[str, float] = {}
            for attr_name in HUAWEI_METRIC_DIMS:
                future = futures.get((instance.instance_id, attr_name))
                if not future:
                    continue
                try:
                    value = future.result()
                    if value is not None:
                        metric_values[attr_name] = value
                except Exception as e:
                    logger.error(
                        "华为云查询指标 %s 最终失败 (instance=%s): %s",
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
                        "华为云查询磁盘使用率最终失败 (instance=%s): %s",
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
        from_time: str,
        to_time: str,
    ) -> float | None:
        request = ShowMetricDataRequest(
            metric_name=metric_name,
            namespace="SYS.ECS",
            dim_0=f"instance_id,{instance_id}",
            _from=from_time,
            to=to_time,
            period=1,
            filter="average",
        )
        response = self._ces_v1_client.show_metric_data(request)
        datapoints = response.datapoints
        if not datapoints:
            return None
        return float(datapoints[-1].average)

    def _query_disk_usage(
        self,
        instance_id: str,
        from_time: str,
        to_time: str,
    ) -> dict[str, float]:
        """查询每块磁盘的使用率，返回 {磁盘标识符: 使用率} 的映射

        华为云 disk_util_inband 指标包含 mount_point 维度：
        - Linux: mount_point 的 origin_value 为挂载路径（如 /, /data, /home）
        - Windows: mount_point 的 origin_value 为驱动器号（如 C:, D:）
        """
        mount_points = self._discover_mount_points(instance_id)
        if not mount_points:
            # 无挂载点信息时回退到实例级聚合查询
            return self._query_aggregate_disk_usage(instance_id, from_time, to_time)

        disk_usage: dict[str, float] = {}
        for mount_point_hash, mount_point_path in mount_points:
            try:
                request = ShowMetricDataRequest(
                    metric_name="disk_util_inband",
                    namespace="SYS.ECS",
                    dim_0=f"instance_id,{instance_id}",
                    dim_1=f"mount_point,{mount_point_hash}",
                    _from=from_time,
                    to=to_time,
                    period=1,
                    filter="average",
                )
                response = self._ces_v1_client.show_metric_data(request)
                datapoints = response.datapoints
                if datapoints:
                    disk_usage[mount_point_path] = float(datapoints[-1].average)
            except Exception as e:
                logger.warning(
                    "华为云查询挂载点 %s 磁盘使用率失败 (instance=%s): %s",
                    mount_point_path, instance_id, e,
                )

        return disk_usage

    def _discover_mount_points(self, instance_id: str) -> list[tuple[str, str]]:
        """发现实例的挂载点维度信息，返回 [(哈希值, 实际路径), ...] 的列表

        使用 CES v2 的 ListAgentDimensionInfo API 获取挂载点维度：
        - value: 32位哈希字符串，用于查询指标时的 dim_1 参数
        - origin_value: 实际挂载路径（Linux: /, /data 等; Windows: C:, D: 等）
        """
        request = ListAgentDimensionInfoRequest(
            instance_id=instance_id,
            dim_name="mount_point",
            limit=1000,
        )
        try:
            response = self._ces_v2_client.list_agent_dimension_info(request)
        except Exception as e:
            logger.warning(
                "华为云发现挂载点失败 (instance=%s): %s",
                instance_id, e,
            )
            return []

        if not response.dimensions:
            return []

        return [
            (dim.value, dim.origin_value)
            for dim in response.dimensions
            if dim.value and dim.origin_value
        ]

    def _query_aggregate_disk_usage(
        self,
        instance_id: str,
        from_time: str,
        to_time: str,
    ) -> dict[str, float]:
        """回退方案：查询实例级聚合磁盘使用率，用 'total' 作为标识符"""
        try:
            request = ShowMetricDataRequest(
                metric_name="disk_util_inband",
                namespace="SYS.ECS",
                dim_0=f"instance_id,{instance_id}",
                _from=from_time,
                to=to_time,
                period=1,
                filter="average",
            )
            response = self._ces_v1_client.show_metric_data(request)
            datapoints = response.datapoints
            if datapoints:
                return {"total": float(datapoints[-1].average)}
        except Exception as e:
            logger.warning(
                "华为云查询聚合磁盘使用率失败 (instance=%s): %s",
                instance_id, e,
            )
        return {}
