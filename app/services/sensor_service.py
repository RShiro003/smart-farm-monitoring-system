import json
import os
import sqlite3
from datetime import datetime, timedelta


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


_TIME_EXPR = (
    "replace(COALESCE(NULLIF(server_received_at, ''), "
    "NULLIF(time, ''), NULLIF(timestamp, '')), 'T', ' ')"
)


def _normalize_positive_int(value, default, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    number = max(1, number)
    if maximum is not None:
        number = min(number, maximum)
    return number


def _valid_hhmm(value):
    value = (value or "").strip()
    if not value:
        return ""
    try:
        datetime.strptime(value, "%H:%M")
        return value
    except ValueError:
        return ""


def _valid_date(value):
    value = (value or "").strip()
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return ""


def _history_where(device_id=None, date=None, time_from=None, time_to=None):
    clauses = []
    params = []

    selected = normalize_device_filter(device_id)
    if selected is not None:
        clauses.append("device_id = ?")
        params.append(selected)

    date = (date or "").strip()
    time_from = _valid_hhmm(time_from)
    time_to = _valid_hhmm(time_to)

    if date:
        valid_date = _valid_date(date)
        if not valid_date:
            clauses.append("0 = 1")
        else:
            start_time = time_from or "00:00"
            start_at = f"{valid_date} {start_time}:00"
            clauses.append("server_received_at >= ?")
            params.append(start_at)

            if time_to:
                clauses.append("server_received_at <= ?")
                params.append(f"{valid_date} {time_to}:00")
            else:
                next_day = datetime.strptime(valid_date, "%Y-%m-%d") + timedelta(days=1)
                clauses.append("server_received_at < ?")
                params.append(next_day.strftime("%Y-%m-%d 00:00:00"))
    else:
        if time_from:
            clauses.append(f"time({_TIME_EXPR}) >= time(?)")
            params.append(time_from)

        if time_to:
            clauses.append(f"time({_TIME_EXPR}) <= time(?)")
            params.append(time_to)

    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def _period_cutoff(period, mapping, default_key):
    delta = mapping.get(period, mapping[default_key])
    return (datetime.now() - delta).strftime("%Y-%m-%d %H:%M:%S")


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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sensor_data_device_id ON sensor_data(device_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sensor_data_id ON sensor_data(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sensor_data_device_id_id ON sensor_data(device_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sensor_data_received_at ON sensor_data(server_received_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sensor_data_device_received ON sensor_data(device_id, server_received_at)")
        conn.commit()
        # 새 DB가 비어 있을 때만 레거시 JSON을 가져온다.
        # 이미 SQLite에 row가 있으면 중복 import를 막기 위해 JSON을 다시 읽지 않는다.
        if conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0] == 0:
            _migrate_from_json(conn)
    finally:
        conn.close()


_init_db()


# ── Public API ─────────────────────────────────────────────────────────────────

def _process_alerts_after_save(record):
    # 알림 처리 실패가 ESP32의 /api/sensor POST 실패로 이어지면 안 된다.
    # 센서 row 저장을 먼저 확정한 뒤, 임계값/Discord 처리는 별도 단계에서 안전하게 시도한다.
    try:
        try:
            from services.alert_service import process_sensor_alerts
        except ModuleNotFoundError:
            from app.services.alert_service import process_sensor_alerts

        process_sensor_alerts(record)
    except Exception as e:
        print(f"[Alert] Sensor alert processing skipped: {e}")

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


def list_sensor_records(device_id=None, limit=500, include_all=False):
    selected = normalize_device_filter(device_id)
    where = ""
    params = []
    if selected is not None:
        where = " WHERE device_id = ?"
        params.append(selected)

    conn = _connect()
    try:
        if include_all:
            rows = conn.execute(
                f"SELECT * FROM sensor_data{where} ORDER BY id ASC",
                params,
            ).fetchall()
        else:
            safe_limit = _normalize_positive_int(limit, 500, maximum=5000)
            rows = conn.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM sensor_data{where}
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                params + [safe_limit],
            ).fetchall()
        return [_row_to_dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def get_latest_sensor_record(device_id=None):
    selected = normalize_device_filter(device_id)
    where = ""
    params = []
    if selected is not None:
        where = " WHERE device_id = ?"
        params.append(selected)

    conn = _connect()
    try:
        row = conn.execute(
            f"SELECT * FROM sensor_data{where} ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
        return _row_to_dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def list_device_ids_from_db():
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT device_id FROM sensor_data ORDER BY device_id"
        ).fetchall()
        return [
            normalize_device_id(row["device_id"])
            for row in rows
            if row["device_id"] is not None
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def get_sensor_history(device_id=None, page=1, per_page=10, date=None, time_from=None, time_to=None):
    page = _normalize_positive_int(page, 1)
    per_page = _normalize_positive_int(per_page, 10, maximum=200)
    offset = (page - 1) * per_page
    where, params = _history_where(device_id, date, time_from, time_to)

    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM sensor_data{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def count_sensor_history(device_id=None, date=None, time_from=None, time_to=None):
    where, params = _history_where(device_id, date, time_from, time_to)
    conn = _connect()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM sensor_data{where}",
            params,
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def get_sensor_rows_for_chart(device_id=None, period="hourly", limit=300):
    chart_windows = {
        "hourly": timedelta(hours=24),
        "daily": timedelta(days=7),
        "weekly": timedelta(weeks=8),
        "monthly": timedelta(days=365),
    }
    bucket_exprs = {
        "hourly": "strftime('%Y-%m-%d %H:00:00', server_received_at)",
        "daily": "strftime('%Y-%m-%d 00:00:00', server_received_at)",
        "weekly": (
            "date(server_received_at, '-' || "
            "((CAST(strftime('%w', server_received_at) AS INTEGER) + 6) % 7) || "
            "' days') || ' 00:00:00'"
        ),
        "monthly": "strftime('%Y-%m-01 00:00:00', server_received_at)",
    }
    period = period if period in chart_windows else "hourly"
    safe_limit = _normalize_positive_int(limit, 300, maximum=5000)

    clauses = ["server_received_at >= ?"]
    params = [_period_cutoff(period, chart_windows, "hourly")]
    selected = normalize_device_filter(device_id)
    if selected is not None:
        clauses.append("device_id = ?")
        params.append(selected)

    where = " WHERE " + " AND ".join(clauses)
    bucket_expr = bucket_exprs[period]

    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT
                bucket AS server_received_at,
                AVG(temperature) AS temperature,
                AVG(humidity) AS humidity,
                AVG(soil_moisture) AS soil_moisture,
                AVG(COALESCE(light, light_digital)) AS light,
                COUNT(*) AS bucket_count
            FROM (
                SELECT
                    {bucket_expr} AS bucket,
                    temperature,
                    humidity,
                    soil_moisture,
                    light,
                    light_digital
                FROM sensor_data
                {where}
            )
            WHERE bucket IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket ASC
            LIMIT ?
            """,
            params + [safe_limit],
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def get_sensor_stats(device_id=None, period="daily"):
    stat_windows = {
        "daily": timedelta(days=1),
        "weekly": timedelta(days=7),
        "monthly": timedelta(days=30),
    }
    period = period if period in stat_windows else "daily"

    clauses = ["server_received_at >= ?"]
    params = [_period_cutoff(period, stat_windows, "daily")]
    selected = normalize_device_filter(device_id)
    if selected is not None:
        clauses.insert(0, "device_id = ?")
        params.insert(0, selected)

    conn = _connect()
    try:
        row = conn.execute(
            f"""
            SELECT
                AVG(temperature) AS temperature,
                AVG(humidity) AS humidity,
                AVG(soil_moisture) AS soil_moisture,
                AVG(COALESCE(light, light_digital)) AS light,
                COUNT(*) AS count
            FROM sensor_data
            WHERE {' AND '.join(clauses)}
            """,
            params,
        ).fetchone()

        def rounded(value):
            return round(float(value), 1) if value is not None else None

        return {
            "temperature": rounded(row["temperature"]),
            "humidity": rounded(row["humidity"]),
            "soil_moisture": rounded(row["soil_moisture"]),
            "light": rounded(row["light"]),
            "count": int(row["count"] or 0),
        }
    except sqlite3.Error:
        return {
            "temperature": None,
            "humidity": None,
            "soil_moisture": None,
            "light": None,
            "count": 0,
        }
    finally:
        conn.close()


def append_sensor_data(new_record):
    """센서 row 하나를 SQLite에 INSERT한다.

    _COLUMNS에 들어 있는 키만 저장된다. 추가 payload 키는 라우트 응답에는
    포함될 수 있지만 sensor_data.db에는 저장되지 않는다.
    """
    record = normalize_sensor_record(new_record)
    conn = _connect()
    saved = False
    try:
        # INSERT와 commit을 한 함수 안에서 묶어 ESP32 POST 한 건이 DB row 한 건으로 확정되게 한다.
        _insert(conn, record)
        conn.commit()
        saved = True
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()

    if saved:
        _process_alerts_after_save(record)


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
