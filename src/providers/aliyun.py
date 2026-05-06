from __future__ import annotations

import json
import logging
import time
from concurrent.futures import Future

from alibabacloud_bssopenapi20171214.client import Client as BssClient
from alibabacloud_bssopenapi20171214 import models as bss_models
from alibabacloud_cms20190101 import models as cms_models
from alibabacloud_cms20190101.client import Client as CmsClient
from alibabacloud_ecs20140526 import models as ecs_models
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

from .base import CloudProvider, InstanceInfo, MetricData, BalanceInfo, DEFAULT_COLLECTION_INTERVAL_SECONDS
from ..pool import DEFAULT_MAX_WORKERS, DEFAULT_MAX_RETRIES, DEFAULT_RETRY_DELAY
from ..instance_cache import InstanceCache

logger = logging.getLogger(__name__)

ALIYUN_METRIC_NAMES = {
    "cpu_utilization_percent": "CPUUtilization",
    "memory_utilization_percent": "memory_usedutilization",
    "network_in_rate_bytes_per_second": "VPC_PublicIP_InternetInRate",
    "network_out_rate_bytes_per_second": "VPC_PublicIP_InternetOutRate",
}

ALIYUN_DISK_IO_AGENT_METRICS = {
    "disk_read_bps": "disk_readbytes",
    "disk_write_bps": "disk_writebytes",
    "disk_read_iops": "disk_readiops",
    "disk_write_iops": "disk_writeiops",
}

ALIYUN_DISK_IO_BASIC_METRICS = {
    "disk_read_bps": "DiskReadBPS",
    "disk_write_bps": "DiskWriteBPS",
    "disk_read_iops": "DiskReadIOPS",
    "disk_write_iops": "DiskWriteIOPS",
}

