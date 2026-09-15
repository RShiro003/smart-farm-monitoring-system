"""장치 메타데이터(별칭, 설치 위치, 메모) 관리.

지금까지 장치는 "센서 데이터에 device_id가 한 번이라도 등장했는가"로만 존재했다.
그래서 아직 데이터를 보내지 않은 새 보드는 대시보드에 나타나지 않았고,
esp32_01 같은 식별자만 보여 어느 온실의 어느 자리인지 알 수 없었다.

이 서비스는 장치 식별자에 사람이 읽을 이름을 붙이고, 데이터가 오기 전에도
미리 등록해 둘 수 있게 한다. 센서 원본 데이터는 건드리지 않는다.
"""
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 장치 메타데이터는 센서 시계열이 아니라 설정값이므로 임계값/작물과 같은 설정 DB에 둔다.
DB_FILE = os.environ.get(
    "SMART_FARM_DB_FILE",
    os.path.join(_BASE_DIR, "data", "smart_farm.db"),
)

# 사용자가 입력하는 텍스트 필드의 최대 길이다.
# SQLite는 길이를 강제하지 않으므로 서비스 계층에서 잘라 저장한다.
MAX_LABEL_LENGTH = 60
MAX_LOCATION_LENGTH = 60
MAX_NOTE_LENGTH = 500

_lock = threading.Lock()


def _connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_tables(conn):
    # device_id를 기본키로 두어 같은 장치가 중복 등록되지 않게 한다.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            device_id  TEXT PRIMARY KEY,
            label      TEXT,
            location   TEXT,
            note       TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def initialize_database():
    with _lock:
        with closing(_connect()) as conn:
            _ensure_tables(conn)


initialize_database()


def _now_string():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_device_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _clean_text(value, max_length):
    # 빈 문자열과 공백만 있는 값은 "설정하지 않음"과 같은 의미이므로 None으로 통일한다.
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:max_length]


def _row_to_dict(row):
    if row is None:
        return None
    return {
        "device_id": row["device_id"],
        "label": row["label"],
        "location": row["location"],
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def display_name(device_id, label=None):
    """화면과 알림 메시지에 쓸 이름을 만든다.

    별칭이 있으면 "북쪽 온실 (esp32_01)"처럼 식별자를 함께 보여준다.
    식별자를 숨기면 여러 보드를 다룰 때 어느 기기인지 특정할 수 없기 때문이다.
    """
    if not label:
        return device_id
    return f"{label} ({device_id})"


def get_device(device_id):
    device_id = normalize_device_id(device_id)
    if device_id is None:
        return None
    with _lock:
        with closing(_connect()) as conn:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            return _row_to_dict(row)


def list_devices(seen_device_ids=()):
    """등록된 장치와 센서 데이터에만 존재하는 장치를 합쳐 돌려준다.

    등록 절차를 강제하면 기존 설치 환경의 장치가 갑자기 목록에서 사라진다.
    그래서 데이터로만 알려진 장치도 registered=False 상태로 함께 보여준다.
    """
    with _lock:
        with closing(_connect()) as conn:
            _ensure_tables(conn)
            rows = conn.execute(
                "SELECT * FROM devices ORDER BY device_id ASC"
            ).fetchall()
            registered = {row["device_id"]: _row_to_dict(row) for row in rows}

    result = []
    for device_id, data in registered.items():
        entry = dict(data)
        entry["registered"] = True
        # 센서 데이터가 한 건도 없으면 아직 한 번도 통신하지 않은 장치다.
        entry["has_data"] = device_id in set(seen_device_ids)
        entry["display_name"] = display_name(device_id, data.get("label"))
        result.append(entry)

    for device_id in seen_device_ids:
        if device_id in registered:
            continue
        result.append({
            "device_id": device_id,
            "label": None,
            "location": None,
            "note": None,
            "created_at": None,
            "updated_at": None,
            "registered": False,
            "has_data": True,
            "display_name": device_id,
        })

    result.sort(key=lambda entry: entry["device_id"])
    return result


def device_labels(seen_device_ids=()):
    """device_id -> 표시 이름 매핑이다.

    대시보드 드롭다운과 CSV 내보내기가 같은 이름을 쓰도록 한 곳에서 만든다.
    """
    return {
        entry["device_id"]: entry["display_name"]
        for entry in list_devices(seen_device_ids)
    }


def upsert_device(device_id, label=None, location=None, note=None):
    device_id = normalize_device_id(device_id)
    if device_id is None:
        raise ValueError("device_id must be a non-empty string")

    label = _clean_text(label, MAX_LABEL_LENGTH)
    location = _clean_text(location, MAX_LOCATION_LENGTH)
    note = _clean_text(note, MAX_NOTE_LENGTH)
    now = _now_string()

    with _lock:
        with closing(_connect()) as conn:
            _ensure_tables(conn)
            existing = conn.execute(
                "SELECT device_id FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO devices (
                        device_id, label, location, note, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (device_id, label, location, note, now, now),
                )
            else:
                # 등록 시각은 유지하고 수정 시각만 갱신한다.
                conn.execute(
                    """
                    UPDATE devices
                    SET label = ?, location = ?, note = ?, updated_at = ?
                    WHERE device_id = ?
                    """,
                    (label, location, note, now, device_id),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            return _row_to_dict(row)


def delete_device(device_id):
    """장치 등록 정보만 삭제한다.

    센서 기록과 알림 이력은 그대로 남긴다. 메타데이터를 지웠다고 과거 측정값을
    함께 지우면 되돌릴 수 없는 데이터 손실이 되기 때문이다.
    삭제 후에도 해당 device_id의 데이터가 있으면 목록에 미등록 장치로 다시 나타난다.
    """
    device_id = normalize_device_id(device_id)
    if device_id is None:
        return False

    with _lock:
        with closing(_connect()) as conn:
            _ensure_tables(conn)
            deleted = conn.execute(
                "DELETE FROM devices WHERE device_id = ?", (device_id,)
            ).rowcount
            conn.commit()
            return deleted > 0
