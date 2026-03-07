from __future__ import annotations

from datetime import datetime
from typing import Optional

from .database import get_conn


class Repository:
    def insert_closed_session(
        self,
        *,
        started_at: datetime,
        ended_at: datetime,
        duration_s: int,
        energy_kwh: float,
        max_power_kw: float,
        start_meter_wh: Optional[float] = None,
        end_meter_wh: Optional[float] = None,
    ) -> int:
        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (
                    started_at, ended_at, duration_s, energy_kwh_est, max_power_kw,
                    start_meter_wh, end_meter_wh
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at.isoformat(),
                    ended_at.isoformat(),
                    max(0, int(duration_s)),
                    max(0.0, float(energy_kwh)),
                    max(0.0, float(max_power_kw)),
                    float(start_meter_wh) if start_meter_wh is not None else None,
                    float(end_meter_wh) if end_meter_wh is not None else None,
                ),
            )
            return int(cur.lastrowid)

    def list_sessions(self, limit: int = 200) -> list[dict]:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, started_at, ended_at, duration_s,
                       ROUND(energy_kwh_est, 3) AS energy_kwh_est,
                       ROUND(max_power_kw, 3) AS max_power_kw,
                       vehicle_label,
                       ROUND(COALESCE(price_usd, 0), 2) AS price_usd,
                       price_plan,
                       price_breakdown_json
                FROM sessions
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_vehicle_labels(self) -> list[str]:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT label
                FROM vehicles
                ORDER BY label COLLATE NOCASE
                """
            ).fetchall()
        return [str(r["label"]) for r in rows]

    def add_vehicle_label(self, vehicle_label: str) -> None:
        label = vehicle_label.strip()
        if not label:
            return
        with get_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO vehicles (label) VALUES (?)", (label,))

    def delete_vehicle_label(self, vehicle_label: str) -> None:
        label = vehicle_label.strip()
        if not label:
            return
        with get_conn() as conn:
            conn.execute("DELETE FROM vehicles WHERE label = ?", (label,))

    def update_vehicle_label(self, session_id: int, vehicle_label: str) -> None:
        label = vehicle_label.strip()
        with get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET vehicle_label = ? WHERE id = ?",
                (label or None, session_id),
            )
            if label:
                conn.execute("INSERT OR IGNORE INTO vehicles (label) VALUES (?)", (label,))

    def update_session_price(
        self,
        session_id: int,
        price_usd: float,
        plan_code: str,
        price_breakdown_json: str,
    ) -> None:
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET price_usd = ?,
                    price_plan = ?,
                    price_breakdown_json = ?
                WHERE id = ?
                """,
                (round(max(0.0, float(price_usd)), 2), plan_code, price_breakdown_json, session_id),
            )
