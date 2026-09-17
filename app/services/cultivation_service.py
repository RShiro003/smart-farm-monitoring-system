import math
import os
import sqlite3
from collections import deque
from datetime import datetime, timedelta
from statistics import median

from .database import connect_database


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.environ.get(
    "SMART_FARM_SENSOR_DB_FILE",
    os.path.join(_BASE_DIR, "data", "sensor_data.db"),
)


def _env_float(name, default, minimum=None):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        return default
    return value


def _env_int(name, default, minimum=1):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


# ESP32의 약 5초 전송 주기를 기준으로 한 관수 감지 설정이다.
# 배포 환경에서 센서 특성에 맞게 환경변수로 조정할 수 있다.
WATERING_MIN_INCREASE = _env_float("WATERING_MIN_INCREASE", 8.0, minimum=0.1)
WATERING_HIGH_CONFIDENCE_INCREASE = _env_float(
    "WATERING_HIGH_CONFIDENCE_INCREASE",
    max(15.0, WATERING_MIN_INCREASE),
    minimum=WATERING_MIN_INCREASE,
)
WATERING_DETECTION_WINDOW_SECONDS = _env_int(
    "WATERING_DETECTION_WINDOW_SECONDS", 180
)
WATERING_COOLDOWN_SECONDS = _env_int("WATERING_COOLDOWN_SECONDS", 1800)
WATERING_EVENT_UPDATE_SECONDS = min(
    _env_int(
        "WATERING_EVENT_UPDATE_SECONDS",
        WATERING_DETECTION_WINDOW_SECONDS,
    ),
    WATERING_COOLDOWN_SECONDS,
)
WATERING_EXPECTED_INTERVAL_SECONDS = _env_int(
    "WATERING_EXPECTED_INTERVAL_SECONDS", 5
)
WATERING_MAX_SAMPLE_GAP_SECONDS = _env_int(
    "WATERING_MAX_SAMPLE_GAP_SECONDS", 15
)
WATERING_POST_SAMPLE_COUNT = _env_int(
    "WATERING_POST_SAMPLE_COUNT", 3, minimum=2
)
WATERING_BASELINE_SAMPLE_COUNT = _env_int(
    "WATERING_BASELINE_SAMPLE_COUNT", 12, minimum=2
)
WATERING_MIN_BASELINE_SAMPLES = min(
    _env_int("WATERING_MIN_BASELINE_SAMPLES", 2, minimum=2),
    WATERING_BASELINE_SAMPLE_COUNT,
)
WATERING_REQUIRED_ELEVATED_SAMPLES = min(
    _env_int("WATERING_REQUIRED_ELEVATED_SAMPLES", 3, minimum=2),
    WATERING_POST_SAMPLE_COUNT,
)
WATERING_MIN_ELEVATED_DURATION_SECONDS = min(
    _env_int("WATERING_MIN_ELEVATED_DURATION_SECONDS", 5),
    WATERING_DETECTION_WINDOW_SECONDS,
    (WATERING_POST_SAMPLE_COUNT - 1) * WATERING_MAX_SAMPLE_GAP_SECONDS,
)
WATERING_BASELINE_MAX_SPREAD = _env_float(
    "WATERING_BASELINE_MAX_SPREAD", 12.0, minimum=0.1
)
WATERING_MIN_RAW_DECREASE = _env_float(
    "WATERING_MIN_RAW_DECREASE", 1.0, minimum=0.0
)
WATERING_DETECTION_METHOD = "soil_moisture_jump"
WATERING_QUERY_LIMIT = (
    max(
        math.ceil(
            WATERING_DETECTION_WINDOW_SECONDS
            / WATERING_EXPECTED_INTERVAL_SECONDS
        ),
        WATERING_BASELINE_SAMPLE_COUNT,
    )
    + WATERING_POST_SAMPLE_COUNT
)


def _connect():
    return connect_database(DB_FILE)


