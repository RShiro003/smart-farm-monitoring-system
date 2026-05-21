import json
import os
import sqlite3
import threading

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.environ.get(
    "SMART_FARM_DB_FILE",
    os.path.join(_BASE_DIR, "data", "smart_farm.db"),
)

# 작물 프로필에 저장되는 수치 필드 목록이다.
# API 검증과 DB INSERT/UPDATE에서 이 목록을 공통으로 사용한다.
CROP_VALUE_FIELDS = [
    "temperature_min", "temperature_max",
    "humidity_min", "humidity_max",
    "soil_moisture_min", "soil_moisture_max",
    "light_min", "light_max",
]

_DEFAULT_CROPS = [
    {
        "name": "바질",
        "temperature_min": 18, "temperature_max": 27,
        "humidity_min": 40, "humidity_max": 70,
        "soil_moisture_min": 50, "soil_moisture_max": 70,
        "light_min": 2000, "light_max": 8000,
        "notes": [
            "🌡️ 최적 온도: 21–27°C. 10°C 이하 생장 정지, 35°C 이상 열 스트레스.",
            "💧 물 주기: 겉흙이 마르면 충분히 관수. 약 2–3일 간격 권장.",
            "☀️ 일조: 하루 6–8시간 직사광선. 실내 2,000 lux 이상 확보.",
            "🍄 주의: 습도 80% 초과 시 잿빛곰팡이 발생 위험.",
            "🌱 토양: 과습 금지. 배수 잘되는 배양토 사용 권장.",
        ],
    },
    {
        "name": "상추",
        "temperature_min": 15, "temperature_max": 20,
        "humidity_min": 60, "humidity_max": 80,
        "soil_moisture_min": 60, "soil_moisture_max": 80,
        "light_min": 1000, "light_max": 3000,
        "notes": [
            "🌡️ 최적 온도: 15–20°C. 서늘한 환경에서 잘 자랍니다.",
            "💧 물 주기: 토양이 촉촉하게 유지되도록 관리. 1–2일 간격 권장.",
            "☀️ 일조: 하루 4–6시간. 강한 직사광선은 쓴맛을 유발합니다.",
            "🌱 토양: 배수가 잘되는 비옥한 토양 선호.",
            "❄️ 주의: 여름 고온 시 꽃대가 올라올 수 있습니다.",
        ],
    },
    {
        "name": "토마토",
        "temperature_min": 20, "temperature_max": 27,
        "humidity_min": 50, "humidity_max": 70,
        "soil_moisture_min": 60, "soil_moisture_max": 80,
        "light_min": 5000, "light_max": 10000,
        "notes": [
            "🌡️ 최적 온도: 20–27°C. 15°C 이하 착과 불량, 30°C 이상 꽃가루 사멸.",
            "💧 물 주기: 규칙적으로 관수. 건조-과습 반복은 배꼽썩음병 원인.",
            "☀️ 일조: 하루 8시간 이상 직사광선 필요.",
            "🌱 토양: 깊고 비옥한 토양, pH 6.0–6.8 적정.",
            "🍅 지지대: 성장하면서 무게를 지탱할 지지대 필요.",
        ],
    },
]

_lock = threading.Lock()


def _connect():
    data_dir = os.path.dirname(DB_FILE)
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables(conn):
    # crop_profiles: 작물별 권장 환경 범위와 재배 팁을 저장한다.
    # device_crop: 각 ESP32 장치에 현재 어떤 작물이 할당되어 있는지 추적한다.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crop_profiles (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL UNIQUE,
            temperature_min  REAL    NOT NULL,
            temperature_max  REAL    NOT NULL,
            humidity_min     REAL    NOT NULL,
            humidity_max     REAL    NOT NULL,
            soil_moisture_min REAL   NOT NULL,
            soil_moisture_max REAL   NOT NULL,
            light_min        REAL    NOT NULL,
            light_max        REAL    NOT NULL,
            notes            TEXT    NOT NULL DEFAULT '[]'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS device_crop (
            device_id TEXT    PRIMARY KEY,
            crop_id   INTEGER NOT NULL REFERENCES crop_profiles(id)
        )
    """)
    conn.commit()
    # 처음 테이블이 생성됐을 때 기본 작물 3종을 넣는다.
    # 이후에는 사용자가 직접 추가/수정/삭제한다.
    if conn.execute("SELECT COUNT(*) FROM crop_profiles").fetchone()[0] == 0:
        for crop in _DEFAULT_CROPS:
            conn.execute(
                """INSERT INTO crop_profiles
                   (name, temperature_min, temperature_max,
                    humidity_min, humidity_max,
                    soil_moisture_min, soil_moisture_max,
                    light_min, light_max, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    crop["name"],
                    crop["temperature_min"], crop["temperature_max"],
                    crop["humidity_min"], crop["humidity_max"],
                    crop["soil_moisture_min"], crop["soil_moisture_max"],
                    crop["light_min"], crop["light_max"],
                    json.dumps(crop["notes"], ensure_ascii=False),
                ),
            )
        conn.commit()


