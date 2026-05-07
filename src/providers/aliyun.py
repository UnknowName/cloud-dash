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

from .base import CloudProvider, InstanceInfo, MetricData, BalanceInfo, ResourcePackageInfo, DEFAULT_COLLECTION_INTERVAL_SECONDS
from .unit_converter import normalize_amounts
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

_BATCH_SIZE = 50


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
        if not instances:
            return []

        end_time = int(time.time() * 1000)
        start_time = end_time - self.collection_interval_seconds * 1000
        cycle_id = self.begin_collection_cycle()

        instance_ids = [ins.instance_id for ins in instances]
        batches = self._build_dimensions(instance_ids)

        # 基础指标（CPU/内存/网络）：每个指标每个批次提交一个查询任务
        metric_futures: dict[tuple[str, int], Future] = {}
        for attr_name, metric_name in ALIYUN_METRIC_NAMES.items():
            priority = METRIC_PRIORITIES.get(attr_name, 5)
            for batch_idx, (_, dims) in enumerate(batches):
                future = self._pool.submit(
                    self._query_metric_batch,
                    metric_name, dims, start_time, end_time,
                    priority=priority, cycle_id=cycle_id,
                )
                metric_futures[(attr_name, batch_idx)] = future

        # 磁盘使用率：每个批次提交一个查询任务
        disk_usage_futures: dict[int, Future] = {}
        for batch_idx, (_, dims) in enumerate(batches):
            priority = METRIC_PRIORITIES.get("disk_usage", 5)
            future = self._pool.submit(
                self._query_metric_with_device_batch,
                "diskusage_utilization", dims, start_time, end_time,
                priority=priority, cycle_id=cycle_id,
            )
            disk_usage_futures[batch_idx] = future

        # 磁盘IO Agent指标：每个指标每个批次提交一个查询任务
        disk_io_agent_futures: dict[tuple[str, int], Future] = {}
        for attr_name, metric_name in ALIYUN_DISK_IO_AGENT_METRICS.items():
            priority = METRIC_PRIORITIES.get("disk_io", 5)
            for batch_idx, (_, dims) in enumerate(batches):
                future = self._pool.submit(
                    self._query_metric_with_device_batch,
                    metric_name, dims, start_time, end_time,
                    priority=priority, cycle_id=cycle_id,
                )
                disk_io_agent_futures[(attr_name, batch_idx)] = future

        # 收集基础指标结果: {instance_id: {attr_name: value}}
        metric_values: dict[str, dict[str, float]] = {}
        for (attr_name, batch_idx), future in metric_futures.items():
            try:
                for iid, value in future.result().items():
                    metric_values.setdefault(iid, {})[attr_name] = value
            except Exception as e:
                logger.error(
                    "阿里云批量查询指标 %s 最终失败 (batch=%d): %s",
                    attr_name, batch_idx, e,
                )

        # 收集磁盘使用率结果: {instance_id: {device: value}}
        disk_usage_values: dict[str, dict[str, float]] = {}
        for batch_idx, future in disk_usage_futures.items():
            try:
                for iid, device_map in future.result().items():
                    disk_usage_values[iid] = device_map
            except Exception as e:
                logger.error(
                    "阿里云批量查询磁盘使用率最终失败 (batch=%d): %s",
                    batch_idx, e,
                )

        # 收集磁盘IO Agent指标结果: {instance_id: {attr_name: {device: value}}}
        disk_io_values: dict[str, dict[str, dict[str, float]]] = {}
        for (attr_name, batch_idx), future in disk_io_agent_futures.items():
            try:
                for iid, device_map in future.result().items():
                    disk_io_values.setdefault(iid, {})
                    disk_io_values[iid][attr_name] = device_map
            except Exception as e:
                logger.error(
                    "阿里云批量查询磁盘IO Agent指标最终失败 (metric=%s, batch=%d): %s",
                    attr_name, batch_idx, e,
                )

        # 磁盘IO回退：对无Agent数据的实例查询基础指标
        instances_without_agent = [
            iid for iid in instance_ids
            if not any(disk_io_values.get(iid, {}).values())
        ]
        if instances_without_agent:
            self._fill_disk_io_fallback(
                instances_without_agent, start_time, end_time, cycle_id, disk_io_values,
            )

        # 按 instance 组装 MetricData
        result = []
        for instance in instances:
            iid = instance.instance_id
            mv = metric_values.get(iid, {})
            du = disk_usage_values.get(iid, {})
            dio = disk_io_values.get(iid, {})

            if not mv and not du and not any(dio.values()):
                continue

            result.append(MetricData(
                instance=instance,
                cpu_utilization_percent=mv.get("cpu_utilization_percent", 0.0),
                memory_utilization_percent=mv.get("memory_utilization_percent", 0.0),
                disk_usage=du,
                disk_read_bps=dio.get("disk_read_bps", {}),
                disk_write_bps=dio.get("disk_write_bps", {}),
                disk_read_iops=dio.get("disk_read_iops", {}),
                disk_write_iops=dio.get("disk_write_iops", {}),
                network_in_rate_bytes_per_second=mv.get("network_in_rate_bytes_per_second", 0.0),
                network_out_rate_bytes_per_second=mv.get("network_out_rate_bytes_per_second", 0.0),
            ))

        return result

    def _fill_disk_io_fallback(
        self,
        instance_ids: list[str],
        start_time: int,
        end_time: int,
        cycle_id: int,
        disk_io_values: dict[str, dict[str, dict[str, float]]],
    ) -> None:
        """对无Agent数据的实例查询基础磁盘IO指标，结果写入 disk_io_values"""
        fallback_batches = self._build_dimensions(instance_ids)
        futures: list[tuple[str, int, Future]] = []
        for attr_name, metric_name in ALIYUN_DISK_IO_BASIC_METRICS.items():
            priority = METRIC_PRIORITIES.get("disk_io", 5)
            for batch_idx, (_, dims) in enumerate(fallback_batches):
                future = self._pool.submit(
                    self._query_metric_batch,
                    metric_name, dims, start_time, end_time,
                    priority=priority, cycle_id=cycle_id,
                )
                futures.append((attr_name, batch_idx, future))

        for attr_name, batch_idx, future in futures:
            try:
                for iid, value in future.result().items():
                    disk_io_values.setdefault(iid, {})
                    disk_io_values[iid].setdefault(attr_name, {})["total"] = value
            except Exception as e:
                logger.error(
                    "阿里云批量查询磁盘IO基础指标最终失败 (metric=%s, batch=%d): %s",
                    attr_name, batch_idx, e,
                )

    @staticmethod
    def _build_dimensions(
        instance_ids: list[str],
        batch_size: int = _BATCH_SIZE,
    ) -> list[tuple[list[str], str]]:
        """将实例ID列表分批，返回 [(批次实例ID列表, Dimensions JSON), ...]

        阿里云 DescribeMetricList/DescribeMetricLast 的 Dimensions 参数
        单次最多支持 50 个实例，超出需分批查询。
        """
        batches: list[tuple[list[str], str]] = []
        for i in range(0, len(instance_ids), batch_size):
            batch_ids = instance_ids[i:i + batch_size]
            dims = json.dumps([{"instanceId": iid} for iid in batch_ids])
            batches.append((batch_ids, dims))
        return batches

    def _query_cms_last(
        self,
        metric_name: str,
        dimensions: str,
        start_time: int,
        end_time: int,
    ) -> list[dict]:
        """调用 DescribeMetricLast API，返回解析后的 datapoints 列表

        使用 DescribeMetricLast 替代 DescribeMetricList：
        - 仅返回最新数据点，减少数据传输量
        - QPS 限制更高（30 vs 20）
        - 分钟级数据自动回溯 2 小时，覆盖采集间隔
        """
        request = cms_models.DescribeMetricLastRequest(
            namespace="acs_ecs_dashboard",
            metric_name=metric_name,
            dimensions=dimensions,
            start_time=str(start_time),
            end_time=str(end_time),
            period="60",
        )
        runtime = util_models.RuntimeOptions()
        try:
            response = self._cms_client.describe_metric_last_with_options(request, runtime)
        except Exception as e:
            logger.warning(
                "阿里云 CMS 指标查询失败 (metric=%s): %s",
                metric_name, e,
            )
            raise
        datapoints_str = response.body.datapoints
        if not datapoints_str:
            return []
        try:
            return json.loads(datapoints_str)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                "阿里云 CMS 指标数据解析失败 (metric=%s): %s",
                metric_name, e,
            )
            return []

    def _query_metric_batch(
        self,
        metric_name: str,
        dimensions: str,
        start_time: int,
        end_time: int,
    ) -> dict[str, float]:
        """批量查询单个指标（无device维度），返回 {instance_id: value}"""
        datapoints = self._query_cms_last(metric_name, dimensions, start_time, end_time)
        result: dict[str, float] = {}
        for point in datapoints:
            instance_id = point.get("instanceId", "")
            if instance_id:
                value = float(point.get("Value", point.get("Average", 0)))
                result[instance_id] = value
        return result

    def _query_metric_with_device_batch(
        self,
        metric_name: str,
        dimensions: str,
        start_time: int,
        end_time: int,
    ) -> dict[str, dict[str, float]]:
        """批量查询含device维度的指标，返回 {instance_id: {device: value}}

        适用于磁盘使用率和磁盘IO Agent指标，这些指标按 device 维度分组：
        - Linux: device 为挂载路径（如 /, /data）
        - Windows: device 为驱动器号（如 C:, D:）
        - 无 device 维度时回退为 "total" 标识
        """
        datapoints = self._query_cms_last(metric_name, dimensions, start_time, end_time)
        result: dict[str, dict[str, float]] = {}
        for point in datapoints:
            instance_id = point.get("instanceId", "")
            device = point.get("device", "")
            value = float(point.get("Value", point.get("Average", 0)))
            if instance_id:
                key = device if device else "total"
                result.setdefault(instance_id, {})[key] = value
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

    def get_resource_packages(self) -> list[ResourcePackageInfo]:
        page_num = 1
        page_size = 300
        all_packages: list[ResourcePackageInfo] = []
        runtime = util_models.RuntimeOptions()

        try:
            while True:
                request = bss_models.QueryResourcePackageInstancesRequest(
                    page_num=page_num,
                    page_size=page_size,
                )
                response = self._bss_client.query_resource_package_instances_with_options(request, runtime)
                body = response.body
                if not body.success:
                    logger.warning(
                        "阿里云查询资源包返回失败: code=%s, message=%s",
                        body.code, body.message,
                    )
                    break

                data = body.data
                if not data or not data.instances or not data.instances.instance:
                    break

                for ins in data.instances.instance:
                    if ins.status != "Available":
                        continue

                    total = self._parse_amount(ins.total_amount)
                    remaining = self._parse_amount(ins.remaining_amount)
                    total_unit = ins.total_amount_unit or ""
                    remaining_unit = ins.remaining_amount_unit or ""

                    # 单位不同时统一换算到同一单位再计算百分比
                    total, remaining, unit = normalize_amounts(
                        total, total_unit, remaining, remaining_unit,
                    )

                    if total > 0 and remaining <= 0:
                        continue

                    if total > 0:
                        remaining_percent = min(remaining / total * 100, 100.0)
                    else:
                        remaining_percent = 100.0

                    all_packages.append(ResourcePackageInfo(
                        instance_id=ins.instance_id or "",
                        name=ins.remark or "",
                        region=ins.region or "",
                        status=ins.status or "",
                        total_amount=total,
                        remaining_amount=remaining,
                        remaining_percent=remaining_percent,
                        unit=unit,
                        effective_time=ins.effective_time or "",
                        expiry_time=ins.expiry_time or "",
                        commodity_code=ins.commodity_code or "",
                    ))

                total_count = int(data.total_count or 0)
                if page_num * page_size >= total_count:
                    break
                page_num += 1

            logger.info("阿里云获取资源包列表完成，共 %d 个", len(all_packages))
            return all_packages
        except Exception as e:
            logger.error("阿里云查询资源包失败: %s", e)
            return []
