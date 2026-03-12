from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .repository import Repository
from .tesla_client import TeslaWallConnectorClient

logger = logging.getLogger(__name__)


@dataclass
class TelemetrySample:
    ts: datetime
    vehicle_connected: bool
    charging: bool
    session_s: Optional[int]
    session_energy_wh: Optional[float]
    voltage_v: Optional[float]
    current_a: Optional[float]
    power_kw: Optional[float]
    meter_wh: Optional[float]


class Poller:
    def __init__(
        self,
        repository: Repository,
        client: TeslaWallConnectorClient,
        poll_interval_seconds: int,
    ) -> None:
        self.repository = repository
        self.client = client
        self.poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # In-memory active session state; one DB write only when session closes.
        self._open_session_started_at: Optional[datetime] = None
        self._last_nonzero_session_energy_wh: Optional[float] = None
        self._pending_energy_kwh: float = 0.0
        self._pending_max_power_kw: float = 0.0
        self._last_sample_ts: Optional[datetime] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Poller started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Poller stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                logger.exception("Polling failed: %s", exc)
            self._stop_event.wait(self.poll_interval_seconds)

    @staticmethod
    def _is_active(sample: TelemetrySample) -> bool:
        if sample.session_s is not None:
            return sample.session_s > 0
        return bool(sample.charging or (sample.session_energy_wh is not None and sample.session_energy_wh > 0))

    def poll_once(self) -> None:
        payload = self.client.read_sample()
        sample = self._to_sample(payload)

        active = self._is_active(sample)

        if active and self._open_session_started_at is None:
            self._open_session_started_at = sample.ts
            self._last_nonzero_session_energy_wh = None
            self._pending_energy_kwh = 0.0
            self._pending_max_power_kw = sample.power_kw or 0.0

        if active and self._open_session_started_at is not None:
            if sample.session_energy_wh is not None and sample.session_energy_wh > 0:
                self._last_nonzero_session_energy_wh = sample.session_energy_wh

            if self._last_sample_ts and sample.power_kw is not None:
                dt_hours = max(0.0, (sample.ts - self._last_sample_ts).total_seconds() / 3600.0)
                self._pending_energy_kwh += max(0.0, sample.power_kw * dt_hours)
            self._pending_max_power_kw = max(self._pending_max_power_kw, sample.power_kw or 0.0)

        # End session when all activity signals are off.
        if self._open_session_started_at is not None and not active:
            duration_s = max(0, int((sample.ts - self._open_session_started_at).total_seconds()))

            # Prefer charger-reported session energy; fallback to integrated power.
            energy_kwh = self._pending_energy_kwh
            if self._last_nonzero_session_energy_wh is not None:
                energy_kwh = max(0.0, self._last_nonzero_session_energy_wh / 1000.0)

            self.repository.insert_closed_session(
                started_at=self._open_session_started_at,
                ended_at=sample.ts,
                duration_s=duration_s,
                energy_kwh=energy_kwh,
                max_power_kw=self._pending_max_power_kw,
            )

            self._open_session_started_at = None
            self._last_nonzero_session_energy_wh = None
            self._pending_energy_kwh = 0.0
            self._pending_max_power_kw = 0.0

        self._last_sample_ts = sample.ts

    @staticmethod
    def _to_sample(payload: dict[str, Any]) -> TelemetrySample:
        vitals = payload.get("vitals", {})

        def f(*keys: str) -> Optional[float]:
            for key in keys:
                if key in vitals and vitals[key] is not None:
                    try:
                        return float(vitals[key])
                    except (TypeError, ValueError):
                        return None
            return None

        vehicle_connected = bool(vitals.get("vehicle_connected", False))
        charging = bool(vitals.get("contactor_closed", False))

        session_s = vitals.get("session_s")
        try:
            session_s = int(session_s) if session_s is not None else None
        except (TypeError, ValueError):
            session_s = None

        session_energy_wh = f("session_energy_wh", "session_energy")

        voltage_v = f("grid_v", "voltage")
        current_a = f("vehicle_current_a", "currentA_a", "current")
        power_kw = f("vehicle_power_kw", "powerW")
        if power_kw is not None and power_kw > 1000:
            power_kw = power_kw / 1000.0
        if power_kw is None and voltage_v is not None and current_a is not None:
            power_kw = (voltage_v * current_a) / 1000.0

        meter_wh = None
        meter_candidates_wh = (
            "energy_wh",
            "lifetime_wh",
            "grid_energy_wh",
            "meter_energy_wh",
            "lifetime_energy_wh",
            "total_energy_wh",
            "total_lifetime_wh",
        )
        meter_candidates_kwh = (
            "energy_kwh",
            "lifetime_kwh",
            "grid_energy_kwh",
            "meter_energy_kwh",
            "lifetime_energy_kwh",
            "total_energy_kwh",
            "total_lifetime_kwh",
        )

        for key in meter_candidates_wh:
            raw = vitals.get(key)
            if raw is None:
                continue
            try:
                meter_wh = float(raw)
                break
            except (TypeError, ValueError):
                continue

        if meter_wh is None:
            for key in meter_candidates_kwh:
                raw = vitals.get(key)
                if raw is None:
                    continue
                try:
                    meter_wh = float(raw) * 1000.0
                    break
                except (TypeError, ValueError):
                    continue

        return TelemetrySample(
            ts=datetime.now(tz=timezone.utc),
            vehicle_connected=vehicle_connected,
            charging=charging,
            session_s=session_s,
            session_energy_wh=session_energy_wh,
            voltage_v=voltage_v,
            current_a=current_a,
            power_kw=power_kw,
            meter_wh=meter_wh,
        )
