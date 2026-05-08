from __future__ import annotations

import math


def safe_float(value: object) -> float:
    """将指标值安全转换为有效浮点数，确保符合 Prometheus 规范

    处理 None、非数值、NaN、Inf 等异常情况，统一返回 0.0
    """
    if value is None:
        return 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isinf(f) or math.isnan(f):
        return 0.0
    return f
