"""End-to-end tests for POST /demand-charge (in-memory SQLite)."""
import os
from decimal import Decimal

import pytest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="session", autouse=True)
def _startup():
    with client:
        yield


def _interval(start, end, energy, timezone="UTC"):
    return {"start": start, "end": end, "timezone": timezone,
            "energy": energy}


def _payload(intervals, timezone="UTC", window_minutes=15, unit_price="0.5"):
    return {
        "timezone": timezone,
        "window_minutes": window_minutes,
        "unit_price": unit_price,
        "intervals": intervals,
    }


# --------------------------------------------------------------- happy paths

def test_basic_aligned_windows_tie_breaks_to_earliest():
    # One hour, four fully covered 15-min windows, uniform load -> all four
    # average 12 kW; the tie goes to the earliest UTC start.
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T00:00:00Z", "2024-07-01T01:00:00Z", "12"),
    ]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_count"] == 4
    assert body["eligible_window_count"] == 4
    assert body["ineligible_windows"] == []
    assert body["total_energy"] == "12"
    assert body["demand"] == "12.000000"
    assert body["fee"] == "6.00"  # 12.000000 * 0.5
    selected = body["selected_window"]
    assert selected["start"] == "2024-07-01T00:00:00Z"
    assert selected["end"] == "2024-07-01T00:15:00Z"
    assert selected["start_local"] == "2024-07-01T00:00:00+00:00"
    assert selected["duration_seconds"] == 900
    assert selected["energy"] == "3.000000"
    assert selected["demand"] == "12.000000"
    # Per-input allocation: 4 parts of 3 kWh, summing exactly to the input.
    assert len(body["allocations"]) == 1
    alloc = body["allocations"][0]
    assert alloc["interval_index"] == 0
    assert alloc["energy"] == "12"
    assert [p["energy"] for p in alloc["parts"]] == ["3.000000"] * 4
    assert [p["window_start"] for p in alloc["parts"]] == [
        "2024-07-01T00:00:00Z", "2024-07-01T00:15:00Z",
        "2024-07-01T00:30:00Z", "2024-07-01T00:45:00Z",
    ]


def test_highest_average_power_window_wins():
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T00:00:00Z", "2024-07-01T00:15:00Z", "1"),
        _interval("2024-07-01T00:15:00Z", "2024-07-01T00:30:00Z", "3"),
    ], unit_price="2"))
    assert r.status_code == 200, r.text
    body = r.json()
    # Window 2 averages 3 * 4 = 12 kW against window 1's 4 kW.
    assert body["selected_window"]["start"] == "2024-07-01T00:15:00Z"
    assert body["selected_window"]["energy"] == "3.000000"
    assert body["demand"] == "12.000000"
    assert body["fee"] == "24.00"
    # Allocations come back in input order with original indices.
    assert [a["interval_index"] for a in body["allocations"]] == [0, 1]
    assert body["allocations"][0]["parts"] == [{
        "window_start": "2024-07-01T00:00:00Z",
        "window_end": "2024-07-01T00:15:00Z",
        "energy": "1.000000",
    }]


def test_allocations_follow_input_order_not_time_order():
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T01:00:00Z", "2024-07-01T01:15:00Z", "1"),
        _interval("2024-07-01T00:00:00Z", "2024-07-01T00:15:00Z", "2"),
    ]))
    assert r.status_code == 200, r.text
    allocs = r.json()["allocations"]
    assert [a["interval_index"] for a in allocs] == [0, 1]
    assert allocs[0]["parts"][0]["window_start"] == "2024-07-01T01:00:00Z"
    assert allocs[1]["parts"][0]["window_start"] == "2024-07-01T00:00:00Z"


def test_cross_window_interval_prorated_by_actual_duration():
    # 30-minute interval straddling three 15-min windows: 5/15/10 minutes.
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T00:10:00Z", "2024-07-01T00:40:00Z", "6"),
    ]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_count"] == 3
    assert body["eligible_window_count"] == 1
    alloc = body["allocations"][0]
    assert [p["energy"] for p in alloc["parts"]] == [
        "1.000000", "3.000000", "2.000000"]
    # Only the middle window is fully covered; it bills 3 * 4 = 12 kW.
    assert body["selected_window"]["start"] == "2024-07-01T00:15:00Z"
    assert body["demand"] == "12.000000"
    assert body["fee"] == "6.00"
    ineligible = {w["start"]: w for w in body["ineligible_windows"]}
    assert ineligible["2024-07-01T00:00:00Z"]["reason"] == "partial_coverage"
    assert ineligible["2024-07-01T00:00:00Z"]["covered_seconds"] == 300
    assert ineligible["2024-07-01T00:30:00Z"]["reason"] == "partial_coverage"
    assert ineligible["2024-07-01T00:30:00Z"]["covered_seconds"] == 600


