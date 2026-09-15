"""사용자가 직접 수행한 재배 작업을 기록한다.

이 서비스는 장치를 제어하지 않는다. 급수·환기·비료·점검 같은 현장 작업을
센서 그래프와 함께 해석할 수 있도록 시각과 메모만 SQLite에 보존한다.
"""
import sqlite3
from datetime import datetime

try:
    from services import sensor_service
except ModuleNotFoundError:
    from app.services import sensor_service


DB_FILE = None  # sensor_service.DB_FILE을 따라 테스트와 배포 DB 경로를 공유한다.
WORK_TYPES = {
    "watering": "급수",
    "ventilation": "환기",
    "fertilizing": "비료 투입",
    "inspection": "작물 점검",
    "sensor_maintenance": "센서 점검",
    "other": "기타",
}


def _connect():
    return sensor_service._connect()


def ensure_tables(conn=None):
    owns_connection = conn is None
    conn = conn or _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS work_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id   TEXT NOT NULL,
                work_type   TEXT NOT NULL,
                note        TEXT,
                occurred_at TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_log_device_occurred "
            "ON work_log(device_id, occurred_at)"
        )
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()


def _row_to_dict(row):
    return {
        "id": int(row["id"]),
        "device_id": row["device_id"],
        "work_type": row["work_type"],
        "work_type_label": WORK_TYPES.get(row["work_type"], row["work_type"]),
        "note": row["note"] or "",
        "occurred_at": row["occurred_at"],
        "created_at": row["created_at"],
    }


def create_work_log(device_id, work_type, note="", occurred_at=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    occurred_at = occurred_at or now
    conn = _connect()
    try:
        ensure_tables(conn)
        cursor = conn.execute(
            """
            INSERT INTO work_log (device_id, work_type, note, occurred_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (device_id, work_type, note or None, occurred_at, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM work_log WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_dict(row)
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_work_logs(device_id=None, start_at=None, end_at=None, limit=200):
    clauses = []
    params = []
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if start_at:
        clauses.append("occurred_at >= ?")
        params.append(start_at)
    if end_at:
        clauses.append("occurred_at <= ?")
        params.append(end_at)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    safe_limit = max(1, min(int(limit), 1000))

    conn = _connect()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            f"SELECT * FROM work_log{where} ORDER BY occurred_at DESC, id DESC LIMIT ?",
            params + [safe_limit],
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def delete_work_log(entry_id):
    conn = _connect()
    try:
        ensure_tables(conn)
        deleted = conn.execute("DELETE FROM work_log WHERE id = ?", (entry_id,)).rowcount
        conn.commit()
        return deleted > 0
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()
