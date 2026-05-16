import json
import os
import sqlite3
from datetime import datetime


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE      = os.path.join(_BASE_DIR, "data", "sensor_data.db")
_JSON_LEGACY = os.path.join(_BASE_DIR, "data", "sensor_data.json")
LEGACY_DEVICE_ID = "legacy"
ALL_DEVICES_VALUE = "all"

_COLUMNS = (
    "device_id", "temperature", "humidity", "soil_moisture", "light",
    "light_digital", "soil_digital", "soil_raw",
    "timestamp", "server_received_at", "time",
)


# ── Normalisation ──────────────────────────────────────────────────────────────

def normalize_device_id(device_id, default=LEGACY_DEVICE_ID):
    if isinstance(device_id, str):
        value = device_id.strip()
        if value:
            return value
    return default


def normalize_device_filter(device_id):
    if not isinstance(device_id, str):
        return None
    value = device_id.strip()
    if not value or value.lower() == ALL_DEVICES_VALUE:
        return None
    return value


def normalize_sensor_record(record):
    normalized = dict(record)
    normalized["device_id"] = normalize_device_id(normalized.get("device_id"))
    return normalized


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _connect():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _insert(conn, record):
    cols = [c for c in _COLUMNS if c in record]
    conn.execute(
        f"INSERT INTO sensor_data ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        [record[c] for c in cols],
    )


def _row_to_dict(row):
    return {k: row[k] for k in row.keys() if row[k] is not None and k != "id"}


def _migrate_from_json(conn):
    if not os.path.exists(_JSON_LEGACY):
        return
    try:
        with open(_JSON_LEGACY, "r", encoding="utf-8") as f:
            records = json.load(f)
        if not isinstance(records, list):
            return
        for rec in records:
            if isinstance(rec, dict):
                _insert(conn, normalize_sensor_record(rec))
        conn.commit()
        print(f"[DB] Migrated {len(records)} records from sensor_data.json")
    except (OSError, json.JSONDecodeError, sqlite3.Error) as e:
        conn.rollback()
        print(f"[DB] Migration failed: {e}")


def _init_db():
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sensor_data (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id          TEXT    NOT NULL DEFAULT 'legacy',
                temperature        REAL,
                humidity           REAL,
                soil_moisture      REAL,
                light              REAL,
                light_digital      INTEGER,
                soil_digital       INTEGER,
                soil_raw           INTEGER,
                timestamp          TEXT,
                server_received_at TEXT,
                time               TEXT
            )
        """)
        conn.commit()
        if conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0] == 0:
            _migrate_from_json(conn)
    finally:
        conn.close()


_init_db()


# ── Public API ─────────────────────────────────────────────────────────────────

def load_sensor_data():
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM sensor_data ORDER BY id ASC"
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def append_sensor_data(new_record):
    record = normalize_sensor_record(new_record)
    conn = _connect()
    try:
        _insert(conn, record)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def filter_sensor_data(data, device_id=None):
    selected = normalize_device_filter(device_id)
    if selected is None:
        return list(data)
    return [
        row for row in data
        if normalize_device_id(row.get("device_id")) == selected
    ]


def list_device_ids(data):
    return sorted({
        normalize_device_id(row.get("device_id"))
        for row in data
        if isinstance(row, dict)
    })


def latest_sensor_record(data):
    return data[-1] if data else None


def parse_record_time(row):
    ts = row.get("server_received_at") or row.get("time") or row.get("timestamp")
    if not ts or ts in {"time_not_set", "time_sync_failed"}:
        return None

    if isinstance(ts, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(ts)
            if parsed.tzinfo is not None:
                return parsed.astimezone().replace(tzinfo=None)
            return parsed
        except ValueError:
            return None

    return None


def sensor_data_mtime():
    conn = _connect()
    try:
        row = conn.execute("SELECT MAX(id) FROM sensor_data").fetchone()
        return row[0] if row[0] is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
