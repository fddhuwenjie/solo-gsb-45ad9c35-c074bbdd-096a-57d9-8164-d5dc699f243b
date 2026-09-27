"""FastAPI application for time-of-use tiered tariff quoting."""
from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import demand, diff, importer, meter, pricing, shiftplan
from .database import engine, init_db
from .pricing import (
    Q2,
    Q6,
    PricingError,
    load_version,
    validate_version,
)
from .schemas import (
    DemandAllocPartOut,
    DemandAllocationOut,
    DemandChargeRequest,
    DemandChargeResponse,
    DiffSegmentOut,
    DiffSideOut,
    IneligibleWindowOut,
    MeterGapOut,
    MeterIntervalOut,
    MeterReconcileRequest,
    MeterReconcileResponse,
    QuoteDiffRequest,
    QuoteDiffResponse,
    QuoteRequest,
    QuoteResponse,
    RuleOut,
    SegmentOut,
    SelectedWindowOut,
    ShiftPlanRequest,
    ShiftPlanResponse,
    ShiftSegmentOut,
    ShiftTaskOut,
    TierOut,
    VersionImportRequest,
    VersionImportResponse,
    VersionOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="分时阶梯计价 API",
    description=(
        "基于版本化费率规则（生效时间段/时区/工作日节假日条件/分段单价/"
        "累计用量上限）的报价服务。所有时间区间均为半开 [start, end)。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(PricingError)
async def pricing_error_handler(request: Request, exc: PricingError):
    body: dict = {"error": {
        "code": exc.code,
        "message": str(exc),
    }}
    if exc.details is not None:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/versions", response_model=list[VersionOut], tags=["versions"])
def list_versions():
    with Session(engine) as session:
        versions = pricing.load_all_versions(session)
        out = []
        for v in versions:
            out.append(_version_out(v, include_rules=True))
        return out


@app.get("/versions/{version_code}", response_model=VersionOut,
         tags=["versions"])
def get_version(version_code: str):
    with Session(engine) as session:
        return _version_out(load_version(session, version_code),
                            include_rules=True)


@app.get("/versions/{version_code}/issues", tags=["versions"])
def get_version_issues(version_code: str):
    """显式返回版本的规则重叠 / 阶梯缺口等结构性问题。"""
    with Session(engine) as session:
        v = load_version(session, version_code)
        issues = validate_version(v)
        return {"version": version_code, "ok": not issues, "issues": issues}


@app.post("/versions/import", response_model=VersionImportResponse,
          status_code=201, tags=["versions"])
def post_version_import(req: VersionImportRequest, response: Response):
    """导入完整费率版本（含 holidays / rules / tiers）。

    首次导入在单个事务内写入全部层级并返回 201；同 code 同摘要的重放
    返回首次的结果（200）；同 code 不同摘要拒绝（409）。导入成功后
    版本立即可用于 /quote、/quote-diff 与 /shift-plan。
    """
    body, created = importer.import_version(req)
    if not created:
        response.status_code = 200
    return body


@app.post("/quote", response_model=QuoteResponse, tags=["quote"])
def post_quote(req: QuoteRequest):
    with Session(engine) as session:
        version = load_version(session, req.version)
        result = pricing.quote(version, req.intervals)

    segments = [
        SegmentOut(**s.to_dict(version.energy_unit, version.currency))
        for s in result.segments
    ]
    total_energy = result.total_energy
    total_amount = result.total_amount.quantize(Q2, rounding=ROUND_HALF_UP)
    return QuoteResponse(
        version=version.code,
        currency=version.currency,
        energy_unit=version.energy_unit,
        total_energy=str(total_energy),
        total_amount=str(total_amount),
        matched_rules=result.matched_rules,
        segments=segments,
    )


@app.post("/quote-diff", response_model=QuoteDiffResponse, tags=["quote"])
def post_quote_diff(req: QuoteDiffRequest):
    with Session(engine) as session:
        before = load_version(session, req.before_version)
        after = load_version(session, req.after_version)
        result = diff.quote_diff(before, after, req.intervals)

    segments = [
        DiffSegmentOut(
            interval_index=s.interval_index,
            start=s.start.isoformat().replace("+00:00", "Z"),
            end=s.end.isoformat().replace("+00:00", "Z"),
            duration_seconds=int((s.end - s.start).total_seconds()),
            before=_diff_side_out(s.before),
            after=_diff_side_out(s.after),
            energy_diff=str(s.energy_diff.quantize(Q6)),
            amount_diff=str(s.amount_diff.quantize(Q6)),
            reasons=s.reasons,
        )
        for s in result.segments
    ]
    before_total = result.before.total_amount.quantize(
        Q2, rounding=ROUND_HALF_UP)
    after_total = result.after.total_amount.quantize(
        Q2, rounding=ROUND_HALF_UP)
    return QuoteDiffResponse(
        before_version=before.code,
        after_version=after.code,
        currency=before.currency,
        energy_unit=before.energy_unit,
        total_energy=str(result.before.total_energy),
        before_total_amount=str(before_total),
        after_total_amount=str(after_total),
        total_amount_diff=str(after_total - before_total),
        before_matched_rules=result.before.matched_rules,
        after_matched_rules=result.after.matched_rules,
        segments=segments,
    )


def _diff_side_out(side: "diff.DiffSide | None") -> DiffSideOut | None:
    if side is None:
        return None
    return DiffSideOut(
        rule_code=side.rule_code,
        rule_name=side.rule_name,
        tier_ordinal=side.tier_ordinal,
        unit_price=str(side.unit_price),
        energy=str(side.energy.quantize(Q6)),
        amount=str(side.amount.quantize(Q6)),
    )


@app.post("/meter-readings/reconcile", response_model=MeterReconcileResponse,
          tags=["meter"])
def post_meter_readings_reconcile(req: MeterReconcileRequest):
    """把累计表计读数整理成可直接提交 /quote 的 UTC 半开用量区间。

    不触碰数据库；所有校验失败都返回统一的 error 响应体。
    """
    result = meter.reconcile(req)
    return MeterReconcileResponse(
        modulus=str(req.modulus),
        reading_count=result.reading_count,
        duplicate_count=result.duplicate_count,
        total_energy=str(result.total_energy),
        intervals=[
            MeterIntervalOut(
                start=meter.to_utc_z(i.start),
                end=meter.to_utc_z(i.end),
                duration_seconds=int((i.end - i.start).total_seconds()),
                from_index=i.from_index,
                to_index=i.to_index,
                wrapped=i.wrapped,
                energy=str(i.energy),
            )
            for i in result.intervals
        ],
        gaps=[
            MeterGapOut(
                start=meter.to_utc_z(g.start),
                end=meter.to_utc_z(g.end),
                duration_seconds=int((g.end - g.start).total_seconds()),
                from_index=g.from_index,
                to_index=g.to_index,
                reason=g.reason,
            )
            for g in result.gaps
        ],
    )


@app.post("/demand-charge", response_model=DemandChargeResponse,
          tags=["demand"])
def post_demand_charge(req: DemandChargeRequest):
    """按本地午夜对齐的需量窗结算最大需量费用。

    不触碰数据库；所有校验失败都返回统一的 error 响应体。
    """
    result = demand.settle(req)
    tz = result.tz
    selected = result.selected
    windows_by_index = {w.index: w for w in result.windows}

    allocations = []
    for alloc in sorted(result.allocations, key=lambda a: a.interval_index):
        allocations.append(DemandAllocationOut(
            interval_index=alloc.interval_index,
            energy=str(alloc.energy),
            parts=[
                DemandAllocPartOut(
                    window_start=demand.to_utc_z(
                        windows_by_index[p.window_index].start),
                    window_end=demand.to_utc_z(
                        windows_by_index[p.window_index].end),
                    energy=str(p.energy.quantize(Q6)),
                )
                for p in alloc.parts
            ],
        ))

    return DemandChargeResponse(
        timezone=str(tz),
        window_minutes=result.window_minutes,
        unit_price=str(result.unit_price),
        total_energy=str(result.total_energy),
        window_count=len(result.windows),
        eligible_window_count=sum(1 for w in result.windows if w.eligible),
        demand=str(result.demand),
        fee=str(result.fee),
        selected_window=SelectedWindowOut(
            start=demand.to_utc_z(selected.start),
            end=demand.to_utc_z(selected.end),
            start_local=selected.start.astimezone(tz).isoformat(),
            end_local=selected.end.astimezone(tz).isoformat(),
            duration_seconds=int((selected.end - selected.start)
                                 .total_seconds()),
            energy=str(selected.energy.quantize(Q6)),
            demand=str(result.demand),
        ),
        allocations=allocations,
        ineligible_windows=[
            IneligibleWindowOut(
                start=demand.to_utc_z(w.start),
                end=demand.to_utc_z(w.end),
                start_local=w.start.astimezone(tz).isoformat(),
                end_local=w.end.astimezone(tz).isoformat(),
                duration_seconds=int((w.end - w.start).total_seconds()),
                covered_seconds=w.covered_micros // 1_000_000,
                reason=w.reason,
            )
            for w in result.windows if not w.eligible
        ],
    )


@app.post("/shift-plan", response_model=ShiftPlanResponse, tags=["quote"])
def post_shift_plan(req: ShiftPlanRequest):
    with Session(engine) as session:
        version = load_version(session, req.version)
        plan = shiftplan.plan_shift(version, req)

    if not plan.feasible:
        return ShiftPlanResponse(
            version=version.code,
            currency=version.currency,
            energy_unit=version.energy_unit,
            timezone=req.timezone,
            feasible=False,
            total_energy="0.000000",
            total_amount="0.00",
            tasks=[],
            segments=[],
            candidates_evaluated=plan.candidates_evaluated,
            non_overlapping_pairs=plan.non_overlapping_pairs,
            rule_gap_pairs=plan.rule_gap_pairs,
            reason_code=plan.reason_code,
            reason=plan.reason,
            gaps=plan.gaps or None,
        )

    starts = plan.best_starts
    tasks_out = []
    for task, start in zip(plan.tasks, starts):
        end = start + task.duration
        tasks_out.append(ShiftTaskOut(
            task_index=task.index,
            start=shiftplan.to_utc_z(start),
            end=shiftplan.to_utc_z(end),
            start_local=start.astimezone(task.tz).isoformat(),
            end_local=end.astimezone(task.tz).isoformat(),
            timezone=str(task.tz),
            duration_minutes=int(task.duration.total_seconds() // 60),
            energy=str(task.energy.quantize(Q6)),
        ))
    # interval_index == task index (payload is built in input order); the
    # field name makes task ownership explicit on each priced segment.
    segments = []
    for s in plan.result.segments:
        fields = s.to_dict(version.energy_unit, version.currency)
        fields.pop("interval_index")
        segments.append(ShiftSegmentOut(task_index=s.interval_index, **fields))
    total_amount = plan.result.total_amount.quantize(
        Q2, rounding=ROUND_HALF_UP)
    return ShiftPlanResponse(
        version=version.code,
        currency=version.currency,
        energy_unit=version.energy_unit,
        timezone=req.timezone,
        feasible=True,
        total_energy=str(plan.result.total_energy),
        total_amount=str(total_amount),
        tasks=tasks_out,
        segments=segments,
        candidates_evaluated=plan.candidates_evaluated,
        non_overlapping_pairs=plan.non_overlapping_pairs,
        rule_gap_pairs=plan.rule_gap_pairs,
    )


def _version_out(v, *, include_rules: bool) -> dict:
    return {
        "code": v.code,
        "currency": v.currency,
        "energy_unit": v.energy_unit,
        "description": v.description,
        "rules": [
            RuleOut(
                code=r.code, name=r.name,
                effective_from=r.effective_from.isoformat(),
                effective_to=r.effective_to.isoformat(),
                timezone=str(r.tz),
                weekdays=",".join(str(d) for d in sorted(r.weekdays)),
                day_scope=r.day_scope,
                time_on=r.time_on.isoformat(),
                time_off=(
                    "24:00" if r.end_of_day else r.time_off.isoformat()
                ),
                tiers=[
                    TierOut(
                        ordinal=t.ordinal,
                        cumulative_cap=(str(t.cumulative_cap)
                                        if t.cumulative_cap is not None else None),
                        unit_price=str(t.unit_price),
                    )
                    for t in r.tiers
                ],
            )
            for r in v.rules
        ],
    }
