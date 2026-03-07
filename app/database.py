import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import settings


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column_def: str) -> None:
    col_name = column_def.split()[0]
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {row[1] for row in cols}
    if col_name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vehicles (
                label TEXT PRIMARY KEY
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_s INTEGER DEFAULT 0,
                energy_kwh_est REAL DEFAULT 0,
                max_power_kw REAL DEFAULT 0,
                start_meter_wh REAL,
                end_meter_wh REAL,
                vehicle_label TEXT,
                price_usd REAL DEFAULT 0,
                price_plan TEXT,
                price_breakdown_json TEXT
            )
            """
        )

        _add_column_if_missing(conn, "sessions", "price_usd REAL DEFAULT 0")
        _add_column_if_missing(conn, "sessions", "price_plan TEXT")
        _add_column_if_missing(conn, "sessions", "price_breakdown_json TEXT")
        _add_column_if_missing(conn, "sessions", "start_meter_wh REAL")
        _add_column_if_missing(conn, "sessions", "end_meter_wh REAL")

        conn.execute("DROP TABLE IF EXISTS telemetry")
        conn.execute(
            """
            INSERT OR IGNORE INTO vehicles (label)
            SELECT DISTINCT TRIM(vehicle_label) AS label
            FROM sessions
            WHERE vehicle_label IS NOT NULL AND TRIM(vehicle_label) <> ''
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vehicles_label ON vehicles(label COLLATE NOCASE)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at DESC)"
        )
