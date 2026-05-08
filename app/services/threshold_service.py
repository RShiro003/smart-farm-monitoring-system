import os
import sqlite3
import threading
from datetime import datetime


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.environ.get(
    "SMART_FARM_DB_FILE",
    os.path.join(_BASE_DIR, "data", "smart_farm.db"),
)

THRESHOLD_FIELDS = [
    "temperature_min",
    "temperature_max",
    "humidity_min",
    "humidity_max",
    "soil_moisture_min",
    "soil_moisture_max",
    "light_min",
    "light_max",
]

DEFAULT_THRESHOLDS = {
    "temperature_min": 18,
    "temperature_max": 25,
    "humidity_min": 60,
    "humidity_max": 80,
    "soil_moisture_min": 40,
    "soil_moisture_max": 70,
    "light_min": 0,
    "light_max": 100,
}

_lock = threading.Lock()


def _connect():
    data_dir = os.path.dirname(DB_FILE)
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS threshold_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL UNIQUE,
            temperature_min REAL NOT NULL DEFAULT 18,
            temperature_max REAL NOT NULL DEFAULT 25,
            humidity_min REAL NOT NULL DEFAULT 60,
            humidity_max REAL NOT NULL DEFAULT 80,
            soil_moisture_min REAL NOT NULL DEFAULT 40,
            soil_moisture_max REAL NOT NULL DEFAULT 70,
            light_min REAL NOT NULL DEFAULT 0,
            light_max REAL NOT NULL DEFAULT 100,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _now_string():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_number(value):
    value = float(value)
    if value.is_integer():
        return int(value)
    return value


def _row_to_thresholds(row):
    if row is None:
        return None

    data = {"device_id": row["device_id"]}
    for field in THRESHOLD_FIELDS:
        data[field] = _format_number(row[field])
    return data


def default_thresholds(device_id):
    data = {"device_id": device_id}
    data.update(DEFAULT_THRESHOLDS)
    return data


def _insert_thresholds(conn, settings):
    now = _now_string()
    conn.execute(
        """
        INSERT INTO threshold_settings (
            device_id,
            temperature_min,
            temperature_max,
            humidity_min,
            humidity_max,
            soil_moisture_min,
            soil_moisture_max,
            light_min,
            light_max,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            settings["device_id"],
            settings["temperature_min"],
            settings["temperature_max"],
            settings["humidity_min"],
            settings["humidity_max"],
            settings["soil_moisture_min"],
            settings["soil_moisture_max"],
            settings["light_min"],
            settings["light_max"],
            now,
        ),
    )


def get_or_create_thresholds(device_id):
    with _lock:
        with _connect() as conn:
            _ensure_table(conn)
            row = conn.execute(
                """
                SELECT *
                FROM threshold_settings
                WHERE device_id = ?
                """,
                (device_id,),
            ).fetchone()
            if row is not None:
                return _row_to_thresholds(row)

            settings = default_thresholds(device_id)
            _insert_thresholds(conn, settings)
            conn.commit()
            return settings


def upsert_thresholds(device_id, values):
    settings = {"device_id": device_id}
    for field in THRESHOLD_FIELDS:
        settings[field] = values[field]

    with _lock:
        with _connect() as conn:
            _ensure_table(conn)
            exists = conn.execute(
                """
                SELECT id
                FROM threshold_settings
                WHERE device_id = ?
                """,
                (device_id,),
            ).fetchone()

            if exists:
                conn.execute(
                    """
                    UPDATE threshold_settings
                    SET temperature_min = ?,
                        temperature_max = ?,
                        humidity_min = ?,
                        humidity_max = ?,
                        soil_moisture_min = ?,
                        soil_moisture_max = ?,
                        light_min = ?,
                        light_max = ?,
                        updated_at = ?
                    WHERE device_id = ?
                    """,
                    (
                        settings["temperature_min"],
                        settings["temperature_max"],
                        settings["humidity_min"],
                        settings["humidity_max"],
                        settings["soil_moisture_min"],
                        settings["soil_moisture_max"],
                        settings["light_min"],
                        settings["light_max"],
                        _now_string(),
                        device_id,
                    ),
                )
            else:
                _insert_thresholds(conn, settings)

            conn.commit()
            row = conn.execute(
                """
                SELECT *
                FROM threshold_settings
                WHERE device_id = ?
                """,
                (device_id,),
            ).fetchone()
            return _row_to_thresholds(row)
