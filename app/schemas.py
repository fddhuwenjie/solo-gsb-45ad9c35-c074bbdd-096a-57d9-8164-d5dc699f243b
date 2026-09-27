"""Pydantic request/response models."""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class QuoteIntervalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(
        ..., description="ISO-8601 开始时间；建议携带偏移量（如 2024-03-10T01:00:00-05:00）"
    )
    end: str = Field(..., description="ISO-8601 结束时间，半开区间 [start, end)")
    timezone: str = Field(
        ..., description="解释朴素时间所用的 IANA 时区，如 America/New_York"
    )
    energy: Decimal = Field(..., gt=0, description="该区间累计用量（如 kWh）")

    def energy_decimal(self) -> Decimal:
        return self.energy


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(..., description="规则版本号，如 us-ny-demo-v2024")
    intervals: list[QuoteIntervalIn] = Field(..., min_length=1)


class QuoteDiffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before_version: str = Field(..., description="基准规则版本号")
    after_version: str = Field(..., description="对比目标规则版本号")
    intervals: list[QuoteIntervalIn] = Field(
        ..., min_length=1, description="与 /quote 相同的用量区间格式"
    )


class DiffSideOut(BaseModel):
    rule_code: str
    rule_name: str
    tier_ordinal: int
    unit_price: str
    energy: str
    amount: str


class DiffSegmentOut(BaseModel):
    interval_index: int
    start: str
    end: str
    duration_seconds: int
    before: DiffSideOut | None
    after: DiffSideOut | None
    energy_diff: str
    amount_diff: str
    reasons: list[str]


class QuoteDiffResponse(BaseModel):
    before_version: str
    after_version: str
    currency: str
    energy_unit: str
    total_energy: str
    before_total_amount: str
    after_total_amount: str
    total_amount_diff: str
    before_matched_rules: list[str]
    after_matched_rules: list[str]
    segments: list[DiffSegmentOut]


class SegmentOut(BaseModel):
    interval_index: int
    start: str
    end: str
    start_local: str
    end_local: str
    timezone: str
    duration_seconds: int
    day_kind: str
    energy: str
    energy_unit: str
    rule_code: str
    rule_name: str
    tier_ordinal: int
    cumulative_from: str
    cumulative_to: str
    unit_price: str
    currency: str
    amount: str


class QuoteResponse(BaseModel):
    version: str
    currency: str
    energy_unit: str
    total_energy: str
    total_amount: str
    matched_rules: list[str]
    segments: list[SegmentOut]


class TierOut(BaseModel):
    ordinal: int
    cumulative_cap: str | None
    unit_price: str


class RuleOut(BaseModel):
    code: str
    name: str
    effective_from: str
    effective_to: str
    timezone: str
    weekdays: str
    day_scope: str
    time_on: str
    time_off: str
    tiers: list[TierOut]


class VersionOut(BaseModel):
    code: str
    currency: str
    energy_unit: str
    description: str
    rules: list[RuleOut]


# ------------------------------------------------------------- version import


class HolidayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str = Field(..., description="ISO 日期，如 2024-01-01")
    kind: str = Field(
        "holiday", description="holiday=法定假日；workday=调休上班日"
    )
    name: str = Field("", description="节日名称（可选）")


class TierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinal: int = Field(..., description="阶梯序号，从 1 开始连续编号")
    cumulative_cap: Decimal | None = Field(
        None, description="累计用量上限；最后一档必须为 null（开放）"
    )
    unit_price: Decimal = Field(..., description="单价（每能量单位，不得为负）")


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="规则代码，版本内唯一")
    name: str = ""
    effective_from: str = Field(
        ..., description="生效起点（规则时区的本地朴素时间，ISO-8601）"
    )
    effective_to: str = Field(
        ..., description="生效终点（本地朴素时间，半开区间）"
    )
    timezone: str = Field(
        ..., description="IANA 时区；同一版本内所有规则必须一致"
    )
    weekdays: str | list[int] = Field(
        "", description="ISO 星期掩码：逗号分隔字符串（如 \"1,2,3\"）或整数数组；空=每天"
    )
    day_scope: str = Field(
        "all", description="all / workdays / holidays / non_workdays / weekends"
    )
    time_on: str = Field(..., description="日内窗口起点 HH:MM")
    time_off: str = Field(
        ..., description="日内窗口终点 HH:MM（半开；可用 24:00 表示日末；"
        "off <= on 表示跨午夜）"
    )
    tiers: list[TierIn] = Field(default_factory=list)


class VersionImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="版本号，全局唯一且内容不可变")
    currency: str = Field(..., description="币种，如 USD / CNY")
    energy_unit: str = Field(..., description="能量单位，如 kWh")
    description: str = ""
    holidays: list[HolidayIn] = Field(default_factory=list)
    rules: list[RuleIn] = Field(default_factory=list)


class HolidayOut(BaseModel):
    day: str
    kind: str
    name: str


class VersionImportResponse(BaseModel):
    code: str
    content_digest: str = Field(
        ..., description="规范化内容的 SHA-256（十六进制）；列表顺序不影响摘要"
    )
    currency: str
    energy_unit: str
    description: str
    holidays: list[HolidayOut]
    rules: list[RuleOut]


class ShiftTaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    earliest_start: str = Field(
        ...,
        description="最早可开机的 ISO-8601 时刻，必须携带 UTC 偏移量（如 "
        "2024-07-06T20:00:00-04:00 或 2024-07-07T00:00:00Z）",
    )
    latest_end: str = Field(
        ...,
        description="最晚完工的 ISO-8601 时刻，必须携带 UTC 偏移量；"
        "任务须在此时刻之前结束（半开区间）",
    )
    duration_minutes: int = Field(
        ..., gt=0, description="任务连续运行时长（整数分钟）"
    )
    energy: Decimal = Field(..., gt=0, description="该任务的正用量（如 kWh）")

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _reject_non_integer(cls, value):
        # bool is an int subclass; JSON 60.0 would also be coerced silently.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("duration_minutes 必须是整数分钟")
        return value


class ShiftPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(..., description="规则版本号，如 us-ny-demo-v2024")
    timezone: str = Field(
        ..., description="回显本地时间所用的 IANA 时区，如 America/New_York"
    )
    tasks: list[ShiftTaskIn] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="恰好两项可移时用电任务，按输入顺序编号为 0、1",
    )


class ShiftTaskOut(BaseModel):
    task_index: int
    start: str
    end: str
    start_local: str
    end_local: str
    timezone: str
    duration_minutes: int
    energy: str


class ShiftSegmentOut(BaseModel):
    # task_index 标明该计价段归属哪一项任务（即输入顺序 0/1）
    task_index: int
    start: str
    end: str
    start_local: str
    end_local: str
    timezone: str
    duration_seconds: int
    day_kind: str
    energy: str
    energy_unit: str
    rule_code: str
    rule_name: str
    tier_ordinal: int
    cumulative_from: str
    cumulative_to: str
    unit_price: str
    currency: str
    amount: str


class ShiftPlanResponse(BaseModel):
    version: str
    currency: str
    energy_unit: str
    timezone: str
    feasible: bool
    total_energy: str
    total_amount: str
    tasks: list[ShiftTaskOut]
    segments: list[ShiftSegmentOut]
    candidates_evaluated: int
    non_overlapping_pairs: int
    rule_gap_pairs: int
    reason_code: str | None = Field(
        None, description="无可行组合时的机器可读原因"
    )
    reason: str | None = Field(None, description="无可行组合时的说明")
    gaps: list[dict] | None = Field(
        None, description="被规则缺口淘汰的候选组合示例（最多 5 条）"
    )


# ------------------------------------------------------------- demand charge


class DemandChargeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(
        ..., description="需量窗对齐所用的 IANA 时区，如 America/New_York"
    )
    window_minutes: int = Field(
        ..., description="需量窗分钟数：15 / 30 / 60，按本地午夜起算对齐"
    )
    unit_price: Decimal = Field(
        ..., gt=0, allow_inf_nan=False,
        description="正需量单价（每 能量单位·小时 的价格，如 USD/kW）",
    )
    intervals: list[QuoteIntervalIn] = Field(
        ..., min_length=1, description="与 /quote 相同的用量区间格式"
    )

    @field_validator("window_minutes", mode="before")
    @classmethod
    def _reject_non_integer(cls, value):
        # bool is an int subclass; JSON 15.0 would also be coerced silently.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("window_minutes 必须是整数分钟（15 / 30 / 60）")
        return value

    @field_validator("window_minutes")
    @classmethod
    def _in_allowed_range(cls, value):
        if value not in (15, 30, 60):
            raise ValueError("window_minutes 只支持 15 / 30 / 60 分钟需量窗")
        return value