def _fmt(value):
    # DB의 REAL 값을 응답에서 18.0 대신 18처럼 보이게 한다.
    v = float(value)
    return int(v) if v.is_integer() else v


def _row_to_dict(row):
    if row is None:
        return None
    d = {
        "id": row["id"],
        "name": row["name"],
        "temperature_min": _fmt(row["temperature_min"]),
        "temperature_max": _fmt(row["temperature_max"]),
        "humidity_min": _fmt(row["humidity_min"]),
        "humidity_max": _fmt(row["humidity_max"]),
        "soil_moisture_min": _fmt(row["soil_moisture_min"]),
        "soil_moisture_max": _fmt(row["soil_moisture_max"]),
        "light_min": _fmt(row["light_min"]),
        "light_max": _fmt(row["light_max"]),
    }
    try:
        d["notes"] = json.loads(row["notes"]) if row["notes"] else []
    except (json.JSONDecodeError, TypeError):
        d["notes"] = []
    return d


def list_crops():
    with _lock:
        with _connect() as conn:
            _ensure_tables(conn)
            rows = conn.execute(
                "SELECT * FROM crop_profiles ORDER BY id ASC"
            ).fetchall()
            return [_row_to_dict(r) for r in rows]


def get_crop(crop_id):
    with _lock:
        with _connect() as conn:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM crop_profiles WHERE id = ?", (crop_id,)
            ).fetchone()
            return _row_to_dict(row)


def create_crop(data):
    notes_json = json.dumps(data.get("notes", []), ensure_ascii=False)
    with _lock:
        with _connect() as conn:
            _ensure_tables(conn)
            cur = conn.execute(
                """INSERT INTO crop_profiles
                   (name, temperature_min, temperature_max,
                    humidity_min, humidity_max,
                    soil_moisture_min, soil_moisture_max,
                    light_min, light_max, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["name"],
                    data["temperature_min"], data["temperature_max"],
                    data["humidity_min"], data["humidity_max"],
                    data["soil_moisture_min"], data["soil_moisture_max"],
                    data["light_min"], data["light_max"],
                    notes_json,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM crop_profiles WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _row_to_dict(row)


def update_crop(crop_id, data):
    notes_json = json.dumps(data.get("notes", []), ensure_ascii=False)
    with _lock:
        with _connect() as conn:
            _ensure_tables(conn)
            conn.execute(
                """UPDATE crop_profiles
                   SET name=?, temperature_min=?, temperature_max=?,
                       humidity_min=?, humidity_max=?,
                       soil_moisture_min=?, soil_moisture_max=?,
                       light_min=?, light_max=?, notes=?
                   WHERE id=?""",
                (
                    data["name"],
                    data["temperature_min"], data["temperature_max"],
                    data["humidity_min"], data["humidity_max"],
                    data["soil_moisture_min"], data["soil_moisture_max"],
                    data["light_min"], data["light_max"],
                    notes_json,
                    crop_id,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM crop_profiles WHERE id = ?", (crop_id,)
            ).fetchone()
            return _row_to_dict(row)


def delete_crop(crop_id):
    with _lock:
        with _connect() as conn:
            _ensure_tables(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM crop_profiles"
            ).fetchone()[0]
            if count <= 1:
                return False, "마지막 작물 프로필은 삭제할 수 없습니다."
            # 삭제될 작물을 사용 중인 장치는 남은 첫 번째 작물로 이전한다.
            fallback = conn.execute(
                "SELECT id FROM crop_profiles WHERE id != ? ORDER BY id ASC LIMIT 1",
                (crop_id,),
            ).fetchone()
            if fallback:
                conn.execute(
                    "UPDATE device_crop SET crop_id=? WHERE crop_id=?",
                    (fallback["id"], crop_id),
                )
            conn.execute("DELETE FROM crop_profiles WHERE id=?", (crop_id,))
            conn.commit()
            return True, None


def get_device_crop(device_id):
    # 장치에 할당된 작물을 반환한다. 할당 기록이 없으면 첫 번째 작물을 기본값으로 반환한다.
    with _lock:
        with _connect() as conn:
            _ensure_tables(conn)
            row = conn.execute(
                """SELECT cp.* FROM crop_profiles cp
                   JOIN device_crop dc ON cp.id = dc.crop_id
                   WHERE dc.device_id = ?""",
                (device_id,),
            ).fetchone()
            if row:
                return _row_to_dict(row)
            row = conn.execute(
                "SELECT * FROM crop_profiles ORDER BY id ASC LIMIT 1"
            ).fetchone()
            return _row_to_dict(row)


def set_device_crop(device_id, crop_id):
    with _lock:
        with _connect() as conn:
            _ensure_tables(conn)
            exists = conn.execute(
                "SELECT device_id FROM device_crop WHERE device_id=?", (device_id,)
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE device_crop SET crop_id=? WHERE device_id=?",
                    (crop_id, device_id),
                )
            else:
                conn.execute(
                    "INSERT INTO device_crop (device_id, crop_id) VALUES (?, ?)",
                    (device_id, crop_id),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM crop_profiles WHERE id=?", (crop_id,)
            ).fetchone()
            return _row_to_dict(row)
