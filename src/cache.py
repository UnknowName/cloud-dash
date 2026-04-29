from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from prometheus_client.core import GaugeMetricFamily

from .collectors.base import MetricCollector
from .config import MIN_COLLECTION_INTERVAL_SECONDS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduleStats:
    """调度统计信息"""
    collection_interval_seconds: int = 0
    total_cycles: int = 0
    last_cycle_duration_seconds: float = 0.0
    last_collection_timestamp: float = 0.0


class MetricsCache:
    def __init__(
        self,
        collectors: list[MetricCollector],
        collection_interval_seconds: int = 300,
    ) -> None:
        # 采集间隔不低于最小限制
        self._collection_interval_seconds = max(
            collection_interval_seconds, MIN_COLLECTION_INTERVAL_SECONDS
        )
        self._collectors = collectors
        self._lock = threading.Lock()
        # 每个采集器独立缓存，key 为采集器类名
        self._collector_cache: dict[str, list[GaugeMetricFamily]] = {}

        # 调度控制：使用 Event + 循环替代 Timer 链，支持动态间隔和精确唤醒
        self._stop_event = threading.Event()
        self._interval_changed = threading.Event()
        self._scheduler_thread: threading.Thread | None = None

        # 调度统计
        self._total_cycles = 0
        self._last_cycle_duration = 0.0
        self._last_collection_timestamp = 0.0

    @property
    def collection_interval_seconds(self) -> int:
        return self._collection_interval_seconds

    def start(self) -> None:
        if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
            logger.warning("缓存刷新服务已在运行中")
            return

        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="metrics-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()
        logger.info(
            "缓存刷新服务已启动，采集间隔: %d秒 (最小间隔: %d秒)",
            self._collection_interval_seconds, MIN_COLLECTION_INTERVAL_SECONDS,
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._interval_changed.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=10.0)
        self._scheduler_thread = None
        logger.info("缓存刷新服务已停止")

    def update_interval(self, new_interval_seconds: int) -> None:
        """动态更新采集间隔，立即生效（当前周期将被中断并重新计时）"""
        clamped = max(new_interval_seconds, MIN_COLLECTION_INTERVAL_SECONDS)
        if clamped == self._collection_interval_seconds:
            return

        old = self._collection_interval_seconds
        self._collection_interval_seconds = clamped
        # 唤醒调度线程使其使用新间隔
        self._interval_changed.set()
        logger.info("采集间隔已动态调整: %d秒 -> %d秒", old, clamped)

    def get_metrics(self) -> list[GaugeMetricFamily]:
        with self._lock:
            result = []
            for metrics in self._collector_cache.values():
                result.extend(metrics)
            return result

    def get_schedule_stats(self) -> ScheduleStats:
        return ScheduleStats(
            collection_interval_seconds=self._collection_interval_seconds,
            total_cycles=self._total_cycles,
            last_cycle_duration_seconds=self._last_cycle_duration,
            last_collection_timestamp=self._last_collection_timestamp,
        )

    def _scheduler_loop(self) -> None:
        # 首次立即执行采集
        self._execute_collection()

        while not self._stop_event.is_set():
            # 等待采集间隔，可被 stop_event 或 interval_changed 提前唤醒
            woke_up = self._interval_changed.wait(
                timeout=self._collection_interval_seconds
            )
            if self._stop_event.is_set():
                break

            if woke_up:
                # 间隔被动态修改，清除标志后立即开始新一轮采集
                self._interval_changed.clear()
                logger.debug("调度周期被间隔变更中断，重新开始采集")

            self._execute_collection()

    def _execute_collection(self) -> None:
        cycle_start = time.monotonic()
        self._last_collection_timestamp = time.time()
        self._total_cycles += 1
        cycle_id = self._total_cycles

        logger.debug("开始第 %d 轮采集", cycle_id)

        for collector in self._collectors:
            collector_name = collector.__class__.__name__
            try:
                collected = collector.collect()
                with self._lock:
                    self._collector_cache[collector_name] = collected
            except Exception as e:
                logger.error("采集器 %s 刷新失败，保留上次缓存: %s", collector_name, e)

        elapsed = time.monotonic() - cycle_start
        self._last_cycle_duration = elapsed

        # 采集耗时超过间隔时发出警告
        if elapsed > self._collection_interval_seconds:
            logger.warning(
                "第 %d 轮采集耗时 %.1f秒，超过采集间隔 %d秒，可能导致采集积压",
                cycle_id, elapsed, self._collection_interval_seconds,
            )
        else:
            logger.debug(
                "第 %d 轮采集完成，耗时 %.2f秒", cycle_id, elapsed,
            )
