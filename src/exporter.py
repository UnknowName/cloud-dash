from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Response
from pydantic import BaseModel
from prometheus_client import CollectorRegistry
from prometheus_client.core import Metric
from prometheus_client.exposition import CONTENT_TYPE_LATEST, generate_latest

from .cache import MetricsCache, ScheduleStats
from .providers.base import CloudProvider

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: str


class PoolStatsResponse(BaseModel):
    active_threads: int
    queue_length: int
    completed_tasks: int
    failed_tasks: int
    total_submitted: int
    max_workers: int
    current_cycle: int


class ProviderStatusResponse(BaseModel):
    name: str
    type: str
    region: str
    collection_interval_seconds: int
    pool_stats: PoolStatsResponse


class ScheduleStatsResponse(BaseModel):
    collection_interval_seconds: int
    total_cycles: int
    last_cycle_duration_seconds: float
    last_collection_timestamp: float


class StatusResponse(BaseModel):
    status: str
    providers: list[ProviderStatusResponse]
    schedule: ScheduleStatsResponse


class _CachedMetricsCollector:
    """将缓存指标适配为 prometheus_client 的 Collector 接口"""

    def __init__(self, cache: MetricsCache) -> None:
        self._cache = cache

    def describe(self) -> list[Metric]:
        return []

    def collect(self) -> list[Metric]:
        cached = self._cache.get_metrics()
        realtime = self._cache.get_realtime_metrics()
        return cached + realtime


def _create_app(cache: MetricsCache, providers: list[CloudProvider]) -> FastAPI:
    """创建并配置 FastAPI 应用实例"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 启动时开始缓存刷新
        cache.start()
        logger.info("缓存刷新服务已通过 FastAPI lifespan 启动")
        yield
        # 关闭时停止缓存刷新和 Provider 线程池
        cache.stop()
        for provider in providers:
            provider.shutdown()
        logger.info("所有服务已通过 FastAPI lifespan 关闭")

    app = FastAPI(
        title="Cloud Dash",
        description="Prometheus exporter for cloud platform monitoring metrics",
        version="0.1.0",
        lifespan=lifespan,
    )

    registry = CollectorRegistry()
    registry.register(_CachedMetricsCollector(cache))

    @app.get(
        "/metrics",
        summary="Prometheus 指标端点",
        description="返回 Prometheus 格式的云平台监控指标数据，供 Prometheus 抓取",
        response_class=Response,
        tags=["metrics"],
    )
    def get_metrics() -> Response:
        try:
            output = generate_latest(registry)
            return Response(content=output, media_type=CONTENT_TYPE_LATEST)
        except Exception as e:
            logger.error("生成指标数据失败: %s", e)
            return Response(
                content=b"",
                media_type=CONTENT_TYPE_LATEST,
                status_code=500,
            )

    @app.get(
        "/health",
        response_model=HealthResponse,
        summary="健康检查",
        description="返回服务健康状态",
        tags=["health"],
    )
    async def health_check() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/api/v1/status",
        response_model=StatusResponse,
        summary="系统状态",
        description="返回服务运行状态，包括调度统计和各云平台 Provider 的线程池统计信息",
        tags=["status"],
    )
    async def get_status() -> StatusResponse:
        schedule_stats = cache.get_schedule_stats()
        provider_statuses = []
        for provider in providers:
            stats = provider.get_pool_stats()
            provider_statuses.append(
                ProviderStatusResponse(
                    name=provider.name,
                    type=provider.provider_type,
                    region=provider.region,
                    collection_interval_seconds=provider.collection_interval_seconds,
                    pool_stats=PoolStatsResponse(
                        active_threads=stats.active_threads,
                        queue_length=stats.queue_length,
                        completed_tasks=stats.completed_tasks,
                        failed_tasks=stats.failed_tasks,
                        total_submitted=stats.total_submitted,
                        max_workers=stats.max_workers,
                        current_cycle=stats.current_cycle,
                    ),
                )
            )
        return StatusResponse(
            status="ok",
            providers=provider_statuses,
            schedule=ScheduleStatsResponse(
                collection_interval_seconds=schedule_stats.collection_interval_seconds,
                total_cycles=schedule_stats.total_cycles,
                last_cycle_duration_seconds=schedule_stats.last_cycle_duration_seconds,
                last_collection_timestamp=schedule_stats.last_collection_timestamp,
            ),
        )

    return app


class Exporter:
    def __init__(
        self,
        cache: MetricsCache,
        providers: list[CloudProvider],
        port: int = 9100,
    ) -> None:
        self._cache = cache
        self._providers = providers
        self._port = port
        self._app = _create_app(cache, providers)

    @property
    def app(self) -> FastAPI:
        return self._app

    def run(self) -> None:
        import uvicorn

        logger.info("Exporter 服务启动，监听端口: %d", self._port)
        uvicorn.run(self._app, host="0.0.0.0", port=self._port)
