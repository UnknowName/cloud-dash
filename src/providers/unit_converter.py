from __future__ import annotations

_STORAGE_UNIT_TO_GB: dict[str, float] = {
    "TB": 1024.0,
    "GB": 1.0,
    "MB": 1.0 / 1024,
    "KB": 1.0 / (1024 * 1024),
}


def normalize_amounts(
    total: float,
    total_unit: str,
    remaining: float,
    remaining_unit: str,
) -> tuple[float, float, str]:
    """将总量和剩余量统一到同一存储单位

    当总量和剩余量单位不同时（如总量1TB、剩余800GB），
    自动换算到较小单位以保证精度，再计算百分比。

    对于非存储单位（如"次"、"小时"等）不做换算，原样返回。

    Args:
        total: 总量数值
        total_unit: 总量单位
        remaining: 剩余量数值
        remaining_unit: 剩余量单位

    Returns:
        (归一化总量, 归一化剩余量, 统一单位)
    """
    t_key = total_unit.strip().upper()
    r_key = remaining_unit.strip().upper()

    if t_key == r_key:
        return total, remaining, total_unit

    t_factor = _STORAGE_UNIT_TO_GB.get(t_key)
    r_factor = _STORAGE_UNIT_TO_GB.get(r_key)

    if t_factor is not None and r_factor is not None:
        if t_factor <= r_factor:
            base_unit = t_key
            base_factor = t_factor
        else:
            base_unit = r_key
            base_factor = r_factor

        normalized_total = total * (t_factor / base_factor)
        normalized_remaining = remaining * (r_factor / base_factor)
        return normalized_total, normalized_remaining, base_unit

    return total, remaining, total_unit
