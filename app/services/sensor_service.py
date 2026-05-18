import json
import os
import sqlite3
from datetime import datetime


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE      = os.path.join(_BASE_DIR, "data", "sensor_data.db")
# sensor_data.json은 예전 JSON 저장 방식에서 넘어온 데이터를 한 번 가져오기 위한 레거시 파일이다.
# 현재 런타임에서 ESP32가 보낸 새 센서값은 JSON 파일이 아니라 위 SQLite DB_FILE에 INSERT된다.
_JSON_LEGACY = os.path.join(_BASE_DIR, "data", "sensor_data.json")
LEGACY_DEVICE_ID = "legacy"
ALL_DEVICES_VALUE = "all"

# sensor_data 테이블에 실제로 저장되는 컬럼 목록이다.
# /api/sensor POST의 JSON body에 디버깅용 키가 추가되어도, 이 목록에 없는 값은 DB에 저장하지 않는다.
# 이렇게 해야 DB 스키마가 예측 가능하게 유지되고 대시보드 SELECT/그래프 로직도 고정 컬럼만 다루면 된다.
_COLUMNS = (
    "device_id", "temperature", "humidity", "soil_moisture", "light",
    "light_digital", "soil_digital", "soil_raw",
    "timestamp", "server_received_at", "time",
)


# ── Normalisation ──────────────────────────────────────────────────────────────

def normalize_device_id(device_id, default=LEGACY_DEVICE_ID):
    # 과거 JSON 데이터에는 device_id가 없을 수 있다.
    # 대시보드와 필터 로직은 항상 장치명이 있다고 가정하므로, 비어 있는 값은 legacy로 통일한다.
    if isinstance(device_id, str):
        value = device_id.strip()
        if value:
            return value
    return default


def normalize_device_filter(device_id):
    # GET /api/sensor?device_id=... 와 대시보드 장치 선택에서 공통으로 쓰는 필터 정규화다.
    # 값이 없거나 "all"이면 전체 장치를 조회해야 하므로 None을 반환한다.
    if not isinstance(device_id, str):
        return None
    value = device_id.strip()
    if not value or value.lower() == ALL_DEVICES_VALUE:
        return None
    return value


def normalize_sensor_record(record):
    # INSERT 직전에도 device_id를 한 번 더 보정한다.
    # 라우트 검증을 거치지 않은 내부 마이그레이션 데이터도 같은 규칙으로 저장하기 위함이다.
    normalized = dict(record)
    normalized["device_id"] = normalize_device_id(normalized.get("device_id"))
    return normalized


# ── SQLite helpers ─────────────────────────────────────────────────────────────

def _connect():
    # SQLite 파일이 들어갈 app/data 디렉터리를 보장한다.
    # Raspberry Pi에서 처음 실행하는 경우 DB 파일이 없어도 여기서 디렉터리 생성 후 연결된다.
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    # sqlite3.Row를 쓰면 row["temperature"]처럼 컬럼명으로 접근할 수 있어
    # 나중에 _row_to_dict()에서 API 응답용 dict로 바꾸기 쉽다.
    conn.row_factory = sqlite3.Row
    # WAL 모드는 대시보드 조회와 ESP32 INSERT가 겹칠 때 잠금 충돌을 줄여 준다.
    conn.execute("PRAGMA journal_mode=WAL")
    # 짧은 순간 DB가 잠겨 있어도 바로 실패하지 않고 최대 5초 기다리게 한다.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _insert(conn, record):
    # 들어온 JSON 전체를 통째로 저장하지 않고, _COLUMNS에 정의된 센서 컬럼만 골라 INSERT한다.
    # 예를 들어 extra_debug 같은 키는 API 응답에는 남을 수 있지만 SQLite row에는 들어가지 않는다.
    cols = [c for c in _COLUMNS if c in record]
    conn.execute(
        f"INSERT INTO sensor_data ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        [record[c] for c in cols],
    )


def _row_to_dict(row):
    # DB row를 Flask jsonify가 바로 처리할 수 있는 dict로 바꾼다.
    # id는 내부 정렬용 기본키라 대시보드에 노출하지 않고, NULL 컬럼은 응답에서 생략해 기존 JSON 형태와 맞춘다.
    return {k: row[k] for k in row.keys() if row[k] is not None and k != "id"}


