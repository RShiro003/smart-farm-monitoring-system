import os
import sqlite3
from datetime import datetime, timedelta

try:
    from services.discord_alert_service import send_discord_message
    from services.threshold_service import DEFAULT_THRESHOLDS, get_or_create_thresholds
except ModuleNotFoundError:
    from app.services.discord_alert_service import send_discord_message
    from app.services.threshold_service import DEFAULT_THRESHOLDS, get_or_create_thresholds


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.environ.get(
    "SMART_FARM_SENSOR_DB_FILE",
    os.path.join(_BASE_DIR, "data", "sensor_data.db"),
)

DEFAULT_COOLDOWN_MINUTES = 10

# light는 임계값/메시지 구조만 준비한다. 현재 기본 검사는 바질 기준의
# temperature, humidity, soil_moisture에 집중해 실제 조도 단위 차이로 인한 오탐을 피한다.
ALERT_METRICS = {
    "temperature": {"label": "온도", "unit": "°C", "enabled": True},
    "humidity": {"label": "습도", "unit": "%", "enabled": True},
    "soil_moisture": {"label": "토양 수분", "unit": "%", "enabled": True},
    "light": {"label": "조도", "unit": "", "enabled": False},
}

EVENT_LABELS = {
    "threshold_below": "기준값 미만",
    "threshold_above": "기준값 초과",
}


def _connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL,
            threshold_min REAL,
            threshold_max REAL,
            message TEXT,
            status TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_state (
            device_id TEXT NOT NULL,
            metric TEXT NOT NULL,
            event_type TEXT NOT NULL,
            last_status TEXT,
            last_sent_at TEXT,
            updated_at TEXT,
            PRIMARY KEY(device_id, metric, event_type)
        )
        """
    )
    conn.commit()


def _init_db():
    conn = _connect()
    try:
        _ensure_tables(conn)
    finally:
        conn.close()


_init_db()


def _now_string():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _coerce_optional_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_number(value):
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _format_value(value, unit):
    return f"{_format_number(value)}{unit}"


def _format_range(threshold_min, threshold_max, unit):
    return f"{_format_number(threshold_min)} ~ {_format_number(threshold_max)}{unit}"


def _cooldown_minutes():
    raw = os.environ.get("ALERT_COOLDOWN_MINUTES", str(DEFAULT_COOLDOWN_MINUTES))
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_MINUTES
    return max(0, minutes)


def _cooldown_elapsed(last_sent_at, now):
    if not last_sent_at:
        return True
    try:
        last_sent = datetime.strptime(last_sent_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return now - last_sent >= timedelta(minutes=_cooldown_minutes())


def _load_thresholds(device_id):
    try:
        return get_or_create_thresholds(device_id)
    except Exception as e:
        print(f"[Alert] Failed to load thresholds. Using defaults: {e}")
        data = {"device_id": device_id}
        data.update(DEFAULT_THRESHOLDS)
        return data


def _metric_thresholds(thresholds, metric):
    threshold_min = _coerce_optional_number(thresholds.get(f"{metric}_min"))
    threshold_max = _coerce_optional_number(thresholds.get(f"{metric}_max"))
    if threshold_min is None or threshold_max is None:
        return None, None
    return threshold_min, threshold_max


def _detect_event_type(value, threshold_min, threshold_max):
    if value < threshold_min:
        return "threshold_below"
    if value > threshold_max:
        return "threshold_above"
    return None


def _get_state(conn, device_id, metric, event_type):
    return conn.execute(
        """
        SELECT *
        FROM alert_state
        WHERE device_id = ? AND metric = ? AND event_type = ?
        """,
        (device_id, metric, event_type),
    ).fetchone()


def _active_states_for_metric(conn, device_id, metric):
    return conn.execute(
        """
        SELECT *
        FROM alert_state
        WHERE device_id = ? AND metric = ? AND last_status = 'abnormal'
        """,
        (device_id, metric),
    ).fetchall()


def _upsert_state(conn, device_id, metric, event_type, status, sent_at, updated_at):
    updated = conn.execute(
        """
        UPDATE alert_state
        SET last_status = ?,
            last_sent_at = ?,
            updated_at = ?
        WHERE device_id = ? AND metric = ? AND event_type = ?
        """,
        (status, sent_at, updated_at, device_id, metric, event_type),
    ).rowcount

    if updated == 0:
        conn.execute(
            """
            INSERT INTO alert_state (
                device_id, metric, event_type, last_status, last_sent_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (device_id, metric, event_type, status, sent_at, updated_at),
        )


