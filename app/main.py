from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import re
from typing import Optional
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .database import init_db
from .poller import Poller
from .pricing import (
    PERIOD_LABELS,
    PERIOD_ORDER,
    PLANS,
    estimate_session_charge,
    valid_plan_or_default,
)
from .repository import Repository
from .tesla_client import TeslaWallConnectorClient

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Tesla Wall Charger Tracker")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

repository = Repository()
client = TeslaWallConnectorClient(
    base_url=settings.tesla_base_url,
    timeout_seconds=settings.request_timeout_seconds,
)
poller: Optional[Poller] = None


def _load_timezone(raw_tz: str) -> tuple[timezone | ZoneInfo, str]:
    try:
        return ZoneInfo(raw_tz), raw_tz
    except ZoneInfoNotFoundError:
        pass

    m = re.fullmatch(r"([+-])(\d{2}):(\d{2})", raw_tz.strip())
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        minutes = int(m.group(3))
        offset = timedelta(hours=hours, minutes=minutes) * sign
        return timezone(offset), raw_tz

    logging.warning("Invalid APP_TIMEZONE '%s', falling back to UTC", raw_tz)
    return timezone.utc, "UTC (fallback)"


APP_TZ, APP_TZ_NAME = _load_timezone(settings.app_timezone)


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _format_dt(ts: Optional[str]) -> str:
    dt = _parse_iso(ts)
    if dt is None:
        return "-"
    return dt.astimezone(APP_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _format_duration_hm(total_seconds: Optional[int]) -> str:
    if total_seconds is None:
        return "-"
    seconds = max(0, int(total_seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


def _read_live_telemetry() -> tuple[Optional[dict], Optional[str]]:
    try:
        payload = client.read_sample()
        sample = Poller._to_sample(payload)
    except Exception as exc:
        logging.warning("Live telemetry read failed: %s", exc)
        return None, str(exc)

    latest = {
        "ts": sample.ts.isoformat(),
        "vehicle_connected": int(sample.vehicle_connected),
        "charging": int(sample.charging),
        "session_s": sample.session_s,
        "voltage_v": sample.voltage_v,
        "current_a": sample.current_a,
        "power_kw": sample.power_kw,
        "lifetime_kwh": sample.lifetime_kwh,
    }
    latest["ts_display"] = _format_dt(latest.get("ts"))
    latest["session_hm"] = _format_duration_hm(latest.get("session_s"))
    return latest, None


def _estimate_charge_for_session(session: dict, plan_code: str) -> dict:
    start_dt = _parse_iso(session.get("started_at"))
    if start_dt is None:
        return {"price": 0.0, "breakdown": {}}
    end_dt = _parse_iso(session.get("ended_at")) or datetime.now(tz=timezone.utc)

    energy_kwh = float(session.get("energy_kwh_est") or 0.0)
    charge = estimate_session_charge(
        start_local=start_dt.astimezone(APP_TZ),
        end_local=end_dt.astimezone(APP_TZ),
        energy_kwh=energy_kwh,
        plan_code=plan_code,
    )
    return {
        "price": charge.total_price_usd,
        "breakdown": charge.breakdown,
    }


def _breakdown_lines(breakdown: dict[str, dict[str, float]]) -> list[str]:
    lines: list[str] = []
    for period in PERIOD_ORDER:
        b = breakdown.get(period, {})
        kwh = float(b.get("kwh", 0.0))
        cost = float(b.get("cost_usd", 0.0))
        if kwh <= 0 and cost <= 0:
            continue
        label = PERIOD_LABELS.get(period, period)
        lines.append(f"{label}: {kwh:.2f} kWh, ${cost:.2f}")
    return lines


def _period_bounds_utc(start_date_str: Optional[str], end_date_str: Optional[str]) -> tuple[Optional[datetime], Optional[datetime], str, str]:
    start_date = _parse_date(start_date_str)
    end_date = _parse_date(end_date_str)

    if start_date and end_date and end_date < start_date:
        start_date, end_date = end_date, start_date

    start_utc = None
    end_utc = None
    if start_date:
        start_local = datetime.combine(start_date, time.min, tzinfo=APP_TZ)
        start_utc = start_local.astimezone(timezone.utc)
    if end_date:
        end_local_next = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=APP_TZ)
        end_utc = end_local_next.astimezone(timezone.utc)

    return start_utc, end_utc, start_date.isoformat() if start_date else "", end_date.isoformat() if end_date else ""


def _overlap_seconds(
    session_start: datetime,
    session_end: datetime,
    filter_start: Optional[datetime],
    filter_end: Optional[datetime],
) -> int:
    a = session_start if filter_start is None else max(session_start, filter_start)
    b = session_end if filter_end is None else min(session_end, filter_end)
    return max(0, int((b - a).total_seconds()))


def _apply_filters_and_pricing(
    *,
    selected_plan: str,
    selected_vehicle: str,
    start_date_str: Optional[str],
    end_date_str: Optional[str],
) -> tuple[list[dict], dict, str, str]:
    sessions = repository.list_sessions(limit=300)
    start_utc, end_utc, normalized_start, normalized_end = _period_bounds_utc(start_date_str, end_date_str)

    filtered: list[dict] = []
    total_kwh = 0.0
    total_price = 0.0

    for session in sessions:
        if selected_vehicle and (session.get("vehicle_label") or "").strip() != selected_vehicle:
            continue

        start_dt = _parse_iso(session.get("started_at"))
        if start_dt is None:
            continue
        end_dt = _parse_iso(session.get("ended_at")) or datetime.now(tz=timezone.utc)

        overlap_s = _overlap_seconds(start_dt, end_dt, start_utc, end_utc)
        if overlap_s <= 0:
            continue

        full_duration_s = max(1, int((end_dt - start_dt).total_seconds()))
        overlap_ratio = min(1.0, max(0.0, overlap_s / full_duration_s))

        full_energy = float(session.get("energy_kwh_est") or 0.0)
        filtered_energy = round(full_energy * overlap_ratio, 3)

        charge = _estimate_charge_for_session(session, selected_plan)
        full_price = float(charge["price"])
        filtered_price = round(full_price * overlap_ratio, 2)

        scaled_breakdown = {}
        for period in PERIOD_ORDER:
            item = charge["breakdown"].get(period, {"kwh": 0.0, "cost_usd": 0.0})
            scaled_breakdown[period] = {
                "kwh": round(float(item.get("kwh", 0.0)) * overlap_ratio, 4),
                "cost_usd": round(float(item.get("cost_usd", 0.0)) * overlap_ratio, 2),
            }

        repository.update_session_price(
            int(session["id"]),
            filtered_price,
            selected_plan,
            json.dumps(scaled_breakdown),
        )

        session["started_at_display"] = _format_dt(session.get("started_at"))
        session["ended_at_display"] = _format_dt(session.get("ended_at"))
        session["duration_hm"] = _format_duration_hm(session.get("duration_s"))
        session["energy_kwh_est"] = filtered_energy
        session["price_usd"] = filtered_price
        session["price_display"] = f"${filtered_price:.2f}"
        session["breakdown_lines"] = _breakdown_lines(scaled_breakdown)

        total_kwh += filtered_energy
        total_price += filtered_price
        filtered.append(session)

    summary = {
        "sessions_count": len(filtered),
        "total_kwh": round(total_kwh, 3),
        "total_price": round(total_price, 2),
    }
    return filtered, summary, normalized_start, normalized_end


@app.on_event("startup")
def on_startup() -> None:
    global poller
    init_db()
    poller = Poller(
        repository=repository,
        client=client,
        poll_interval_seconds=settings.poll_interval_seconds,
    )
    poller.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    if poller is not None:
        poller.stop()


@app.get("/")
def home(
    request: Request,
    plan: Optional[str] = None,
    vehicle: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    selected_plan = valid_plan_or_default(plan, settings.default_rate_plan)
    selected_vehicle = (vehicle or "").strip()
    sessions, summary, normalized_start, normalized_end = _apply_filters_and_pricing(
        selected_plan=selected_plan,
        selected_vehicle=selected_vehicle,
        start_date_str=start_date,
        end_date_str=end_date,
    )
    vehicle_options = set(repository.list_vehicle_labels())
    for session in sessions:
        label = (session.get("vehicle_label") or "").strip()
        if label:
            vehicle_options.add(label)
    vehicle_options_sorted = sorted(vehicle_options, key=str.lower)

    latest, telemetry_error = _read_live_telemetry()

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "sessions": sessions,
            "latest": latest,
            "telemetry_error": telemetry_error,
            "base_url": settings.tesla_base_url,
            "poll_interval": settings.poll_interval_seconds,
            "timezone_name": APP_TZ_NAME,
            "plan_options": list(PLANS.values()),
            "selected_plan": selected_plan,
            "selected_plan_info": PLANS[selected_plan],
            "vehicle_options": vehicle_options_sorted,
            "selected_vehicle": selected_vehicle,
            "start_date": normalized_start,
            "end_date": normalized_end,
            "summary": summary,
        },
    )


@app.get("/export.csv")
def export_csv(
    plan: Optional[str] = None,
    vehicle: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    selected_plan = valid_plan_or_default(plan, settings.default_rate_plan)
    selected_vehicle = (vehicle or "").strip()

    sessions, summary, normalized_start, normalized_end = _apply_filters_and_pricing(
        selected_plan=selected_plan,
        selected_vehicle=selected_vehicle,
        start_date_str=start_date,
        end_date_str=end_date,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id",
        "start_local",
        "end_local",
        "duration_hm",
        "vehicle",
        "energy_kwh",
        "price_usd",
        "plan",
        "rate_breakdown",
    ])

    for s in sessions:
        writer.writerow([
            s.get("id"),
            s.get("started_at_display"),
            s.get("ended_at_display") if s.get("ended_at") else "Active",
            s.get("duration_hm"),
            s.get("vehicle_label") or "",
            s.get("energy_kwh_est"),
            s.get("price_usd"),
            selected_plan,
            " | ".join(s.get("breakdown_lines") or []),
        ])

    writer.writerow([])
    writer.writerow(["sessions_count", summary["sessions_count"]])
    writer.writerow(["total_kwh", summary["total_kwh"]])
    writer.writerow(["total_price_usd", summary["total_price"]])
    writer.writerow(["vehicle_filter", selected_vehicle or "ALL"])
    writer.writerow(["start_date", normalized_start or ""])
    writer.writerow(["end_date", normalized_end or ""])
    writer.writerow(["timezone", APP_TZ_NAME])

    csv_bytes = output.getvalue().encode("utf-8")
    filename = f"sessions_{selected_plan}_{normalized_start or 'any'}_{normalized_end or 'any'}.csv"
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/sessions/{session_id}/vehicle")
def set_vehicle(
    session_id: int,
    vehicle_label: str = Form(default=""),
    plan: str = Form(default="EV2-A"),
    vehicle: str = Form(default=""),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
):
    repository.update_vehicle_label(session_id, vehicle_label)
    query = f"/?plan={quote_plus(plan)}"
    if vehicle.strip():
        query += f"&vehicle={quote_plus(vehicle.strip())}"
    if start_date.strip():
        query += f"&start_date={quote_plus(start_date.strip())}"
    if end_date.strip():
        query += f"&end_date={quote_plus(end_date.strip())}"
    return RedirectResponse(url=query, status_code=303)


@app.post("/vehicles/add")
def add_vehicle(
    vehicle_label: str = Form(default=""),
    plan: str = Form(default="EV2-A"),
    vehicle: str = Form(default=""),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
):
    repository.add_vehicle_label(vehicle_label)
    query = f"/?plan={quote_plus(plan)}"
    selected_vehicle = vehicle.strip()
    if selected_vehicle:
        query += f"&vehicle={quote_plus(selected_vehicle)}"
    if start_date.strip():
        query += f"&start_date={quote_plus(start_date.strip())}"
    if end_date.strip():
        query += f"&end_date={quote_plus(end_date.strip())}"
    return RedirectResponse(url=query, status_code=303)


@app.post("/vehicles/delete")
def delete_vehicle(
    vehicle_label: str = Form(default=""),
    plan: str = Form(default="EV2-A"),
    vehicle: str = Form(default=""),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
):
    remove_label = vehicle_label.strip()
    repository.delete_vehicle_label(remove_label)
    selected_vehicle = vehicle.strip()
    if selected_vehicle == remove_label:
        selected_vehicle = ""

    query = f"/?plan={quote_plus(plan)}"
    if selected_vehicle:
        query += f"&vehicle={quote_plus(selected_vehicle)}"
    if start_date.strip():
        query += f"&start_date={quote_plus(start_date.strip())}"
    if end_date.strip():
        query += f"&end_date={quote_plus(end_date.strip())}"
    return RedirectResponse(url=query, status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}