class SelectedWindowOut(BaseModel):
    start: str = Field(..., description="UTC 半开窗口起点（Z 后缀）")
    end: str = Field(..., description="UTC 半开窗口终点（Z 后缀）")
    start_local: str = Field(..., description="对齐时区的本地起点（含偏移）")
    end_local: str = Field(..., description="对齐时区的本地终点（含偏移）")
    duration_seconds: int
    energy: str = Field(..., description="分摊进该窗口的能量（六位小数）")
    demand: str = Field(..., description="窗口平均功率（六位小数，即计费需量）")


class IneligibleWindowOut(BaseModel):
    start: str
    end: str
    start_local: str
    end_local: str
    duration_seconds: int
    covered_seconds: int = Field(..., description="被输入覆盖的秒数")
    reason: str = Field(..., description="no_coverage | partial_coverage")


class DemandAllocPartOut(BaseModel):
    window_start: str = Field(..., description="分摊目标窗口的 UTC 起点（Z 后缀）")
    window_end: str = Field(..., description="分摊目标窗口的 UTC 终点（Z 后缀）")
    energy: str = Field(..., description="分摊能量（六位小数）")


class DemandAllocationOut(BaseModel):
    interval_index: int = Field(..., description="输入区间的原始下标（0 起）")
    energy: str = Field(..., description="该输入的能量（未舍入）")
    parts: list[DemandAllocPartOut] = Field(
        ..., description="按实际时长分摊到各重叠窗口的能量；各部分之和 == energy"
    )


class DemandChargeResponse(BaseModel):
    timezone: str
    window_minutes: int
    unit_price: str
    total_energy: str = Field(..., description="全部输入能量之和（未舍入）")
    window_count: int = Field(..., description="输入跨度内生成的需量窗总数")
    eligible_window_count: int = Field(..., description="参评（完整覆盖）窗口数")
    demand: str = Field(..., description="计费需量：获选窗口平均功率（六位小数）")
    fee: str = Field(..., description="需量费用 = demand × unit_price，四舍五入到分")
    selected_window: SelectedWindowOut
    allocations: list[DemandAllocationOut]
    ineligible_windows: list[IneligibleWindowOut]


# ---------------------------------------------------------- meter reconcile


class MeterReadingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: str = Field(
        ..., description="读数时刻（ISO-8601），必须携带 UTC 偏移量（如 Z / +08:00）"
    )
    value: Decimal = Field(
        ..., allow_inf_nan=False,
        description="累计计数器读数，必须落在 [0, modulus) 内",
    )


class MeterReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readings: list[MeterReadingIn] = Field(
        ..., min_length=2, max_length=500,
        description="2~500 条累计读数；顺序任意，服务端按 UTC 时刻排序处理",
    )
    modulus: Decimal = Field(
        ..., gt=0, allow_inf_nan=False,
        description="计数器模数（正数）：读数达到 modulus 后回卷为 0",
    )
    max_interval_seconds: int = Field(
        ..., gt=0,
        description="最大采样间隔（正整数秒）；相邻读数间隔超过它只记为 gap",
    )
    resets: list[str] = Field(
        default_factory=list,
        description="可选的计数器 reset 时刻（ISO-8601，必须携带 UTC 偏移量）；"
        "每个 reset 都必须命中某条读数，命中处序列断开",
    )

    @field_validator("max_interval_seconds", mode="before")
    @classmethod
    def _reject_non_integer(cls, value):
        # bool is an int subclass; JSON 60.0 would also be coerced silently.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("max_interval_seconds 必须是正整数秒")
        return value


class MeterIntervalOut(BaseModel):
    start: str = Field(..., description="UTC 半开区间起点（Z 后缀）")
    end: str = Field(..., description="UTC 半开区间终点（Z 后缀）")
    duration_seconds: int
    from_index: int = Field(..., description="起点读数在请求 readings 中的原始下标")
    to_index: int = Field(..., description="终点读数在请求 readings 中的原始下标")
    wrapped: bool = Field(..., description="是否按一次 modulus 回卷计算")
    energy: str = Field(..., description="该区间精确用量（未舍入）")


class MeterGapOut(BaseModel):
    start: str
    end: str
    duration_seconds: int
    from_index: int
    to_index: int
    reason: str = Field(..., description="reset | max_interval_exceeded")


class MeterReconcileResponse(BaseModel):
    modulus: str
    reading_count: int = Field(..., description="去重后的读数条数")
    duplicate_count: int = Field(..., description="被去重丢弃的重复读数条数")
    total_energy: str = Field(
        ..., description="全部区间用量之和（精确值），等于各连续段净增量之和"
    )
    intervals: list[MeterIntervalOut]
    gaps: list[MeterGapOut]
