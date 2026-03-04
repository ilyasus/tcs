from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable


@dataclass(frozen=True)
class PlanInfo:
    code: str
    name: str
    description: str
    effective_date: str
    source_url: str


@dataclass(frozen=True)
class SessionChargeEstimate:
    total_price_usd: float
    breakdown: dict[str, dict[str, float]]


PERIOD_ORDER = ["off_peak", "partial_peak", "peak"]
PERIOD_LABELS = {
    "off_peak": "Off-peak",
    "partial_peak": "Partial-peak",
    "peak": "Peak",
}


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    d = date(year, month, day)
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    d = d + timedelta(days=offset + (n - 1) * 7)
    return d


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def is_holiday(d: date) -> bool:
    year = d.year
    holidays = {
        _observed_fixed_holiday(year, 1, 1),
        _nth_weekday(year, 2, 0, 3),
        _last_weekday(year, 5, 0),
        _observed_fixed_holiday(year, 7, 4),
        _nth_weekday(year, 9, 0, 1),
        _observed_fixed_holiday(year, 11, 11),
        _nth_weekday(year, 11, 3, 4),
        _observed_fixed_holiday(year, 12, 25),
    }
    return d in holidays


PLANS: dict[str, PlanInfo] = {
    "EV2-A": PlanInfo(
        code="EV2-A",
        name="PG&E EV2-A",
        description="Whole-home EV TOU plan with summer/winter peak, partial-peak, and off-peak periods.",
        effective_date="2026-03-01",
        source_url="https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_EV2%20%28Sch%29.pdf",
    ),
    "EV-B": PlanInfo(
        code="EV-B",
        name="PG&E EV-B",
        description="Separate EV meter TOU plan. Weekday and weekend/holiday periods differ.",
        effective_date="2026-03-01",
        source_url="https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_EV%20%28Sch%29.pdf",
    ),
    "E-ELEC": PlanInfo(
        code="E-ELEC",
        name="PG&E E-ELEC",
        description="Electrification TOU plan with summer/winter peak, partial-peak, and off-peak periods.",
        effective_date="2026-03-01",
        source_url="https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-ELEC.pdf",
    ),
}


def _ev2_period_rate(dt: datetime) -> tuple[str, float]:
    summer = dt.month in (6, 7, 8, 9)
    h = dt.hour + dt.minute / 60.0
    if 16 <= h < 21:
        return "peak", (0.53809 if summer else 0.41099)
    if 15 <= h < 16 or 21 <= h < 24:
        return "partial_peak", (0.42760 if summer else 0.39428)
    return "off_peak", 0.22558


def _eelec_period_rate(dt: datetime) -> tuple[str, float]:
    summer = dt.month in (6, 7, 8, 9)
    h = dt.hour + dt.minute / 60.0
    if 16 <= h < 21:
        return "peak", (0.55214 if summer else 0.32063)
    if 15 <= h < 16 or 21 <= h < 24:
        return "partial_peak", (0.39026 if summer else 0.29854)
    return "off_peak", (0.33358 if summer else 0.28468)


def _evb_period_rate(dt: datetime) -> tuple[str, float]:
    summer = dt.month in (5, 6, 7, 8, 9, 10)
    h = dt.hour + dt.minute / 60.0
    weekend_or_holiday = dt.weekday() >= 5 or is_holiday(dt.date())

    if weekend_or_holiday:
        if 15 <= h < 19:
            return "peak", (0.62131 if summer else 0.43878)
        return "off_peak", (0.26465 if summer else 0.23504)

    if 14 <= h < 21:
        return "peak", (0.62131 if summer else 0.43878)
    if 7 <= h < 14 or 21 <= h < 23:
        return "partial_peak", (0.37720 if summer else 0.30677)
    return "off_peak", (0.26465 if summer else 0.23504)


_PERIOD_RATE_FN: dict[str, Callable[[datetime], tuple[str, float]]] = {
    "EV2-A": _ev2_period_rate,
    "EV-B": _evb_period_rate,
    "E-ELEC": _eelec_period_rate,
}


def valid_plan_or_default(plan_code: str | None, default_plan: str) -> str:
    if plan_code and plan_code in PLANS:
        return plan_code
    return default_plan if default_plan in PLANS else "EV2-A"


def estimate_session_charge(
    *,
    start_local: datetime,
    end_local: datetime,
    energy_kwh: float,
    plan_code: str,
) -> SessionChargeEstimate:
    breakdown = {
        period: {"kwh": 0.0, "cost_usd": 0.0}
        for period in PERIOD_ORDER
    }

    if energy_kwh <= 0 or end_local <= start_local:
        return SessionChargeEstimate(total_price_usd=0.0, breakdown=breakdown)

    period_rate_fn = _PERIOD_RATE_FN[plan_code]
    total_seconds = (end_local - start_local).total_seconds()
    if total_seconds <= 0:
        return SessionChargeEstimate(total_price_usd=0.0, breakdown=breakdown)

    t = start_local
    while t < end_local:
        nxt = min(t + timedelta(minutes=1), end_local)
        sec = (nxt - t).total_seconds()
        period, rate = period_rate_fn(t)
        slice_kwh = energy_kwh * (sec / total_seconds)
        slice_cost = slice_kwh * rate
        breakdown[period]["kwh"] += slice_kwh
        breakdown[period]["cost_usd"] += slice_cost
        t = nxt

    total_cost = 0.0
    for period in PERIOD_ORDER:
        breakdown[period]["kwh"] = round(breakdown[period]["kwh"], 4)
        breakdown[period]["cost_usd"] = round(breakdown[period]["cost_usd"], 2)
        total_cost += breakdown[period]["cost_usd"]

    return SessionChargeEstimate(total_price_usd=round(total_cost, 2), breakdown=breakdown)
