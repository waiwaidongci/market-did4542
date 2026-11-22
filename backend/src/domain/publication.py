"""对外发布的按日合计与互补披露（反推）核查。

一次发布申请只提供一个「日合计」：把所选市场当天的平均等候分钟相加。
公开的若干合计都来自同一批门岗底稿，都是市场集合上的求和；任意几条合计
相减或线性组合，可能解出单个市场的数值。因此每新增一条合计，都要把已发布
的合计（含已撤下的，它们曾经公开过）一并代入线性方程组，确认任何一家市场
当天的数值都无法被唯一解出，才允许发布。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction

_SPLIT = re.compile(r'[,，、;；\n]+')


def parse_day(value):
    """接受 date 或 YYYY-MM-DD 文本，返回 date。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError("观测日期须使用有效的 YYYY-MM-DD 日期。") from None


def normalize_markets(values):
    """去除首尾空白、校验长度与重复，返回排序后的市场列表。"""
    if isinstance(values, str):
        values = [part for part in _SPLIT.split(values) if part.strip()]
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("请至少选择一个市场。")
    result, seen = [], set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("市场名称须为文本。")
        market = raw.strip()
        if not market or len(market) > 300:
            raise ValueError("市场名称须为 1–300 个字符。")
        if market in seen:
            raise ValueError(f"市场「{market}」重复，请只选择一次。")
        seen.add(market)
        result.append(market)
    if len(result) > 100:
        raise ValueError("一次合计最多选择 100 家市场。")
    return sorted(result)


def _minutes(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("平均等候分钟须为数值。")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise ValueError("平均等候分钟须为数值。") from None


def aggregate(rows, day, markets):
    """按市场日底稿求所选市场当天的平均等候分钟合计。"""
    target = parse_day(day).isoformat()
    wanted = set(markets)
    observed = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market_id", "")).strip()
        if market not in wanted or str(row.get("date", ""))[:10] != target:
            continue
        if market in observed:
            raise ValueError(f"市场「{market}」在 {target} 存在重复记录，无法合计。")
        observed[market] = _minutes(row.get("wait_minutes"))
    missing = [market for market in markets if market not in observed]
    if missing:
        raise ValueError(
            f"市场「{'、'.join(missing)}」在 {target} 没有门岗记录，无法合计。"
        )
    total = sum((observed[market] for market in markets), Decimal(0))
    return float(round(total, 2))


def fingerprint(checksum, day, markets):
    """同一批底稿（同一校验指纹）、同一天、同一组市场视为同一条发布申请。"""
    payload = json.dumps(
        [str(checksum), parse_day(day).isoformat(), list(markets)],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _basis(vectors):
    """把一组 0/1 行向量化为行阶梯基，返回主元位置到归一化行。"""
    basis = {}
    for vector in vectors:
        vector = list(vector)
        for pivot in sorted(basis):
            if vector[pivot]:
                factor = vector[pivot]
                vector = [left - factor * right for left, right in zip(vector, basis[pivot])]
        pivot = next((index for index, value in enumerate(vector) if value), None)
        if pivot is not None:
            factor = vector[pivot]
            basis[pivot] = [value / factor for value in vector]
    return basis


def _indicator(subset, position):
    return [Fraction(1 if market in subset else 0) for market in position]


def derivable_market(subsets):
    """返回可被这组合计唯一解出的第一家市场；没有则返回 None。

    第 m 家市场可解出，当且仅当单位向量 e_m 落在集合指示向量张成的行空间里。
    例如 {A,B} 与 {A,B,C} 合起来可解出 C，{A,B}、{B,C}、{A,C} 合起来可解出三家。
    """
    rows = [set(subset) for subset in subsets if subset]
    universe = sorted({market for subset in rows for market in subset})
    if not universe:
        return None
    basis = _basis(_indicator(subset, universe) for subset in rows)
    for index, market in enumerate(universe):
        vector = [Fraction(1 if position == index else 0) for position in range(len(universe))]
        for pivot in sorted(basis):
            if vector[pivot]:
                factor = vector[pivot]
                vector = [left - factor * right for left, right in zip(vector, basis[pivot])]
        if not any(vector):
            return market
    return None


def conflict(prior_subsets, new_subset):
    """把新合计并入既有一组合计后，检查是否会暴露任一市场的当日数值。"""
    return derivable_market([*prior_subsets, set(new_subset)])