def _ensure_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS growth_records (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id   TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            height_cm   REAL,
            leaf_count  INTEGER,
            note        TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watering_events (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id         TEXT NOT NULL,
            detected_at       TEXT NOT NULL,
            moisture_before   REAL NOT NULL,
            moisture_after    REAL NOT NULL,
            increase_amount   REAL NOT NULL,
            confidence        REAL CHECK(confidence BETWEEN 0 AND 1),
            detection_method  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_growth_records_device_recorded
        ON growth_records(device_id, recorded_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_watering_events_device_detected
        ON watering_events(device_id, detected_at)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_watering_events_dedupe
        ON watering_events(device_id, detected_at, detection_method)
        """
    )
    conn.commit()


def initialize_database():
    conn = _connect()
    try:
        _ensure_tables(conn)
    finally:
        conn.close()


initialize_database()


def _row_to_dict(row):
    return {
        key: row[key]
        for key in row.keys()
        if key != "id" and row[key] is not None
    }


def _parse_datetime(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def format_recorded_at(value=None):
    if value is None:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ValueError("recorded_at must be a valid ISO datetime")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def create_growth_record(
    device_id,
    height_cm=None,
    leaf_count=None,
    note=None,
    recorded_at=None,
):
    recorded_at = format_recorded_at(recorded_at)
    conn = _connect()
    try:
        cursor = conn.execute(
            """
            INSERT INTO growth_records (
                device_id, recorded_at, height_cm, leaf_count, note
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (device_id, recorded_at, height_cm, leaf_count, note),
        )
        row = conn.execute(
            "SELECT * FROM growth_records WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        conn.commit()
        return _row_to_dict(row)
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_growth_records(device_id, limit=1000):
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 1000
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM growth_records
                WHERE device_id = ?
                ORDER BY recorded_at DESC, id DESC LIMIT ?
            ) ORDER BY recorded_at ASC, id ASC
            """,
            (device_id, limit),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def _continuous_suffix(samples):
    if not samples:
        return []
    suffix = [samples[-1]]
    next_time = samples[-1]["parsed_at"]
    for sample in reversed(samples[:-1]):
        gap = (next_time - sample["parsed_at"]).total_seconds()
        if gap <= 0 or gap > WATERING_MAX_SAMPLE_GAP_SECONDS:
            break
        suffix.append(sample)
        next_time = sample["parsed_at"]
    suffix.reverse()
    return suffix


def _watering_confidence(increase, elevated_count):
    magnitude_span = max(
        0.1,
        WATERING_HIGH_CONFIDENCE_INCREASE - WATERING_MIN_INCREASE,
    )
    magnitude = min(
        1.0,
        max(0.0, (increase - WATERING_MIN_INCREASE) / magnitude_span),
    )
    persistence = min(1.0, elevated_count / WATERING_POST_SAMPLE_COUNT)
    return round(min(1.0, 0.45 + 0.40 * magnitude + 0.15 * persistence), 2)


def evaluate_watering_samples(samples):
    """최근 센서 샘플에서 지속된 토양수분 급상승 한 건을 판정한다."""
    prepared = []
    for sample in samples:
        parsed_at = _parse_datetime(sample.get("server_received_at"))
        value = sample.get("soil_moisture")
        if parsed_at is None or value is None or isinstance(value, bool):
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or not 0 <= value <= 100:
            continue

        soil_raw = sample.get("soil_raw")
        if soil_raw is not None and not isinstance(soil_raw, bool):
            try:
                soil_raw = float(soil_raw)
            except (TypeError, ValueError):
                soil_raw = None
            if (
                soil_raw is not None
                and (not math.isfinite(soil_raw) or not 0 <= soil_raw <= 4095)
            ):
                soil_raw = None
        else:
            soil_raw = None
        prepared.append({
            "id": sample.get("id"),
            "server_received_at": parsed_at.strftime("%Y-%m-%d %H:%M:%S"),
            "parsed_at": parsed_at,
            "soil_moisture": value,
            "soil_raw": soil_raw,
        })

    prepared.sort(key=lambda item: (item["parsed_at"], item.get("id") or 0))
    continuous = _continuous_suffix(prepared)
    required = WATERING_MIN_BASELINE_SAMPLES + WATERING_POST_SAMPLE_COUNT
    if len(continuous) < required:
        return None

    elapsed = (
        continuous[-1]["parsed_at"] - continuous[0]["parsed_at"]
    ).total_seconds()
    if elapsed > WATERING_DETECTION_WINDOW_SECONDS:
        return None

    post = continuous[-WATERING_POST_SAMPLE_COUNT:]
    baseline = continuous[:-WATERING_POST_SAMPLE_COUNT]
    baseline = baseline[-WATERING_BASELINE_SAMPLE_COUNT:]
    if len(baseline) < WATERING_MIN_BASELINE_SAMPLES:
        return None

    baseline_values = [sample["soil_moisture"] for sample in baseline]
    post_values = [sample["soil_moisture"] for sample in post]
    if (
        len(baseline_values) == WATERING_MIN_BASELINE_SAMPLES
        and max(baseline_values) - min(baseline_values)
        > WATERING_BASELINE_MAX_SPREAD
    ):
        return None

    moisture_before = float(median(baseline_values))
    moisture_after = float(median(post_values))
    increase = moisture_after - moisture_before
    if increase < WATERING_MIN_INCREASE:
        return None

    elevated_threshold = moisture_before + WATERING_MIN_INCREASE
    elevated = [
        sample for sample in post
        if sample["soil_moisture"] >= elevated_threshold
    ]
    if len(elevated) < WATERING_REQUIRED_ELEVATED_SAMPLES:
        return None
    if post[-1]["soil_moisture"] < elevated_threshold:
        return None

    elevated_duration = (
        elevated[-1]["parsed_at"] - elevated[0]["parsed_at"]
    ).total_seconds()
    if elevated_duration < WATERING_MIN_ELEVATED_DURATION_SECONDS:
        return None

    # 실제 SEN0308 노드는 젖을수록 soil_raw가 감소한다. 모든 비교 샘플에
    # 원시값이 있으면 이 방향도 함께 확인해 보정값 변경만으로 %가 뛴 경우를 막는다.
    baseline_raw = [sample["soil_raw"] for sample in baseline]
    post_raw = [sample["soil_raw"] for sample in post]
    if all(value is not None for value in baseline_raw + post_raw):
        raw_decrease = float(median(baseline_raw)) - float(median(post_raw))
        if raw_decrease < WATERING_MIN_RAW_DECREASE:
            return None

    return {
        "detected_at": elevated[0]["server_received_at"],
        "moisture_before": round(moisture_before, 2),
        "moisture_after": round(moisture_after, 2),
        "increase_amount": round(increase, 2),
        "confidence": _watering_confidence(increase, len(elevated)),
        "detection_method": WATERING_DETECTION_METHOD,
    }


def _recent_watering_samples(conn, device_id, received_at, sensor_row_id=None):
    received_time = _parse_datetime(received_at)
    if received_time is None:
        return []
    cutoff = (
        received_time - timedelta(seconds=WATERING_DETECTION_WINDOW_SECONDS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    clauses = [
        "device_id = ?",
        "soil_moisture IS NOT NULL",
        "server_received_at >= ?",
        "server_received_at <= ?",
    ]
    params = [device_id, cutoff, received_time.strftime("%Y-%m-%d %H:%M:%S")]
    if sensor_row_id is not None:
        clauses.append("id <= ?")
        params.append(sensor_row_id)

    rows = conn.execute(
        f"""
        SELECT id, server_received_at, soil_moisture, soil_raw
        FROM sensor_data
        WHERE {' AND '.join(clauses)}
        ORDER BY server_received_at DESC, id DESC
        LIMIT ?
        """,
        params + [WATERING_QUERY_LIMIT],
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def _update_watering_event(conn, latest, event):
    """같은 관수의 후속 샘플로 수분 상승량과 신뢰도를 보강한다."""
    moisture_before = float(latest["moisture_before"])
    moisture_after = max(
        float(latest["moisture_after"]),
        float(event["moisture_after"]),
    )
    increase_amount = round(moisture_after - moisture_before, 2)
    confidence = max(
        float(latest["confidence"] or 0),
        float(event["confidence"]),
        _watering_confidence(increase_amount, WATERING_POST_SAMPLE_COUNT),
    )
    if (
        moisture_after > float(latest["moisture_after"])
        or increase_amount > float(latest["increase_amount"])
        or confidence > float(latest["confidence"] or 0)
    ):
        conn.execute(
            """
            UPDATE watering_events
            SET moisture_after = ?, increase_amount = ?, confidence = ?
            WHERE id = ?
            """,
            (
                round(moisture_after, 2),
                increase_amount,
                round(min(1.0, confidence), 2),
                latest["id"],
            ),
        )
        return True
    return False


def _insert_watering_event(conn, device_id, event):
    return conn.execute(
        """
        INSERT INTO watering_events (
            device_id, detected_at, moisture_before, moisture_after,
            increase_amount, confidence, detection_method
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            device_id,
            event["detected_at"],
            event["moisture_before"],
            event["moisture_after"],
            event["increase_amount"],
            event["confidence"],
            event["detection_method"],
        ),
    ).lastrowid


def detect_watering_event(record, sensor_row_id=None):
    device_id = record.get("device_id")
    received_at = record.get("server_received_at")
    if not device_id or not received_at:
        return None

    conn = _connect()
    try:
        samples = _recent_watering_samples(
            conn,
            device_id,
            received_at,
            sensor_row_id=sensor_row_id,
        )
        event = evaluate_watering_samples(samples)
        if event is None:
            return None

        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute(
            """
            SELECT * FROM watering_events
            WHERE device_id = ?
            ORDER BY detected_at DESC, id DESC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()
        if latest:
            latest_at = _parse_datetime(latest["detected_at"])
            event_at = _parse_datetime(event["detected_at"])
            if latest_at and event_at:
                since_last = (event_at - latest_at).total_seconds()
                if since_last < 0:
                    conn.rollback()
                    return None
                if since_last < WATERING_COOLDOWN_SECONDS:
                    if since_last <= WATERING_EVENT_UPDATE_SECONDS:
                        if _update_watering_event(conn, latest, event):
                            conn.commit()
                            updated = conn.execute(
                                "SELECT * FROM watering_events WHERE id = ?",
                                (latest["id"],),
                            ).fetchone()
                            return _row_to_dict(updated)
                    conn.rollback()
                    return None

        try:
            _insert_watering_event(conn, device_id, event)
        except sqlite3.IntegrityError:
            conn.rollback()
            return None
        conn.commit()
        return {"device_id": device_id, **event}
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def _store_backfilled_watering_event(conn, device_id, event):
    event_at = _parse_datetime(event["detected_at"])
    cooldown = timedelta(seconds=WATERING_COOLDOWN_SECONDS)
    # 판정 중에는 쓰기 잠금을 잡지 않고, 중복 확인과 저장만 직렬화한다.
    # 별도 reader 연결의 스냅샷과 달리 실시간/다른 backfill의 저장도 확인한다.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            """
            SELECT * FROM watering_events
            WHERE device_id = ? AND detected_at <= ? AND detected_at > ?
            ORDER BY detected_at DESC, id DESC
            LIMIT 1
            """,
            (
                device_id,
                event["detected_at"],
                (event_at - cooldown).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        ).fetchone()
        if previous is not None:
            since_last = (
                event_at - _parse_datetime(previous["detected_at"])
            ).total_seconds()
            if since_last <= WATERING_EVENT_UPDATE_SECONDS:
                _update_watering_event(conn, previous, event)
            return previous["id"], False

        # 과거 시점의 후보는 이미 저장된 이후 이벤트와도 겹칠 수 있다.
        following = conn.execute(
            """
            SELECT id FROM watering_events
            WHERE device_id = ? AND detected_at > ? AND detected_at < ?
            ORDER BY detected_at ASC, id ASC
            LIMIT 1
            """,
            (
                device_id,
                event["detected_at"],
                (event_at + cooldown).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        ).fetchone()
        if following is not None:
            return following["id"], False

        return _insert_watering_event(conn, device_id, event), True


def backfill_watering_events(device_id=None):
    """저장된 센서 기록을 순차 재생한다. 서버 초기화에서는 호출하지 않는다.

    scanned_samples는 읽은 센서 행 수(판정 불가능한 행 포함), created_events는
    새 이벤트 수, existing_events는 중복/cooldown으로 매칭된 기존 이벤트의
    고유 개수다. 이번 실행에서 생성한 이벤트의 후속 판정은 중복 집계하지 않는다.
    """
    if device_id is not None:
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must be a non-empty string")
        device_id = device_id.strip()

    result = {"scanned_samples": 0, "created_events": 0, "existing_events": 0}
    reader = _connect()
    writer = None
    try:
        writer = _connect()
        clauses = ["device_id IS NOT NULL", "TRIM(device_id) != ''"]
        params = []
        if device_id is not None:
            clauses.append("device_id = ?")
            params.append(device_id)
        rows = reader.execute(
            f"""
            SELECT id, device_id, server_received_at, soil_moisture, soil_raw
            FROM sensor_data
            WHERE {' AND '.join(clauses)}
            ORDER BY device_id ASC, server_received_at ASC, id ASC
            """,
            params,
        )
        current_device = None
        samples = deque(maxlen=WATERING_QUERY_LIMIT)
        created_ids = set()
        existing_ids = set()
        for row in rows:
            result["scanned_samples"] += 1
            if row["device_id"] != current_device:
                current_device = row["device_id"]
                samples.clear()
                created_ids.clear()
                existing_ids.clear()
            received_at = _parse_datetime(row["server_received_at"])
            if received_at is None:
                continue
            cutoff = received_at - timedelta(
                seconds=WATERING_DETECTION_WINDOW_SECONDS
            )
            while samples and samples[0][0] < cutoff:
                samples.popleft()
            # 실시간 SELECT처럼 NULL 수분은 query limit을 차지하지 않는다.
            if row["soil_moisture"] is not None:
                samples.append((received_at, dict(row)))
            event = evaluate_watering_samples([
                sample for at, sample in samples if cutoff <= at <= received_at
            ])
            if event is None:
                continue
            event_id, created = _store_backfilled_watering_event(
                writer, current_device, event
            )
            if created:
                created_ids.add(event_id)
                result["created_events"] += 1
            elif event_id not in created_ids and event_id not in existing_ids:
                existing_ids.add(event_id)
                result["existing_events"] += 1
        return result
    finally:
        reader.close()
        if writer is not None:
            writer.close()


def list_watering_events(device_id, limit=100):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))

    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM watering_events
            WHERE device_id = ?
            ORDER BY detected_at DESC, id DESC
            LIMIT ?
            """,
            (device_id, limit),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def get_daily_analysis(device_id, days=30):
    days = max(1, min(int(days), 365))
    start_date = (datetime.now().date() - timedelta(days=days - 1)).isoformat()
    conn = _connect()
    try:
        _ensure_tables(conn)
        has_hourly = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='sensor_data_hourly'"
        ).fetchone() is not None
        oldest_raw = conn.execute(
            "SELECT strftime('%Y-%m-%d %H:00:00', MIN(server_received_at)) "
            "FROM sensor_data WHERE device_id = ?",
            (device_id,),
        ).fetchone()[0]
        hourly_union = ""
        sensor_params = [device_id, start_date]
        if has_hourly:
            boundary = oldest_raw or "9999-12-31 23:00:00"
            hourly_union = """
                UNION ALL
                SELECT date(bucket), temperature_avg, temperature_min,
                       temperature_max, humidity_avg, humidity_min, humidity_max,
                       soil_moisture_avg, soil_moisture_min, soil_moisture_max,
                       sample_count
                FROM sensor_data_hourly
                WHERE device_id = ? AND bucket >= ? AND bucket < ?
            """
            sensor_params.extend([device_id, start_date, boundary])
        rows = conn.execute(
            f"""
            WITH sensor_parts AS (
                SELECT
                    date(server_received_at) AS day,
                    AVG(temperature) AS temperature_avg,
                    MIN(temperature) AS temperature_min,
                    MAX(temperature) AS temperature_max,
                    AVG(humidity) AS humidity_avg,
                    MIN(humidity) AS humidity_min,
                    MAX(humidity) AS humidity_max,
                    AVG(soil_moisture) AS soil_moisture_avg,
                    MIN(soil_moisture) AS soil_moisture_min,
                    MAX(soil_moisture) AS soil_moisture_max,
                    COUNT(*) AS sample_count
                FROM sensor_data
                WHERE device_id = ?
                  AND server_received_at >= ?
                  AND server_received_at IS NOT NULL
                GROUP BY date(server_received_at)
                {hourly_union}
            ),
            daily_sensor AS (
                SELECT day,
                    SUM(temperature_avg * sample_count) / NULLIF(SUM(CASE WHEN temperature_avg IS NOT NULL THEN sample_count ELSE 0 END), 0) AS temperature_avg,
                    MIN(temperature_min) AS temperature_min,
                    MAX(temperature_max) AS temperature_max,
                    SUM(humidity_avg * sample_count) / NULLIF(SUM(CASE WHEN humidity_avg IS NOT NULL THEN sample_count ELSE 0 END), 0) AS humidity_avg,
                    MIN(humidity_min) AS humidity_min,
                    MAX(humidity_max) AS humidity_max,
                    SUM(soil_moisture_avg * sample_count) / NULLIF(SUM(CASE WHEN soil_moisture_avg IS NOT NULL THEN sample_count ELSE 0 END), 0) AS soil_moisture_avg,
                    MIN(soil_moisture_min) AS soil_moisture_min,
                    MAX(soil_moisture_max) AS soil_moisture_max,
                    SUM(sample_count) AS sample_count
                FROM sensor_parts
                GROUP BY day
            ),
            daily_watering AS (
                SELECT date(detected_at) AS day, COUNT(*) AS watering_count
                FROM watering_events
                WHERE device_id = ? AND detected_at >= ?
                GROUP BY date(detected_at)
            )
            SELECT
                daily_sensor.*,
                COALESCE(daily_watering.watering_count, 0) AS watering_count
            FROM daily_sensor
            LEFT JOIN daily_watering ON daily_watering.day = daily_sensor.day
            WHERE daily_sensor.day IS NOT NULL
            ORDER BY daily_sensor.day ASC
            """,
            sensor_params + [device_id, start_date],
        ).fetchall()
    finally:
        conn.close()

    def rounded(value):
        return round(float(value), 1) if value is not None else None

    return [
        {
            "date": row["day"],
            "temperature_avg": rounded(row["temperature_avg"]),
            "temperature_min": rounded(row["temperature_min"]),
            "temperature_max": rounded(row["temperature_max"]),
            "humidity_avg": rounded(row["humidity_avg"]),
            "humidity_min": rounded(row["humidity_min"]),
            "humidity_max": rounded(row["humidity_max"]),
            "soil_moisture_avg": rounded(row["soil_moisture_avg"]),
            "soil_moisture_min": rounded(row["soil_moisture_min"]),
            "soil_moisture_max": rounded(row["soil_moisture_max"]),
            "watering_count": int(row["watering_count"] or 0),
            "sample_count": int(row["sample_count"] or 0),
        }
        for row in rows
    ]