def _migrate_from_json(conn):
    # 예전 버전이 sensor_data.json에 저장하던 데이터를 SQLite로 가져오기 위한 호환 코드다.
    # 새 데이터는 이 함수로 들어오지 않고 append_sensor_data()를 통해 바로 SQLite에 저장된다.
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
    # 모듈 import 시점에 테이블을 준비한다.
    # Flask 서버가 시작된 뒤 첫 ESP32 요청이 오기 전에 스키마가 존재해야 POST가 바로 성공한다.
    # 컬럼 의미:
    # - device_id: 여러 ESP32 노드를 구분하는 장치 ID. 예전 데이터는 legacy로 보정된다.
    # - temperature/humidity/soil_moisture/light: 대시보드 카드와 그래프가 사용하는 대표 센서값.
    # - light_digital/soil_digital/soil_raw: 실제 센서 노드의 디지털/원시 진단값.
    # - timestamp: ESP32가 측정한 시각, server_received_at: Flask 서버가 받은 시각.
    # - time: 오래된 JSON 데이터와의 호환을 위해 유지하는 과거 컬럼.
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
        # 새 DB가 비어 있을 때만 레거시 JSON을 가져온다.
        # 이미 SQLite에 row가 있으면 중복 import를 막기 위해 JSON을 다시 읽지 않는다.
        if conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0] == 0:
            _migrate_from_json(conn)
    finally:
        conn.close()


_init_db()


# ── Public API ─────────────────────────────────────────────────────────────────

def load_sensor_data():
    # 대시보드, 상태 API, /api/sensor GET이 공통으로 사용하는 SELECT 함수다.
    # id ASC는 저장 순서 그대로, 즉 오래된 데이터에서 최신 데이터 순서로 반환한다.
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
    """센서 row 하나를 SQLite에 INSERT한다.

    _COLUMNS에 들어 있는 키만 저장된다. 추가 payload 키는 라우트 응답에는
    포함될 수 있지만 sensor_data.db에는 저장되지 않는다.
    """
    record = normalize_sensor_record(new_record)
    conn = _connect()
    try:
        # INSERT와 commit을 한 함수 안에서 묶어 ESP32 POST 한 건이 DB row 한 건으로 확정되게 한다.
        _insert(conn, record)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def filter_sensor_data(data, device_id=None):
    # 장치 선택 드롭다운과 /api/sensor?device_id=...에서 쓰는 공통 필터다.
    # selected가 None이면 전체 장치를 의미하므로 원본 순서를 유지한 복사본을 반환한다.
    selected = normalize_device_filter(device_id)
    if selected is None:
        return list(data)
    return [
        row for row in data
        if normalize_device_id(row.get("device_id")) == selected
    ]


def list_device_ids(data):
    # 대시보드 상단의 장치 선택 목록을 만들기 위해 저장된 row에서 device_id를 수집한다.
    # legacy 보정을 함께 적용해 과거 데이터도 하나의 선택지로 보이게 한다.
    return sorted({
        normalize_device_id(row.get("device_id"))
        for row in data
        if isinstance(row, dict)
    })


def latest_sensor_record(data):
    # load_sensor_data()가 오래된 순서로 반환하므로 마지막 row가 최신 센서값이다.
    # 현재 센서 카드와 상태 API에서 같은 규칙을 사용한다.
    return data[-1] if data else None


def parse_record_time(row):
    # 대시보드 통계/그래프/히스토리 필터에서 사용할 시간 값을 고른다.
    # 서버 수신 시각이 가장 신뢰 가능하므로 우선 사용하고,
    # 없으면 예전 time 컬럼 또는 ESP32 timestamp를 fallback으로 사용한다.
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
    # 브라우저가 3초마다 호출하는 변경 감지용 값이다.
    # 실제 파일 mtime 대신 MAX(id)를 쓰면 새 센서 row가 들어왔는지만 가볍게 확인할 수 있다.
    conn = _connect()
    try:
        row = conn.execute("SELECT MAX(id) FROM sensor_data").fetchone()
        return row[0] if row[0] is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