METRIC_PRIORITIES = {
    "cpu_utilization_percent": 1,
    "memory_utilization_percent": 2,
    "disk_usage": 3,
    "disk_io": 3,
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
        include_name: list[str] | None = None,
        instance_cache: InstanceCache | None = None,
    ) -> None:
        super().__init__(
            name, region, credentials, max_workers, max_retries,
            retry_delay, collection_interval_seconds, include_name,
            instance_cache=instance_cache,
        )
        self._ecs_client = self._create_ecs_client()
        self._cms_client = self._create_cms_client()
        self._bss_client = self._create_bss_client()

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

    def _create_bss_client(self) -> BssClient:
        # BSS 为全局服务，endpoint 固定为 business.aliyuncs.com
        # 补充 region_id 以确保 SDK 内部配置校验通过
        config = open_api_models.Config(
            access_key_id=self.credentials.get("access_key_id", ""),
            access_key_secret=self.credentials.get("access_key_secret", ""),
            region_id=self.region,
            endpoint="business.aliyuncs.com",
        )
        return BssClient(config)

    def list_instances(self) -> list[InstanceInfo]:
        page_size = 100
        page_number = 1
        all_instances: list[InstanceInfo] = []
        runtime = util_models.RuntimeOptions()

        # 单关键词时使用 API 侧过滤（高效），多关键词时获取全部后本地过滤
        if len(self.include_name) == 1:
            instance_name_filter = f"*{self.include_name[0]}*"
        else:
            instance_name_filter = None

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
                if len(page_instances) < page_size:
                    break
                page_number += 1

            # 多关键词时在本地进行 OR 过滤
            if len(self.include_name) > 1:
                all_instances = self._filter_by_name(all_instances)

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

        # 磁盘IO指标：每个实例提交一个查询任务，返回每块磁盘的IO数据
        disk_io_futures: dict[str, Future] = {}
        for instance in instances:
            priority = METRIC_PRIORITIES.get("disk_io", 5)
            future = self._pool.submit(
                self._query_disk_io,
                instance.instance_id, start_time, end_time,
                priority=priority,
                cycle_id=cycle_id,
            )
            disk_io_futures[instance.instance_id] = future

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

            # 获取每块磁盘的IO数据
            disk_io_data: dict[str, dict[str, float]] = {
                "disk_read_bps": {},
                "disk_write_bps": {},
                "disk_read_iops": {},
                "disk_write_iops": {},
            }
            disk_io_future = disk_io_futures.get(instance.instance_id)
            if disk_io_future:
                try:
                    disk_io_result = disk_io_future.result()
                    if disk_io_result is not None:
                        disk_io_data = disk_io_result
                except Exception as e:
                    logger.error(
                        "阿里云查询磁盘IO最终失败 (instance=%s): %s",
                        instance.instance_id, e,
                    )

            if not metric_values and not disk_usage and not any(disk_io_data.values()):
                continue

            result.append(MetricData(
                instance=instance,
                cpu_utilization_percent=metric_values.get("cpu_utilization_percent", 0.0),
                memory_utilization_percent=metric_values.get("memory_utilization_percent", 0.0),
                disk_usage=disk_usage,
                disk_read_bps=disk_io_data.get("disk_read_bps", {}),
                disk_write_bps=disk_io_data.get("disk_write_bps", {}),
                disk_read_iops=disk_io_data.get("disk_read_iops", {}),
                disk_write_iops=disk_io_data.get("disk_write_iops", {}),
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

    def _query_disk_io(
        self,
        instance_id: str,
        start_time: int,
        end_time: int,
    ) -> dict[str, dict[str, float]]:
        """查询每块磁盘的IO数据，返回 {指标名: {磁盘标识符: 值}} 的映射

        优先查询 Agent 指标（含 device 维度），无数据时回退到基础指标（实例级聚合）
        """
        result: dict[str, dict[str, float]] = {
            "disk_read_bps": {},
            "disk_write_bps": {},
            "disk_read_iops": {},
            "disk_write_iops": {},
        }

        # 第一轮：尝试 Agent 指标（含 device 维度）
        for attr_name, metric_name in ALIYUN_DISK_IO_AGENT_METRICS.items():
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
                    "阿里云 CMS 磁盘IO Agent指标查询失败 (instance=%s, metric=%s): %s",
                    instance_id, metric_name, e,
                )
                raise
            datapoints_str = response.body.datapoints
            if not datapoints_str:
                continue
            try:
                datapoints = json.loads(datapoints_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if not datapoints:
                continue
            for point in datapoints:
                device = point.get("device", "")
                value = float(point.get("Value", point.get("Average", 0)))
                if device:
                    result[attr_name][device] = value

        # 若 Agent 指标获取到了按磁盘的数据，直接返回
        if any(result.values()):
            return result

        # 第二轮：回退到基础指标（实例级聚合，无 device 维度）
        for attr_name, metric_name in ALIYUN_DISK_IO_BASIC_METRICS.items():
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
                    "阿里云 CMS 磁盘IO基础指标查询失败 (instance=%s, metric=%s): %s",
                    instance_id, metric_name, e,
                )
                continue
            datapoints_str = response.body.datapoints
            if not datapoints_str:
                continue
            try:
                datapoints = json.loads(datapoints_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if not datapoints:
                continue
            last_point = datapoints[-1]
            value = float(last_point.get("Value", last_point.get("Average", 0)))
            result[attr_name]["total"] = value

        return result

    def get_balance(self) -> BalanceInfo | None:
        runtime = util_models.RuntimeOptions()
        try:
            response = self._bss_client.query_account_balance_with_options(runtime)
        except Exception as e:
            logger.error("阿里云查询账户余额失败: %s", e)
            return None

        # 校验 API 响应状态，避免错误响应中 data 为空对象导致余额显示为 0
        body = response.body
        if not body.success:
            logger.warning(
                "阿里云查询账户余额返回失败: code=%s, message=%s, request_id=%s",
                body.code, body.message, body.request_id,
            )
            return None

        data = body.data
        if not data:
            return None
        return BalanceInfo(
            available_amount=self._parse_amount(data.available_amount),
            available_cash_amount=self._parse_amount(data.available_cash_amount),
            credit_amount=self._parse_amount(data.credit_amount),
            currency=data.currency or "CNY",
        )

    @staticmethod
    def _parse_amount(value: str | None) -> float:
        # 阿里云 BSS 返回的金额为字符串类型，可能包含千位分隔符（如 "13,296.12"）
        if not value:
            return 0.0
        try:
            return float(value.replace(",", ""))
        except (TypeError, ValueError, AttributeError):
            return 0.0
