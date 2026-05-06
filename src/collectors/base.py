from __future__ import annotations

from abc import ABC, abstractmethod

from prometheus_client.core import GaugeMetricFamily


class MetricCollector(ABC):
    no_cache: bool = False

    @abstractmethod
    def collect(self) -> list[GaugeMetricFamily]:
        ...
