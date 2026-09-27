"""最大需量结算：按对齐需量窗的最高平均功率计费（纯计算，不触碰数据库）。

输入沿用 ``/quote`` 的用量区间格式（``start`` / ``end`` / ``timezone`` /
``energy``），另给出用于窗口对齐的 IANA 时区、需量窗分钟数
（15 / 30 / 60）与正单价（每 能量单位·小时 的价格，如 USD/kW）。

窗口对齐
--------
每个本地日以其**本地午夜**为锚点，沿 **UTC 时间线**按固定分钟步进切窗，
到下一个本地午夜为止（偏移变化使日长不是窗长的整数倍时，尾窗截断到下一
个本地午夜）。因此：

* 春季跳时日只有 23 个小时窗——不存在的本地边界（如 02:00）自然跳过；
* 秋季回拨日有 25 个小时窗——重复的本地边界（如 01:00 出现两次）沿 UTC
  时间线分别生成、分别计算。

参评与分摊
----------
只有被输入区间**连续完整覆盖**的窗口才参评（多条首尾相接的输入拼成的
连续覆盖也算）。跨窗区间按**实际时长**（UTC 微秒）比例分摊能量；每条
输入的分摊在六位小数处舍入、残差并入该输入的最后一个重叠窗口，因此
逐输入分摊严格守恒（各部分之和 == 该输入的能量）。

选窗与计费
----------
参评窗口按 ``平均功率 = 窗口能量 ÷ 窗口时长（小时）`` 比较——用精确
交叉相乘比较，不做舍入；取最高者，**同值取 UTC 起点最早**的窗口。获选
窗口的平均功率按六位小数（ROUND_HALF_UP）作为计费需量 ``demand``，
费用 ``fee = demand × unit_price`` 四舍五入到分（ROUND_HALF_UP）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .pricing import (
    Q2,
    Q6,
    ZERO,
    PricingError,
    get_zone,
    parse_input_intervals,
)

WINDOW_MINUTES = (15, 30, 60)

REASON_NO_COVERAGE = "no_coverage"
REASON_PARTIAL_COVERAGE = "partial_coverage"

_MICROS_PER_HOUR = 3_600_000_000


def to_utc_z(dt: datetime) -> str:
    """Format an aware UTC datetime with a trailing ``Z``."""
    return dt.isoformat().replace("+00:00", "Z")


def _micros(delta: timedelta) -> int:
    """Exact length of a timedelta in whole microseconds."""
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _distribute(total: Decimal, weights: list[int]) -> list[Decimal]:
    """Split ``total`` proportionally to ``weights`` at 6 dp; the rounding
    residual lands on the final part so the parts sum to ``total`` exactly."""
    whole = sum(weights)
    if whole <= 0:  # zero-length window: nothing to prorate, keep total on last
        return [ZERO] * (len(weights) - 1) + [total]
    shares = [(total * w / whole).quantize(Q6) for w in weights[:-1]]
    shares.append(total - sum(shares, ZERO))
    return shares


@dataclass
class Window:
    """一个对齐需量窗及其覆盖/分摊状态。"""

    index: int
    start: datetime              # UTC，半开区间 [start, end)
    end: datetime                # UTC
    covered_micros: int = 0      # 被输入区间覆盖的时长（微秒，输入互不重叠故可累加）
    energy: Decimal = ZERO       # 分摊进窗口的能量（各输入六位小数分摊之和）

    @property
    def duration_micros(self) -> int:
        return _micros(self.end - self.start)

    @property
    def eligible(self) -> bool:
        """只有被输入连续完整覆盖的窗口才参评。"""
        return self.covered_micros == self.duration_micros

    @property
    def reason(self) -> str | None:
        """未参评原因；参评窗口为 None。"""
        if self.eligible:
            return None
        if self.covered_micros == 0:
            return REASON_NO_COVERAGE
        return REASON_PARTIAL_COVERAGE


@dataclass(frozen=True)
class AllocPart:
    """一条输入分摊到某个窗口的能量。"""
    window_index: int
    energy: Decimal


@dataclass(frozen=True)
class IntervalAllocation:
    """一条输入区间的能量分摊（各部分之和 == 输入能量，严格守恒）。"""
    interval_index: int          # 原始输入下标（0 起）
    energy: Decimal              # 输入能量（未舍入）
    parts: tuple[AllocPart, ...]


@dataclass
class DemandResult:
    tz: ZoneInfo                 # 对齐时区
    window_minutes: int
    unit_price: Decimal
    windows: list[Window] = field(default_factory=list)
    allocations: list[IntervalAllocation] = field(default_factory=list)
    selected: Window | None = None
    demand: Decimal = ZERO       # 计费需量（六位小数）
    fee: Decimal = ZERO          # 费用（四舍五入到分）
    total_energy: Decimal = ZERO


def generate_windows(tz: ZoneInfo, step: timedelta,
                     lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
    """UTC half-open demand windows intersecting ``[lo, hi)``.

    每个本地日以其本地午夜为锚点、沿 UTC 时间线按 ``step`` 步进；尾窗截断
    到下一个本地午夜。窗口整体按 UTC 起点升序且互不重叠。
    """
    windows: list[tuple[datetime, datetime]] = []
    one_day = timedelta(days=1)
    first = lo.astimezone(tz).date()
    last = (hi - timedelta(microseconds=1)).astimezone(tz).date()
    d = first
    while d <= last:
        anchor = datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc)
        day_end = datetime.combine(d + one_day, time.min,
                                   tzinfo=tz).astimezone(timezone.utc)
        cursor = anchor
        while cursor < day_end:
            nxt = min(cursor + step, day_end)
            if cursor < hi and nxt > lo:
                windows.append((cursor, nxt))
            cursor = nxt
        d += one_day
    return windows


def settle(req) -> DemandResult:
    """结算一次最大需量请求：切窗、分摊、选窗、计费。"""
    tz = get_zone(req.timezone)
    inputs = parse_input_intervals(req.intervals)  # 校验并按 UTC 起点排序

    lo = min(i.start for i in inputs)
    hi = max(i.end for i in inputs)
    step = timedelta(minutes=req.window_minutes)
    windows = [Window(index=i, start=s, end=e)
               for i, (s, e) in enumerate(generate_windows(tz, step, lo, hi))]

    # 输入区间与窗口均按 UTC 升序且各自互不重叠，双指针推进即可。
    allocations: list[IntervalAllocation] = []
    j = 0
    for iv in inputs:
        while j < len(windows) and windows[j].end <= iv.start:
            j += 1
        overlapped: list[tuple[Window, int]] = []
        k = j
        while k < len(windows) and windows[k].start < iv.end:
            w = windows[k]
            overlap = _micros(min(w.end, iv.end) - max(w.start, iv.start))
            w.covered_micros += overlap
            overlapped.append((w, overlap))
            k += 1
        shares = _distribute(iv.energy, [o for _, o in overlapped])
        parts = []
        for (w, _), share in zip(overlapped, shares):
            w.energy += share
            if share > 0:
                parts.append(AllocPart(window_index=w.index, energy=share))
        allocations.append(IntervalAllocation(
            interval_index=iv.index, energy=iv.energy, parts=tuple(parts)))

    eligible = [w for w in windows if w.eligible]
    if not eligible:
        raise PricingError(
            "没有任何被输入连续完整覆盖的需量窗，无法结算最大需量"
            "（只有完整覆盖的窗口才参评）",
            code="no_eligible_window", status_code=422,
            details={"timezone": str(tz), "window_minutes": req.window_minutes},
        )

    # 平均功率 ∝ 能量 ÷ 时长；交叉相乘精确比较，严格大于才替换——
    # 同值时保留 UTC 起点最早者（窗口本身按 UTC 起点升序）。
    best = eligible[0]
    for w in eligible[1:]:
        if w.energy * best.duration_micros > best.energy * w.duration_micros:
            best = w

    demand = (best.energy * _MICROS_PER_HOUR
              / best.duration_micros).quantize(Q6, rounding=ROUND_HALF_UP)
    fee = (demand * req.unit_price).quantize(Q2, rounding=ROUND_HALF_UP)

    return DemandResult(
        tz=tz, window_minutes=req.window_minutes, unit_price=req.unit_price,
        windows=windows, allocations=allocations, selected=best,
        demand=demand, fee=fee,
        total_energy=sum((a.energy for a in allocations), ZERO),
    )
