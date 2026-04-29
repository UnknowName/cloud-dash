from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..pool import PriorityThreadPool, PoolStats, DEFAULT_MAX_WORKERS, DEFAULT_MAX_RETRIES, DEFAULT_RETRY_DELAY
from ..config import MIN_COLLECTION_INTERVAL_SECONDS

if TYPE_CHECKING:
    from ..instance_cache import InstanceCache

DEFAULT_COLLECTION_INTERVAL_SECONDS = 300

logger = logging.getLogger(__name__)


@dataclass
class InstanceInfo:
    instance_id: str = ""
    instance_name: str = ""
    region: str = ""


@dataclass
class MetricData:
    instance: InstanceInfo
    cpu_utilization_percent: float = 0.0
    memory_utilization_percent: float = 0.0
    disk_usage: dict[str, float] = field(default_factory=dict)
    network_in_rate_bytes_per_second: float = 0.0
    network_out_rate_bytes_per_second: float = 0.0


@dataclass
class MetricResult:
    provider_name: str = ""
    provider_type: str = ""
    metrics: list[MetricData] = field(default_factory=list)


class CloudProvider(ABC):
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
        self.name = name
        self.region = region
        self.credentials = credentials
        self.include_name = include_name
        self._instance_cache = instance_cache
        # 采集间隔（秒），强制不低于最小限制
        self.collection_interval_seconds = max(
            collection_interval_seconds, MIN_COLLECTION_INTERVAL_SECONDS
        )
        self._pool = PriorityThreadPool(
            max_workers=max_workers,
            max_retries=max_retries,
            retry_delay=retry_delay,
            name_prefix=name,
        )
        # 记录初始工作线程数，用于动态调整的基准
        self._base_max_workers = max_workers

    def shutdown(self) -> None:
        stats = self._pool.get_stats()
        logger.info(
            "Provider [%s] 线程池状态: 活跃=%d, 队列=%d, 完成=%d, 失败=%d, 周期=%d",
            self.name, stats.active_threads, stats.queue_length,
            stats.completed_tasks, stats.failed_tasks, stats.current_cycle,
        )
        self._pool.shutdown()

    def get_pool_stats(self) -> PoolStats:
        return self._pool.get_stats()

    def begin_collection_cycle(self) -> int:
        """开始新一轮采集周期，返回周期ID"""
        cycle_id = self._pool.begin_cycle()
        # 根据当前实例数和采集间隔动态调整线程池大小
        self._adjust_pool_for_cycle()
        return cycle_id

    def wait_collection_cycle(self, cycle_id: int, timeout: float | None = None) -> bool:
        """等待指定采集周期的所有任务完成"""
        if timeout is None:
            timeout = self.collection_interval_seconds * 0.8
        return self._pool.wait_cycle(cycle_id, timeout=timeout)

    def _adjust_pool_for_cycle(self) -> None:
        """根据采集间隔和任务量动态调整线程池大小

        策略：采集间隔越短，需要更多并发来确保在间隔内完成采集；
        间隔越长，可以适当减少并发以节省资源。
        """
        stats = self._pool.get_stats()
        # 用队列长度 + 活跃线程数估算下一轮任务量
        estimated_tasks = max(stats.queue_length + stats.active_threads, self._base_max_workers)
        suggested = self._pool.suggest_workers(
            task_count=estimated_tasks,
            collection_interval_seconds=self.collection_interval_seconds,
        )
        if suggested != stats.max_workers:
            try:
                self._pool.adjust_workers(suggested)
            except ValueError:
                pass

    def update_collection_interval(self, new_interval_seconds: int) -> None:
        """动态更新采集间隔"""
        clamped = max(new_interval_seconds, MIN_COLLECTION_INTERVAL_SECONDS)
        if clamped == self.collection_interval_seconds:
            return
        old = self.collection_interval_seconds
        self.collection_interval_seconds = clamped
        logger.info("Provider [%s] 采集间隔已更新: %d秒 -> %d秒", self.name, old, clamped)

    @abstractmethod
    def list_instances(self) -> list[InstanceInfo]:
        ...

    def get_instances(self) -> list[InstanceInfo]:
        if self._instance_cache:
            cached = self._instance_cache.load(self.name)
            if cached is not None:
                return cached

        instances = self.list_instances()

        if self._instance_cache and instances:
            self._instance_cache.save(self.name, instances)

        return instances

    def _filter_by_name(self, instances: list[InstanceInfo]) -> list[InstanceInfo]:
        if not self.include_name:
            return instances
        return [ins for ins in instances if self.include_name in ins.instance_name]

    @abstractmethod
    def get_metrics(self, instances: list[InstanceInfo]) -> list[MetricData]:
        ...

    def collect(self) -> MetricResult:
        instances = self.get_instances()
        if not instances:
            return MetricResult(provider_name=self.name, provider_type=self.provider_type)
        metrics = self.get_metrics(instances)
        return MetricResult(
            provider_name=self.name,
            provider_type=self.provider_type,
            metrics=metrics,
        )

    @property
    def provider_type(self) -> str:
        return self.__class__.__name__.replace("Provider", "").lower()