def _insert_event_log(
    conn,
    device_id,
    metric,
    event_type,
    value,
    threshold_min,
    threshold_max,
    status,
    message,
    created_at,
):
    conn.execute(
        """
        INSERT INTO event_log (
            device_id,
            event_type,
            metric,
            value,
            threshold_min,
            threshold_max,
            message,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            device_id,
            event_type,
            metric,
            value,
            threshold_min,
            threshold_max,
            message,
            status,
            created_at,
        ),
    )


def _abnormal_message(device_id, metric_info, event_type, value, threshold_min, threshold_max, created_at):
    unit = metric_info["unit"]
    return "\n".join([
        "[스마트팜 이상 감지]",
        f"장치: {device_id}",
        f"항목: {metric_info['label']}",
        f"상태: {EVENT_LABELS[event_type]}",
        f"현재값: {_format_value(value, unit)}",
        f"정상 범위: {_format_range(threshold_min, threshold_max, unit)}",
        f"시간: {created_at}",
    ])


def _recovered_message(device_id, metric_info, value, threshold_min, threshold_max, created_at):
    unit = metric_info["unit"]
    return "\n".join([
        "[스마트팜 상태 복구]",
        f"장치: {device_id}",
        f"항목: {metric_info['label']}",
        f"현재값: {_format_value(value, unit)}",
        f"정상 범위: {_format_range(threshold_min, threshold_max, unit)}",
        f"시간: {created_at}",
    ])


def _should_record_abnormal(state, now):
    if state is None or state["last_status"] != "abnormal":
        return True
    return _cooldown_elapsed(state["last_sent_at"], now)


def _record_recovery_events(
    conn,
    messages,
    device_id,
    metric,
    metric_info,
    value,
    threshold_min,
    threshold_max,
    created_at,
    current_event_type=None,
):
    for state in _active_states_for_metric(conn, device_id, metric):
        event_type = state["event_type"]
        if event_type == current_event_type:
            continue
        message = _recovered_message(
            device_id, metric_info, value, threshold_min, threshold_max, created_at
        )
        _insert_event_log(
            conn,
            device_id,
            metric,
            event_type,
            value,
            threshold_min,
            threshold_max,
            "recovered",
            message,
            created_at,
        )
        _upsert_state(conn, device_id, metric, event_type, "recovered", created_at, created_at)
        messages.append(message)


def process_sensor_alerts(record):
    """저장된 센서 row를 기준으로 임계값 상태 전이를 검사한다.

    ESP32는 짧은 주기로 같은 값을 계속 보낼 수 있으므로 alert_state에 마지막 상태를 저장한다.
    정상->이상, 이상->복구 전이 때만 알림을 만들고, 이상 지속 중 재알림은 쿨타임으로 제한한다.
    """
    device_id = record.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        return

    device_id = device_id.strip()
    thresholds = _load_thresholds(device_id)
    created_at = record.get("server_received_at") or _now_string()
    now = datetime.now()
    messages = []

    conn = _connect()
    try:
        _ensure_tables(conn)

        for metric, metric_info in ALERT_METRICS.items():
            if not metric_info.get("enabled"):
                continue

            value = _coerce_optional_number(record.get(metric))
            if value is None:
                continue

            threshold_min, threshold_max = _metric_thresholds(thresholds, metric)
            if threshold_min is None or threshold_max is None:
                continue

            current_event_type = _detect_event_type(value, threshold_min, threshold_max)
            if current_event_type is None:
                _record_recovery_events(
                    conn,
                    messages,
                    device_id,
                    metric,
                    metric_info,
                    value,
                    threshold_min,
                    threshold_max,
                    created_at,
                )
                continue

            _record_recovery_events(
                conn,
                messages,
                device_id,
                metric,
                metric_info,
                value,
                threshold_min,
                threshold_max,
                created_at,
                current_event_type=current_event_type,
            )

            state = _get_state(conn, device_id, metric, current_event_type)
            if not _should_record_abnormal(state, now):
                _upsert_state(
                    conn,
                    device_id,
                    metric,
                    current_event_type,
                    "abnormal",
                    state["last_sent_at"],
                    created_at,
                )
                continue

            message = _abnormal_message(
                device_id,
                metric_info,
                current_event_type,
                value,
                threshold_min,
                threshold_max,
                created_at,
            )
            _insert_event_log(
                conn,
                device_id,
                metric,
                current_event_type,
                value,
                threshold_min,
                threshold_max,
                "abnormal",
                message,
                created_at,
            )
            _upsert_state(
                conn,
                device_id,
                metric,
                current_event_type,
                "abnormal",
                created_at,
                created_at,
            )
            messages.append(message)

        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        print(f"[Alert] Failed to process alert state: {e}")
        return
    finally:
        conn.close()

    for message in messages:
        send_discord_message(message)