def test_allocation_rounding_residual_keeps_energy_conserved():
    # 31-minute interval over three windows (8/15/8 min): 6-dp shares round,
    # the residual lands on the last overlapped window; parts sum to 1 exactly.
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T00:07:00Z", "2024-07-01T00:38:00Z", "1"),
    ], unit_price="1"))
    assert r.status_code == 200, r.text
    body = r.json()
    parts = body["allocations"][0]["parts"]
    assert [p["energy"] for p in parts] == [
        "0.258065", "0.483871", "0.258064"]
    assert sum(Decimal(p["energy"]) for p in parts) == Decimal("1")
    # The only fully covered window averages 0.483871 * 4 kW.
    assert body["selected_window"]["start"] == "2024-07-01T00:15:00Z"
    assert body["demand"] == "1.935484"
    assert body["fee"] == "1.94"  # 1.935484 * 1, half-up to cents


def test_windows_align_to_local_midnight_not_interval_start():
    # Off-grid interval: windows still align to :00 / :30.
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T00:05:00Z", "2024-07-01T01:05:00Z", "12"),
    ], window_minutes=30))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_count"] == 3
    assert body["eligible_window_count"] == 1
    assert body["selected_window"]["start"] == "2024-07-01T00:30:00Z"
    assert body["selected_window"]["energy"] == "6.000000"
    assert body["demand"] == "12.000000"  # 6 kWh over 30 min
    assert [w["reason"] for w in body["ineligible_windows"]] == [
        "partial_coverage", "partial_coverage"]


def test_gap_between_intervals_yields_no_coverage_windows():
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T00:00:00Z", "2024-07-01T00:15:00Z", "2"),
        _interval("2024-07-01T01:00:00Z", "2024-07-01T01:15:00Z", "2"),
    ]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_count"] == 5
    assert body["eligible_window_count"] == 2
    # Equal average power (8 kW) -> earliest UTC start wins.
    assert body["selected_window"]["start"] == "2024-07-01T00:00:00Z"
    assert body["demand"] == "8.000000"
    ineligible = body["ineligible_windows"]
    assert len(ineligible) == 3
    assert all(w["reason"] == "no_coverage" for w in ineligible)
    assert all(w["covered_seconds"] == 0 for w in ineligible)
    assert [w["start"] for w in ineligible] == [
        "2024-07-01T00:15:00Z", "2024-07-01T00:30:00Z",
        "2024-07-01T00:45:00Z",
    ]


def test_adjacent_intervals_jointly_cover_a_window():
    # Two back-to-back inputs continuously tile one 30-min window.
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T00:00:00Z", "2024-07-01T00:10:00Z", "1"),
        _interval("2024-07-01T00:10:00Z", "2024-07-01T00:30:00Z", "2"),
    ], window_minutes=30))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["eligible_window_count"] == 1
    assert body["selected_window"]["energy"] == "3.000000"
    assert body["demand"] == "6.000000"  # 3 kWh over 30 min
    # Both inputs allocate into the same window.
    assert body["allocations"][0]["parts"][0]["energy"] == "1.000000"
    assert body["allocations"][1]["parts"][0]["energy"] == "2.000000"


def test_fee_uses_six_decimal_demand_rounded_to_cents():
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T00:00:00Z", "2024-07-01T00:15:00Z", "0.833333"),
    ], unit_price="0.35"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["demand"] == "3.333332"  # 0.833333 * 4
    # 3.333332 * 0.35 = 1.1666662 -> 1.17 (half-up to cents)
    assert body["fee"] == "1.17"


def test_alignment_timezone_differs_from_interval_timezone():
    # Interval times given in UTC; windows align to New York local midnight.
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T04:00:00Z", "2024-07-01T05:00:00Z", "2"),
    ], timezone="America/New_York", window_minutes=60))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["timezone"] == "America/New_York"
    selected = body["selected_window"]
    assert selected["start"] == "2024-07-01T04:00:00Z"
    assert selected["start_local"] == "2024-07-01T00:00:00-04:00"
    assert selected["end_local"] == "2024-07-01T01:00:00-04:00"
    assert body["demand"] == "2.000000"


# ---------------------------------------------------------------------- DST

def test_dst_spring_forward_day_has_23_windows():
    # 2024-03-10 in New York: 02:00 -> 03:00 spring forward (23 UTC hours).
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-03-10T00:00:00-05:00", "2024-03-11T00:00:00-04:00",
                  "23", timezone="America/New_York"),
    ], timezone="America/New_York", window_minutes=60, unit_price="1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_count"] == 23
    assert body["eligible_window_count"] == 23
    parts = body["allocations"][0]["parts"]
    assert len(parts) == 23
    assert all(p["energy"] == "1.000000" for p in parts)
    # Windows step along the UTC timeline: 06:00Z -> 07:00Z is the window
    # whose local clock runs 01:00 straight to 03:00 (02:00 does not exist).
    by_start = {p["window_start"]: p for p in parts}
    assert "2024-03-10T07:00:00Z" in by_start  # local 03:00 EDT
    assert body["demand"] == "1.000000"
    assert body["fee"] == "1.00"


