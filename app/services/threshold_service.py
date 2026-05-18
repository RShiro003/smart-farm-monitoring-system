import os
import sqlite3
import threading
from datetime import datetime


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 임계값은 센서 원본 데이터와 별도 DB 파일에 저장한다.
# 센서 row는 계속 누적되는 시계열 데이터이고, 임계값은 장치별 설정값이라 변경 주기와 사용 목적이 다르다.
DB_FILE = os.environ.get(
    "SMART_FARM_DB_FILE",
    os.path.join(_BASE_DIR, "data", "smart_farm.db"),
)

# 서버와 대시보드, ESP32 실제 노드가 같은 필드명을 사용해야 한다.
# 이 목록은 threshold_settings 테이블 컬럼, /api/thresholds JSON body, 브라우저 입력 id의 기준이 된다.
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

# 장치별 임계값이 아직 저장되지 않았을 때 사용하는 초기값이다.
# ESP32는 부팅 직후 서버에서 임계값을 가져오므로, DB에 row가 없어도 바로 LED 판단을 시작할 수 있어야 한다.
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

# Flask 개발 서버나 브라우저/ESP32 요청이 겹칠 수 있어 임계값 DB 접근은 lock으로 직렬화한다.
# SQLite 파일 하나를 여러 요청이 동시에 쓰면 잠금 충돌이 날 수 있기 때문이다.
_lock = threading.Lock()


def _connect():
    # SMART_FARM_DB_FILE 환경변수를 쓰면 테스트나 배포 환경에서 임계값 DB 위치를 바꿀 수 있다.
    # 기본값은 app/data/smart_farm.db이며, 없으면 디렉터리를 만든 뒤 SQLite에 연결한다.
    data_dir = os.path.dirname(DB_FILE)
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn):
    # 임계값 테이블은 장치별(device_id UNIQUE)로 한 row만 가진다.
    # 같은 ESP32가 임계값을 다시 저장하면 새 row를 늘리지 않고 기존 row를 UPDATE한다.
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
    # updated_at은 사람이 DB를 직접 확인할 때 마지막 설정 변경 시점을 알 수 있게 남기는 값이다.
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_number(value):
    # JSON 응답에서 18.0 대신 18처럼 보이게 하기 위한 표시용 정리다.
    # DB에는 REAL로 저장되지만 브라우저 입력창에는 불필요한 소수점을 줄여 보여준다.
    value = float(value)
    if value.is_integer():
        return int(value)
    return value


def _row_to_thresholds(row):
    # SQLite row를 /api/thresholds 응답에 바로 사용할 dict로 변환한다.
    # THRESHOLD_FIELDS 순서대로 꺼내면 서버/브라우저/ESP32가 공유하는 필드명이 유지된다.
    if row is None:
        return None

    data = {"device_id": row["device_id"]}
    for field in THRESHOLD_FIELDS:
        data[field] = _format_number(row[field])
    return data


def default_thresholds(device_id):
    # 새 device_id가 처음 등장했을 때 DB row를 만들기 위한 기본 설정이다.
    # 장치명을 포함해 반환하므로 이후 insert와 API 응답에서 같은 객체를 사용할 수 있다.
    data = {"device_id": device_id}
    data.update(DEFAULT_THRESHOLDS)
    return data


def _insert_thresholds(conn, settings):
    # 특정 장치의 임계값 row를 처음 생성한다.
    # get_or_create_thresholds()와 upsert_thresholds()가 모두 이 함수를 사용해 INSERT 형태를 통일한다.
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
    # 대시보드가 열릴 때와 ESP32 실제 노드가 주기적으로 호출하는 조회 함수다.
    # row가 없으면 기본값으로 생성해, 이후 같은 device_id는 항상 같은 설정을 조회하게 한다.
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

            # 최초 조회 시점에 기본 임계값을 DB에 저장해 둔다.
            # 이렇게 하면 사용자가 아직 저장 버튼을 누르지 않아도 ESP32가 일관된 기준을 받을 수 있다.
            settings = default_thresholds(device_id)
            _insert_thresholds(conn, settings)
            conn.commit()
            return settings


def upsert_thresholds(device_id, values):
    # 대시보드 임계값 저장 버튼이 호출하는 함수다.
    # 이미 장치 row가 있으면 UPDATE, 없으면 INSERT하여 API 호출자는 같은 endpoint만 사용하면 된다.
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
                # 같은 device_id에는 하나의 설정 row만 유지한다.
                # 최신 입력값으로 덮어써야 ESP32의 다음 임계값 GET에서 변경 사항이 반영된다.
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
                # 아직 서버가 모르는 새 장치라도 사용자가 설정을 먼저 저장할 수 있게 INSERT를 허용한다.
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