def test_dst_fall_back_day_has_25_windows_and_repeated_boundaries():
    # 2024-11-03 in New York: 02:00 -> 01:00 fall back (25 UTC hours).
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-11-03T00:00:00-04:00", "2024-11-04T00:00:00-05:00",
                  "25", timezone="America/New_York"),
    ], timezone="America/New_York", window_minutes=60, unit_price="1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_count"] == 25
    assert body["eligible_window_count"] == 25
    parts = body["allocations"][0]["parts"]
    assert len(parts) == 25
    # The repeated local 01:00 hour yields two distinct UTC windows
    # (05:00Z and 06:00Z), each computed separately.
    starts = [p["window_start"] for p in parts]
    assert "2024-11-03T05:00:00Z" in starts
    assert "2024-11-03T06:00:00Z" in starts
    assert body["demand"] == "1.000000"


def test_dst_repeated_hour_windows_are_settled_separately():
    # First pass through 01:00 (EDT) carries 4 kWh, the repeated pass
    # (EST) carries 1 kWh; they are separate windows and the first wins.
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-11-03T01:00:00-04:00", "2024-11-03T02:00:00-04:00",
                  "4", timezone="America/New_York"),
        _interval("2024-11-03T01:00:00-05:00", "2024-11-03T02:00:00-05:00",
                  "1", timezone="America/New_York"),
    ], timezone="America/New_York", window_minutes=60, unit_price="2"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_count"] == 2
    assert body["eligible_window_count"] == 2
    selected = body["selected_window"]
    assert selected["start"] == "2024-11-03T05:00:00Z"
    assert selected["start_local"] == "2024-11-03T01:00:00-04:00"
    assert selected["end_local"] == "2024-11-03T01:00:00-05:00"
    assert selected["energy"] == "4.000000"
    assert body["demand"] == "4.000000"
    assert body["fee"] == "8.00"


# -------------------------------------------------------------------- errors

def test_no_eligible_window_rejected():
    # 10 minutes of usage can never fully cover a 15-minute window.
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T00:10:00Z", "2024-07-01T00:20:00Z", "1"),
    ]))
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "no_eligible_window"
    assert body["details"]["window_minutes"] == 15


def test_window_minutes_must_be_15_30_or_60():
    base = [_interval("2024-07-01T00:00:00Z", "2024-07-01T01:00:00Z", "1")]
    for bad in (0, 7, 14, 61, 15.0, True, "15"):
        r = client.post("/demand-charge",
                        json=_payload(base, window_minutes=bad))
        assert r.status_code == 422, bad


def test_unit_price_must_be_positive_finite_decimal():
    base = [_interval("2024-07-01T00:00:00Z", "2024-07-01T01:00:00Z", "1")]
    for bad in ("0", "-0.5", "NaN", "Infinity"):
        r = client.post("/demand-charge", json=_payload(base, unit_price=bad))
        assert r.status_code == 422, bad


def test_invalid_alignment_timezone_rejected():
    r = client.post("/demand-charge", json=_payload(
        [_interval("2024-07-01T00:00:00Z", "2024-07-01T01:00:00Z", "1")],
        timezone="Mars/Olympus"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_timezone"


def test_interval_validation_matches_quote_semantics():
    # Ambiguous naive local time during the fall-back repeated hour.
    r = client.post("/demand-charge", json=_payload(
        [_interval("2024-11-03T01:30:00", "2024-11-03T03:00:00", "1",
                   timezone="America/New_York")],
        timezone="America/New_York"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ambiguous_local_time"

    # Nonexistent naive local time in the spring-forward gap.
    r = client.post("/demand-charge", json=_payload(
        [_interval("2024-03-10T02:30:00", "2024-03-10T04:00:00", "1",
                   timezone="America/New_York")],
        timezone="America/New_York"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "nonexistent_local_time"

    # end <= start.
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T01:00:00Z", "2024-07-01T00:00:00Z", "1"),
    ]))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_interval"

    # Overlapping intervals.
    r = client.post("/demand-charge", json=_payload([
        _interval("2024-07-01T00:00:00Z", "2024-07-01T00:30:00Z", "1"),
        _interval("2024-07-01T00:15:00Z", "2024-07-01T00:45:00Z", "1"),
    ]))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "interval_overlap"


def test_schema_bounds_and_extra_fields():
    ok = _payload([_interval("2024-07-01T00:00:00Z",
                             "2024-07-01T01:00:00Z", "1")])
    # intervals must be non-empty.
    empty = dict(ok, intervals=[])
    assert client.post("/demand-charge", json=empty).status_code == 422
    # energy must be positive (shared QuoteIntervalIn schema).
    zero_energy = _payload([_interval("2024-07-01T00:00:00Z",
                                      "2024-07-01T01:00:00Z", "0")])
    assert client.post("/demand-charge", json=zero_energy).status_code == 422
    # extra fields are forbidden.
    extra = dict(ok, version="nope")
    assert client.post("/demand-charge", json=extra).status_code == 422
