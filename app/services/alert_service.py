import os
import sqlite3
import threading
from datetime import datetime, timedelta

from .database import connect_database
from .datetime_filters import datetime_conditions
from .validation import finite_number, positive_int as _normalize_positive_int

try:
    from services.discord_alert_service import send_discord_message
    from services.sensor_service import LIGHT_UNIT_LUX, list_device_status, offline_after_seconds
    from services.threshold_service import DEFAULT_THRESHOLDS, get_or_create_thresholds
except ModuleNotFoundError:
    from app.services.discord_alert_service import send_discord_message
    from app.services.sensor_service import LIGHT_UNIT_LUX, list_device_status, offline_after_seconds
    from app.services.threshold_service import DEFAULT_THRESHOLDS, get_or_create_thresholds


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.environ.get(
    "SMART_FARM_SENSOR_DB_FILE",
    os.path.join(_BASE_DIR, "data", "sensor_data.db"),
)

DEFAULT_COOLDOWN_MINUTES = 10
DEFAULT_ABNORMAL_COUNT = 1
DEFAULT_RECOVERY_COUNT = 1
DEFAULT_ABNORMAL_DURATION_SECONDS = 0
DEFAULT_DANGER_DEVIATION_PERCENT = 25.0
DEFAULT_SUMMARY_HOUR = 8
DEFAULT_SUMMARY_WEEKDAY = 0  # Monday
# 오프라인 감시 스레드가 장치 상태를 다시 확인하는 주기다.
DEFAULT_OFFLINE_CHECK_SECONDS = 30

# 조도는 단위가 lux인 노드에서만 검사한다.
# LM393류 비교기 모듈의 0/1(digital) 출력은 lux 기준 임계값과 비교할 수 없어
# process_sensor_alerts()가 해당 row의 조도 검사를 건너뛴다.
ALERT_METRICS = {
    "temperature": {"label": "온도", "unit": "°C", "enabled": True},
    "humidity": {"label": "습도", "unit": "%", "enabled": True},
    "soil_moisture": {"label": "토양 수분", "unit": "%", "enabled": True},
    "light": {
        "label": "조도",
        "unit": " lux",
        "enabled": True,
        "requires_light_unit": LIGHT_UNIT_LUX,
    },
}

EVENT_LABELS = {
    "threshold_below": "기준값 미만",
    "threshold_above": "기준값 초과",
    "device_offline": "장치 무응답",
}

# 오프라인 감지는 센서값이 아니라 장치 연결 상태를 다루므로
# metric 컬럼에 실제 센서 항목 대신 이 예약어를 사용한다.
DEVICE_METRIC = "device"

# alert_settings에서 "모든 장치의 기본값" row를 가리키는 예약 device_id다.
# 실제 장치 ID와 겹치지 않도록 일반 식별자에 쓰지 않는 문자를 포함한다.
GLOBAL_SETTINGS_ID = "__global__"

# 화면에서 켜고 끌 수 있는 알림 항목이다.
# 센서 항목 4개에 더해 장치 무응답 알림도 별도로 제어한다.
ALERT_TOGGLE_FIELDS = (
    "temperature",
    "humidity",
    "soil_moisture",
    "light",
    "device_offline",
)

ALERT_TOGGLE_LABELS = {
    "temperature": "온도",
    "humidity": "습도",
    "soil_moisture": "토양 수분",
    "light": "조도",
    "device_offline": "장치 무응답",
}

ALERT_RULE_DEFAULTS = {
    "abnormal_count": DEFAULT_ABNORMAL_COUNT,
    "recovery_count": DEFAULT_RECOVERY_COUNT,
    "abnormal_duration_seconds": DEFAULT_ABNORMAL_DURATION_SECONDS,
    "danger_deviation_percent": DEFAULT_DANGER_DEVIATION_PERCENT,
    "danger_immediate": True,
    "daily_summary": False,
    "weekly_summary": False,
    "summary_hour": DEFAULT_SUMMARY_HOUR,
    "summary_weekday": DEFAULT_SUMMARY_WEEKDAY,
}
ALERT_RULE_FIELDS = tuple(ALERT_RULE_DEFAULTS)
ALERT_RULE_BOOLEAN_FIELDS = ("danger_immediate", "daily_summary", "weekly_summary")


def _connect():
    return connect_database(DB_FILE)


def _ensure_tables(conn):
    # 알림 설정은 장치별로 한 row를 가진다.
    # device_id가 GLOBAL_SETTINGS_ID인 row는 개별 설정이 없는 장치에 적용되는 기본값이다.
    # 이렇게 하면 "전체 기본값 하나 + 예외 장치 몇 개" 형태를 자연스럽게 표현할 수 있다.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_settings (
            device_id        TEXT PRIMARY KEY,
            temperature      INTEGER NOT NULL DEFAULT 1,
            humidity         INTEGER NOT NULL DEFAULT 1,
            soil_moisture    INTEGER NOT NULL DEFAULT 1,
            light            INTEGER NOT NULL DEFAULT 1,
            device_offline   INTEGER NOT NULL DEFAULT 1,
            cooldown_minutes REAL,
            webhook_url      TEXT,
            abnormal_count   INTEGER NOT NULL DEFAULT 1,
            recovery_count   INTEGER NOT NULL DEFAULT 1,
            abnormal_duration_seconds INTEGER NOT NULL DEFAULT 0,
            danger_deviation_percent REAL NOT NULL DEFAULT 25,
            danger_immediate INTEGER NOT NULL DEFAULT 1,
            daily_summary    INTEGER NOT NULL DEFAULT 0,
            weekly_summary   INTEGER NOT NULL DEFAULT 0,
            summary_hour     INTEGER NOT NULL DEFAULT 8,
            summary_weekday  INTEGER NOT NULL DEFAULT 0,
            updated_at       TEXT NOT NULL
        )
        """
    )
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
            severity TEXT NOT NULL DEFAULT 'warning',
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
            candidate_count INTEGER NOT NULL DEFAULT 0,
            first_observed_at TEXT,
            recovery_count INTEGER NOT NULL DEFAULT 0,
            severity TEXT,
            PRIMARY KEY(device_id, metric, event_type)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id       TEXT NOT NULL,
            message         TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            claimed_at      TEXT,
            sent_at         TEXT,
            last_error      TEXT,
            created_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notification_outbox_due "
        "ON notification_outbox(status, next_attempt_at)"
    )
    # 오래된 월의 알림을 기간으로 조회할 때 전체 event_log를 매번 훑지 않게 한다.
    # 장치 선택 여부에 따라 두 인덱스 중 하나를 SQLite가 고를 수 있다.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_log_created "
        "ON event_log(created_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_log_device_created "
        "ON event_log(device_id, created_at DESC, id DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS summary_delivery (
            device_id   TEXT NOT NULL,
            summary_type TEXT NOT NULL,
            period_key  TEXT NOT NULL,
            queued_at   TEXT NOT NULL,
            PRIMARY KEY (device_id, summary_type, period_key)
        )
        """
    )

    # 기존 설치 DB를 데이터 손실 없이 확장한다.
    migrations = {
        "alert_settings": {
            "abnormal_count": "INTEGER NOT NULL DEFAULT 1",
            "recovery_count": "INTEGER NOT NULL DEFAULT 1",
            "abnormal_duration_seconds": "INTEGER NOT NULL DEFAULT 0",
            "danger_deviation_percent": "REAL NOT NULL DEFAULT 25",
            "danger_immediate": "INTEGER NOT NULL DEFAULT 1",
            "daily_summary": "INTEGER NOT NULL DEFAULT 0",
            "weekly_summary": "INTEGER NOT NULL DEFAULT 0",
            "summary_hour": "INTEGER NOT NULL DEFAULT 8",
            "summary_weekday": "INTEGER NOT NULL DEFAULT 0",
        },
        "alert_state": {
            "candidate_count": "INTEGER NOT NULL DEFAULT 0",
            "first_observed_at": "TEXT",
            "recovery_count": "INTEGER NOT NULL DEFAULT 0",
            "severity": "TEXT",
        },
        "event_log": {
            "severity": "TEXT NOT NULL DEFAULT 'warning'",
        },
    }
    for table, columns in migrations.items():
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
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


def _parse_datetime(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError:
        return None


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


def _env_cooldown_minutes():
    # 저장된 설정이 없을 때 쓰는 값이다.
    # 기존 배포는 ALERT_COOLDOWN_MINUTES 환경변수로 운영해 왔으므로 그 값을 계속 존중한다.
    raw = os.environ.get("ALERT_COOLDOWN_MINUTES", str(DEFAULT_COOLDOWN_MINUTES))
    try:
        minutes = finite_number(raw)
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_MINUTES
    return max(0, minutes)


def _cooldown_elapsed(last_sent_at, now, cooldown_minutes=None):
    if not last_sent_at:
        return True
    try:
        last_sent = datetime.strptime(last_sent_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    if cooldown_minutes is None:
        cooldown_minutes = _env_cooldown_minutes()
    return (now - last_sent).total_seconds() >= cooldown_minutes * 60


# ── 알림 설정 ──────────────────────────────────────────────────────────────────

def _settings_row_to_dict(row, device_id):
    cooldown = row["cooldown_minutes"] if row is not None else None
    result = {
        "device_id": device_id,
        **{
            field: bool(row[field]) if row is not None else True
            for field in ALERT_TOGGLE_FIELDS
        },
        # None이면 "환경변수 기본값을 따른다"는 뜻이라 그대로 내려보낸다.
        "cooldown_minutes": float(cooldown) if cooldown is not None else None,
        "webhook_url": (row["webhook_url"] if row is not None else None),
        "updated_at": (row["updated_at"] if row is not None else None),
    }
    for field, default in ALERT_RULE_DEFAULTS.items():
        value = row[field] if row is not None else default
        result[field] = bool(value) if field in ALERT_RULE_BOOLEAN_FIELDS else value
    return result


def _read_settings(conn, device_id):
    return conn.execute(
        "SELECT * FROM alert_settings WHERE device_id = ?", (device_id,)
    ).fetchone()


def _resolve_settings(conn, device_id):
    """장치 설정 -> 전역 설정 -> 코드 기본값 순으로 적용값을 결정한다.

    장치별 row가 없으면 전역 row를 쓰고, 전역도 없으면 모든 항목이 켜져 있고
    쿨타임은 환경변수를 따르는 기존 동작이 그대로 유지된다.
    """
    row = _read_settings(conn, device_id)
    if row is not None:
        resolved = _settings_row_to_dict(row, device_id)
        resolved["source"] = "device"
    else:
        global_row = _read_settings(conn, GLOBAL_SETTINGS_ID)
        resolved = _settings_row_to_dict(global_row, device_id)
        resolved["source"] = "global" if global_row is not None else "default"

    if resolved["cooldown_minutes"] is None:
        resolved["cooldown_minutes"] = _env_cooldown_minutes()
    if not resolved["webhook_url"]:
        # 저장된 URL이 없으면 discord_alert_service가 환경변수를 사용한다.
        resolved["webhook_url"] = None
    return resolved


def get_alert_settings(device_id=None):
    """대시보드 알림 설정 화면이 읽는 값이다.

    device_id를 주지 않으면 전역 기본 설정을 돌려준다.
    """
    target = (device_id or "").strip() or GLOBAL_SETTINGS_ID
    conn = _connect()
    try:
        _ensure_tables(conn)
        return _resolve_settings(conn, target)
    except sqlite3.Error as e:
        print(f"[Alert] Failed to read alert settings: {e}")
        return {
            "device_id": target,
            **{field: True for field in ALERT_TOGGLE_FIELDS},
            **ALERT_RULE_DEFAULTS,
            "cooldown_minutes": _env_cooldown_minutes(),
            "webhook_url": None,
            "updated_at": None,
            "source": "default",
        }
    finally:
        conn.close()


def save_alert_settings(device_id, values):
    """알림 설정을 저장한다. device_id가 비어 있으면 전역 기본값을 저장한다."""
    target = (device_id or "").strip() or GLOBAL_SETTINGS_ID
    now = _now_string()

    conn = _connect()
    try:
        _ensure_tables(conn)
        existing = _read_settings(conn, target)
        # 부분 갱신을 허용한다. 화면이 항상 전체 필드를 보내지 않아도 되게 하기 위함이다.
        current = _settings_row_to_dict(existing, target)

        toggles = []
        for field in ALERT_TOGGLE_FIELDS:
            value = values.get(field, current[field])
            toggles.append(1 if value else 0)

        cooldown = values.get("cooldown_minutes", current["cooldown_minutes"])
        webhook = values.get("webhook_url", current["webhook_url"])
        if isinstance(webhook, str):
            webhook = webhook.strip() or None
        rules = [values.get(field, current[field]) for field in ALERT_RULE_FIELDS]
        rules = [
            (1 if value else 0) if field in ALERT_RULE_BOOLEAN_FIELDS else value
            for field, value in zip(ALERT_RULE_FIELDS, rules)
        ]

        if existing is None:
            conn.execute(
                f"""
                INSERT INTO alert_settings (
                    device_id, {', '.join(ALERT_TOGGLE_FIELDS)},
                    cooldown_minutes, webhook_url, {', '.join(ALERT_RULE_FIELDS)},
                    updated_at
                ) VALUES (?, {', '.join('?' * len(ALERT_TOGGLE_FIELDS))}, ?, ?,
                    {', '.join('?' * len(ALERT_RULE_FIELDS))}, ?)
                """,
                (target, *toggles, cooldown, webhook, *rules, now),
            )
        else:
            assignments = ", ".join(f"{field} = ?" for field in ALERT_TOGGLE_FIELDS)
            rule_assignments = ", ".join(f"{field} = ?" for field in ALERT_RULE_FIELDS)
            conn.execute(
                f"""
                UPDATE alert_settings
                SET {assignments}, cooldown_minutes = ?, webhook_url = ?,
                    {rule_assignments}, updated_at = ?
                WHERE device_id = ?
                """,
                (*toggles, cooldown, webhook, *rules, now, target),
            )
        conn.commit()
        return _resolve_settings(conn, target)
    except sqlite3.Error as e:
        conn.rollback()
        print(f"[Alert] Failed to save alert settings: {e}")
        raise
    finally:
        conn.close()


def delete_alert_settings(device_id):
    """장치별 설정을 지워 전역 기본값을 다시 따르게 한다."""
    target = (device_id or "").strip()
    if not target or target == GLOBAL_SETTINGS_ID:
        return False
    conn = _connect()
    try:
        _ensure_tables(conn)
        deleted = conn.execute(
            "DELETE FROM alert_settings WHERE device_id = ?", (target,)
        ).rowcount
        conn.commit()
        return deleted > 0
    except sqlite3.Error:
        conn.rollback()
        return False
    finally:
        conn.close()


def list_configured_devices():
    # 장치별 예외 설정이 걸린 장치 목록이다. 설정 화면에서 한눈에 보여 준다.
    conn = _connect()
    try:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT device_id FROM alert_settings WHERE device_id != ? ORDER BY device_id",
            (GLOBAL_SETTINGS_ID,),
        ).fetchall()
        return [row["device_id"] for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


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


def _event_severity(value, threshold_min, threshold_max, danger_percent):
    """정상 범위 폭 대비 이탈률로 경고와 위험을 구분한다."""
    span = max(abs(threshold_max - threshold_min), 1e-9)
    if value < threshold_min:
        deviation = threshold_min - value
    elif value > threshold_max:
        deviation = value - threshold_max
    else:
        return "warning"
    return "danger" if deviation / span * 100 >= danger_percent else "warning"


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


def _candidate_states_for_metric(conn, device_id, metric):
    return conn.execute(
        """
        SELECT * FROM alert_state
        WHERE device_id = ? AND metric = ?
          AND last_status IN ('abnormal', 'pending')
        """,
        (device_id, metric),
    ).fetchall()


def _upsert_state(
    conn, device_id, metric, event_type, status, sent_at, updated_at,
    candidate_count=0, first_observed_at=None, recovery_count=0, severity=None,
):
    updated = conn.execute(
        """
        UPDATE alert_state
        SET last_status = ?,
            last_sent_at = ?,
            updated_at = ?,
            candidate_count = ?,
            first_observed_at = ?,
            recovery_count = ?,
            severity = ?
        WHERE device_id = ? AND metric = ? AND event_type = ?
        """,
        (
            status, sent_at, updated_at, candidate_count, first_observed_at,
            recovery_count, severity, device_id, metric, event_type,
        ),
    ).rowcount

    if updated == 0:
        conn.execute(
            """
            INSERT INTO alert_state (
                device_id, metric, event_type, last_status, last_sent_at, updated_at,
                candidate_count, first_observed_at, recovery_count, severity
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id, metric, event_type, status, sent_at, updated_at,
                candidate_count, first_observed_at, recovery_count, severity,
            ),
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
    severity="warning",
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
            severity,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            severity,
            created_at,
        ),
    )


def _abnormal_message(
    device_id, metric_info, event_type, value, threshold_min, threshold_max,
    created_at, severity="warning",
):
    unit = metric_info["unit"]
    level = "위험" if severity == "danger" else "경고"
    return "\n".join([
        f"[스마트팜 {level} 감지]",
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


def _should_record_abnormal(state, now, cooldown_minutes=None, severity="warning"):
    if state is None or state["last_status"] != "abnormal":
        return True
    if severity == "danger" and state["severity"] != "danger":
        return True
    return _cooldown_elapsed(state["last_sent_at"], now, cooldown_minutes)


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
    recovery_required=1,
):
    for state in _candidate_states_for_metric(conn, device_id, metric):
        event_type = state["event_type"]
        if event_type == current_event_type:
            continue
        if state["last_status"] == "pending":
            # 조건을 채우기 전에 정상화된 후보는 실제 알림 이력 없이 제거한다.
            conn.execute(
                "DELETE FROM alert_state WHERE device_id = ? AND metric = ? AND event_type = ?",
                (device_id, metric, event_type),
            )
            continue

        recovered_count = int(state["recovery_count"] or 0) + 1
        if recovered_count < recovery_required:
            _upsert_state(
                conn, device_id, metric, event_type, "abnormal",
                state["last_sent_at"], created_at,
                candidate_count=state["candidate_count"] or 0,
                first_observed_at=state["first_observed_at"],
                recovery_count=recovered_count,
                severity=state["severity"],
            )
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
            severity=state["severity"] or "warning",
        )
        _upsert_state(conn, device_id, metric, event_type, "recovered", created_at, created_at)
        messages.append(message)


def _enqueue_notification(conn, device_id, message, created_at=None):
    created_at = created_at or _now_string()
    conn.execute(
        """
        INSERT INTO notification_outbox (
            device_id, message, status, attempts, next_attempt_at, created_at
        ) VALUES (?, ?, 'pending', 0, ?, ?)
        """,
        (device_id, message, created_at, created_at),
    )


def deliver_pending_notifications(limit=20):
    """Claim and deliver durable notification jobs.

    Each claim is committed before network I/O. A crashed worker's claim is
    returned to pending after five minutes, and transient failures are retried
    with bounded exponential backoff.
    """
    max_attempts = 8
    delivered = 0
    for _ in range(max(1, min(int(limit), 100))):
        conn = _connect()
        job = None
        try:
            _ensure_tables(conn)
            now = datetime.now()
            now_string = now.strftime("%Y-%m-%d %H:%M:%S")
            stale_claim = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'pending', claimed_at = NULL
                WHERE status = 'sending' AND claimed_at < ?
                """,
                (stale_claim,),
            )
            job = conn.execute(
                """
                SELECT * FROM notification_outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY id ASC LIMIT 1
                """,
                (now_string,),
            ).fetchone()
            if job is None:
                conn.commit()
                break
            claimed = conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'sending', claimed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now_string, job["id"]),
            ).rowcount
            conn.commit()
            if not claimed:
                continue
            job = dict(job)
        finally:
            conn.close()

        settings = get_alert_settings(job["device_id"])
        ok = send_discord_message(
            job["message"], webhook_url=settings.get("webhook_url")
        )
        conn = _connect()
        try:
            attempts = int(job["attempts"] or 0) + 1
            if ok:
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'sent', attempts = ?, sent_at = ?,
                        claimed_at = NULL, last_error = NULL
                    WHERE id = ?
                    """,
                    (attempts, _now_string(), job["id"]),
                )
                delivered += 1
            else:
                failed = attempts >= max_attempts
                delay_minutes = min(60, 2 ** min(attempts - 1, 6))
                next_attempt = (
                    datetime.now() + timedelta(minutes=delay_minutes)
                ).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = ?, attempts = ?, next_attempt_at = ?,
                        claimed_at = NULL, last_error = ?
                    WHERE id = ?
                    """,
                    (
                        "failed" if failed else "pending",
                        attempts,
                        next_attempt,
                        "Discord delivery failed",
                        job["id"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    return delivered


def process_sensor_alerts(record):
    """저장된 센서 row를 기준으로 임계값 상태 전이를 검사한다.

    연속 횟수와 지속 시간 조건을 만족한 상태 전이만 알림으로 확정한다.
    정상 범위 폭에서 크게 벗어난 위험 값은 설정에 따라 즉시 확정할 수 있다.
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
    settings = None
    try:
        _ensure_tables(conn)
        # 사용자가 화면에서 끈 항목은 아예 검사하지 않는다.
        # 쿨타임도 장치별 설정을 우선 적용한다.
        settings = _resolve_settings(conn, device_id)
        cooldown_minutes = settings["cooldown_minutes"]
        abnormal_required = max(1, int(settings["abnormal_count"]))
        recovery_required = max(1, int(settings["recovery_count"]))
        duration_required = max(0, int(settings["abnormal_duration_seconds"]))
        danger_percent = max(0.0, float(settings["danger_deviation_percent"]))

        for metric, metric_info in ALERT_METRICS.items():
            if not metric_info.get("enabled"):
                continue

            if not settings.get(metric, True):
                continue

            # 조도처럼 특정 단위에서만 의미가 있는 항목은 단위가 맞을 때만 검사한다.
            required_unit = metric_info.get("requires_light_unit")
            if required_unit is not None and record.get("light_unit") != required_unit:
                # 단위가 바뀌어 더 이상 평가할 수 없는 항목을 계속 활성 경보로
                # 남겨 두지 않는다. 과거 low/high 상태를 명시적으로 종료한다.
                for event_type in ("threshold_below", "threshold_above"):
                    state = _get_state(conn, device_id, metric, event_type)
                    if state is None or state["last_status"] != "abnormal":
                        continue
                    message = (
                        f"✅ [{device_id}] {metric_info['label']} 감시 종료\n"
                        f"단위가 {record.get('light_unit') or 'unknown'}(으)로 변경되어 "
                        f"{required_unit} 기준 경보를 종료합니다.\n시간: {created_at}"
                    )
                    _insert_event_log(
                        conn, device_id, metric, event_type, None, None, None,
                        "recovered", message, created_at,
                    )
                    _upsert_state(
                        conn, device_id, metric, event_type, "recovered",
                        created_at, created_at,
                    )
                    messages.append(message)
                conn.execute(
                    "DELETE FROM alert_state WHERE device_id = ? AND metric = ? "
                    "AND last_status = 'pending'",
                    (device_id, metric),
                )
                continue

            value = _coerce_optional_number(record.get(metric))
            if value is None:
                # 읽기 실패는 연속 관측을 끊는다. 누락 전후 값을 연속으로 세지 않는다.
                conn.execute(
                    "DELETE FROM alert_state WHERE device_id = ? AND metric = ? "
                    "AND last_status = 'pending'",
                    (device_id, metric),
                )
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
                    recovery_required=recovery_required,
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
                recovery_required=recovery_required,
            )

            state = _get_state(conn, device_id, metric, current_event_type)
            severity = _event_severity(
                value, threshold_min, threshold_max, danger_percent
            )

            if state is not None and state["last_status"] == "abnormal":
                candidate_count = int(state["candidate_count"] or abnormal_required)
                first_observed_at = state["first_observed_at"] or created_at
                if not _should_record_abnormal(
                    state, now, cooldown_minutes, severity=severity
                ):
                    _upsert_state(
                        conn, device_id, metric, current_event_type,
                        "abnormal", state["last_sent_at"], created_at,
                        candidate_count=candidate_count,
                        first_observed_at=first_observed_at,
                        severity=(
                            "danger" if state["severity"] == "danger" else severity
                        ),
                    )
                    continue
            else:
                candidate_count = (
                    int(state["candidate_count"] or 0) + 1
                    if state is not None and state["last_status"] == "pending"
                    else 1
                )
                first_observed_at = (
                    state["first_observed_at"]
                    if state is not None and state["last_status"] == "pending"
                    else created_at
                )
                first_time = _parse_datetime(first_observed_at) or now
                observed_time = _parse_datetime(created_at) or now
                duration_elapsed = max(0, (observed_time - first_time).total_seconds())
                confirmed = (
                    candidate_count >= abnormal_required
                    and duration_elapsed >= duration_required
                )
                if severity == "danger" and settings.get("danger_immediate", True):
                    confirmed = True
                if not confirmed:
                    _upsert_state(
                        conn, device_id, metric, current_event_type,
                        "pending", None, created_at,
                        candidate_count=candidate_count,
                        first_observed_at=first_observed_at,
                        severity=severity,
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
                severity=severity,
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
                severity=severity,
            )
            _upsert_state(
                conn,
                device_id,
                metric,
                current_event_type,
                "abnormal",
                created_at,
                created_at,
                candidate_count=candidate_count,
                first_observed_at=first_observed_at,
                severity=severity,
            )
            messages.append(message)

        for message in messages:
            _enqueue_notification(conn, device_id, message, created_at)
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        print(f"[Alert] Failed to process alert state: {e}")
        return
    finally:
        conn.close()

    if messages:
        deliver_pending_notifications(limit=len(messages))


# ── Event log 조회 ─────────────────────────────────────────────────────────────

# 대시보드 알림 기록 표에서 쓰는 상태 필터다.
EVENT_STATUSES = ("abnormal", "recovered")


def _event_where(
    device_id=None, status=None, metric=None, date=None,
    time_from=None, time_to=None, date_from=None, date_to=None,
):
    clauses, params = datetime_conditions(
        "created_at", date=date, time_from=time_from, time_to=time_to,
        date_from=date_from, date_to=date_to,
    )
    if isinstance(device_id, str) and device_id.strip():
        clauses.append("device_id = ?")
        params.append(device_id.strip())
    if isinstance(status, str) and status.strip() in EVENT_STATUSES:
        clauses.append("status = ?")
        params.append(status.strip())
    if isinstance(metric, str) and metric.strip():
        clauses.append("metric = ?")
        params.append(metric.strip())
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _event_row_to_dict(row):
    # 화면에서 쓸 한글 라벨까지 여기서 붙인다.
    # 브라우저가 metric/event_type 코드값을 다시 해석하지 않아도 되게 하기 위함이다.
    metric = row["metric"]
    metric_info = ALERT_METRICS.get(metric)
    if metric_info is not None:
        metric_label = metric_info["label"]
        unit = metric_info["unit"]
    else:
        metric_label = "장치" if metric == DEVICE_METRIC else metric
        unit = ""

    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "event_type": row["event_type"],
        "event_label": EVENT_LABELS.get(row["event_type"], row["event_type"]),
        "metric": metric,
        "metric_label": metric_label,
        "unit": unit,
        "value": row["value"],
        "threshold_min": row["threshold_min"],
        "threshold_max": row["threshold_max"],
        "status": row["status"],
        "severity": row["severity"],
        "message": row["message"],
        "created_at": row["created_at"],
    }


def list_events(
    device_id=None,
    page=1,
    per_page=10,
    status=None,
    metric=None,
    date=None,
    time_from=None,
    time_to=None,
    date_from=None,
    date_to=None,
):
    """알림 기록을 최신순으로 페이지 단위 조회한다."""
    page = _normalize_positive_int(page, 1)
    per_page = _normalize_positive_int(per_page, 10, maximum=200)
    where, params = _event_where(
        device_id, status, metric, date, time_from, time_to, date_from, date_to
    )

    conn = _connect()
    try:
        _ensure_tables(conn)
        rows = conn.execute(
            f"SELECT * FROM event_log{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
        return [_event_row_to_dict(row) for row in rows]
    except sqlite3.Error as e:
        print(f"[Alert] Failed to list events: {e}")
        raise
    finally:
        conn.close()


def count_events(
    device_id=None,
    status=None,
    metric=None,
    date=None,
    time_from=None,
    time_to=None,
    date_from=None,
    date_to=None,
):
    where, params = _event_where(
        device_id, status, metric, date, time_from, time_to, date_from, date_to
    )
    conn = _connect()
    try:
        _ensure_tables(conn)
        row = conn.execute(
            f"SELECT COUNT(*) FROM event_log{where}", params
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error as e:
        print(f"[Alert] Failed to count events: {e}")
        raise
    finally:
        conn.close()


def count_active_alerts(device_id=None):
    # 현재 이상 상태로 남아 있는 항목 수다.
    # 대시보드가 "지금 문제가 있는지"를 한 숫자로 보여줄 때 사용한다.
    clauses = ["last_status = 'abnormal'"]
    params = []
    if isinstance(device_id, str) and device_id.strip():
        clauses.append("device_id = ?")
        params.append(device_id.strip())

    conn = _connect()
    try:
        _ensure_tables(conn)
        row = conn.execute(
            f"SELECT COUNT(*) FROM alert_state WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        raise
    finally:
        conn.close()


# ── 장치 오프라인 감지 ─────────────────────────────────────────────────────────

def _offline_message(device_id, last_seen_at, age_seconds, created_at):
    return "\n".join([
        "[스마트팜 장치 무응답]",
        f"장치: {device_id}",
        f"마지막 수신: {last_seen_at or '기록 없음'}",
        f"경과: 약 {int(age_seconds // 60)}분",
        f"시간: {created_at}",
    ])


def _online_message(device_id, last_seen_at, created_at):
    return "\n".join([
        "[스마트팜 장치 복구]",
        f"장치: {device_id}",
        f"마지막 수신: {last_seen_at or '기록 없음'}",
        f"시간: {created_at}",
    ])


def check_device_offline():
    """모든 장치의 마지막 수신 시각을 확인해 오프라인/복구 전이를 기록한다.

    센서값 기반 알림과 달리 이 검사는 "데이터가 오지 않는 것"이 신호다.
    /api/sensor 처리 흐름에서는 원리상 감지할 수 없어 주기적 호출이 필요하다.
    """
    created_at = _now_string()
    now = datetime.now()
    messages = []

    try:
        statuses = list_device_status()
        try:
            from services.device_service import list_devices
        except ModuleNotFoundError:
            from app.services.device_service import list_devices
        seen = {entry["device_id"] for entry in statuses}
        for device in list_devices(seen):
            if device["device_id"] in seen or not device.get("registered"):
                continue
            created = _parse_datetime(device.get("created_at"))
            age = (now - created).total_seconds() if created else None
            statuses.append({
                "device_id": device["device_id"],
                "last_seen_at": None,
                "age_seconds": age,
                "status": (
                    "offline" if age is not None and age > offline_after_seconds()
                    else "unknown"
                ),
                "offline_after_seconds": offline_after_seconds(),
                "total": 0,
            })
    except Exception as e:
        print(f"[Alert] Failed to read device status: {e}")
        return []

    conn = _connect()
    try:
        _ensure_tables(conn)
        # 여러 WSGI 프로세스가 watchdog을 각각 실행하더라도 상태 조회와 갱신은
        # 한 프로세스씩 수행한다. 둘이 같은 이전 상태를 읽고 중복 알림을 만드는
        # 경쟁 조건을 SQLite 쓰기 잠금으로 차단한다.
        conn.execute("BEGIN IMMEDIATE")

        for entry in statuses:
            # 시각을 해석할 수 없는 장치(unknown)는 판정 근거가 없어 건너뛴다.
            if entry["status"] not in {"online", "offline"}:
                continue

            device_id = entry["device_id"]
            # 장치 무응답 알림은 장치별로 끌 수 있다.
            # 예를 들어 배터리로 간헐 동작하는 보드는 오프라인이 정상 상태다.
            device_settings = _resolve_settings(conn, device_id)
            if not device_settings.get("device_offline", True):
                continue
            cooldown_minutes = device_settings["cooldown_minutes"]
            webhook_url = device_settings.get("webhook_url")

            state = _get_state(conn, device_id, DEVICE_METRIC, "device_offline")

            if entry["status"] == "offline":
                if not _should_record_abnormal(state, now, cooldown_minutes):
                    # 이상 상태가 계속되는 중이다. 쿨타임 안이면 재알림하지 않고
                    # updated_at만 갱신해 마지막 확인 시점을 남긴다.
                    _upsert_state(
                        conn, device_id, DEVICE_METRIC, "device_offline",
                        "abnormal", state["last_sent_at"], created_at,
                    )
                    continue

                message = _offline_message(
                    device_id, entry["last_seen_at"], entry["age_seconds"] or 0, created_at
                )
                _insert_event_log(
                    conn, device_id, DEVICE_METRIC, "device_offline",
                    entry["age_seconds"], None, entry["offline_after_seconds"],
                    "abnormal", message, created_at,
                )
                _upsert_state(
                    conn, device_id, DEVICE_METRIC, "device_offline",
                    "abnormal", created_at, created_at,
                )
                _enqueue_notification(conn, device_id, message, created_at)
                messages.append((message, webhook_url))
                continue

            # 온라인 상태. 직전이 abnormal이었을 때만 복구 이벤트를 남긴다.
            # 그래야 정상 동작 중인 장치가 매 주기마다 복구 알림을 만들지 않는다.
            if state is not None and state["last_status"] == "abnormal":
                message = _online_message(device_id, entry["last_seen_at"], created_at)
                _insert_event_log(
                    conn, device_id, DEVICE_METRIC, "device_offline",
                    entry["age_seconds"], None, entry["offline_after_seconds"],
                    "recovered", message, created_at,
                )
                _upsert_state(
                    conn, device_id, DEVICE_METRIC, "device_offline",
                    "recovered", created_at, created_at,
                )
                _enqueue_notification(conn, device_id, message, created_at)
                messages.append((message, webhook_url))

        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        print(f"[Alert] Failed to process offline state: {e}")
        return []
    finally:
        conn.close()

    # 장치마다 다른 채널로 보낼 수 있으므로 메시지와 webhook을 쌍으로 들고 다닌다.
    if messages:
        deliver_pending_notifications(limit=len(messages))

    # 호출자와 테스트는 메시지 본문만 필요로 한다.
    return [message for message, _ in messages]


# ── 정기 Discord 요약 ──────────────────────────────────────────────────────────

def _scheduled_period(now, summary_type, settings):
    hour = int(settings["summary_hour"])
    if summary_type == "daily":
        scheduled = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if scheduled > now:
            scheduled -= timedelta(days=1)
        end = scheduled.replace(hour=0)
        return scheduled, end - timedelta(days=1), end

    weekday = int(settings["summary_weekday"])
    days_back = (now.weekday() - weekday) % 7
    scheduled = (now - timedelta(days=days_back)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    if scheduled > now:
        scheduled -= timedelta(days=7)
    end = scheduled.replace(hour=0)
    return scheduled, end - timedelta(days=7), end


def build_summary_message(device_id, summary_type, start, end):
    conn = _connect()
    try:
        _ensure_tables(conn)
        row = conn.execute(
            """
            SELECT COUNT(*) AS samples,
                   AVG(temperature) AS temperature_avg,
                   MIN(temperature) AS temperature_min,
                   MAX(temperature) AS temperature_max,
                   AVG(humidity) AS humidity_avg,
                   MIN(humidity) AS humidity_min,
                   MAX(humidity) AS humidity_max,
                   AVG(soil_moisture) AS soil_avg,
                   MIN(soil_moisture) AS soil_min,
                   MAX(soil_moisture) AS soil_max,
                   AVG(CASE WHEN light_unit = 'lux' THEN light END) AS light_avg,
                   MIN(CASE WHEN light_unit = 'lux' THEN light END) AS light_min,
                   MAX(CASE WHEN light_unit = 'lux' THEN light END) AS light_max,
                   SUM(CASE WHEN sensor_errors IS NOT NULL
                                  AND sensor_errors NOT IN ('', '[]')
                            THEN 1 ELSE 0 END) AS error_samples
            FROM sensor_data
            WHERE device_id = ? AND server_received_at >= ? AND server_received_at < ?
            """,
            (
                device_id,
                start.strftime("%Y-%m-%d %H:%M:%S"),
                end.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        ).fetchone()
        events = conn.execute(
            """
            SELECT status, severity, event_type, COUNT(*) AS count
            FROM event_log
            WHERE device_id = ? AND created_at >= ? AND created_at < ?
            GROUP BY status, severity, event_type
            """,
            (
                device_id,
                start.strftime("%Y-%m-%d %H:%M:%S"),
                end.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        ).fetchall()
    finally:
        conn.close()

    def metric_line(label, prefix, unit):
        avg = row[f"{prefix}_avg"]
        low = row[f"{prefix}_min"]
        high = row[f"{prefix}_max"]
        if avg is None:
            return f"{label}: 데이터 없음"
        return (
            f"{label}: 평균 {_format_number(avg)}{unit} "
            f"(최저 {_format_number(low)} / 최고 {_format_number(high)}{unit})"
        )

    abnormal = sum(int(item["count"]) for item in events if item["status"] == "abnormal")
    recovered = sum(int(item["count"]) for item in events if item["status"] == "recovered")
    danger = sum(
        int(item["count"])
        for item in events
        if item["status"] == "abnormal" and item["severity"] == "danger"
    )
    offline = sum(
        int(item["count"])
        for item in events
        if item["status"] == "abnormal" and item["event_type"] == "device_offline"
    )
    title = "일간" if summary_type == "daily" else "주간"
    return "\n".join([
        f"[스마트팜 {title} 요약]",
        f"장치: {device_id}",
        f"기간: {start:%Y-%m-%d} ~ {(end - timedelta(seconds=1)):%Y-%m-%d %H:%M}",
        f"수집: {int(row['samples'] or 0):,}건 · 센서 오류 포함 {int(row['error_samples'] or 0):,}건",
        metric_line("온도", "temperature", "°C"),
        metric_line("습도", "humidity", "%"),
        metric_line("토양 수분", "soil", "%"),
        metric_line("조도", "light", " lux"),
        f"알림: 이상 {abnormal}건(위험 {danger}건) · 복구 {recovered}건 · 오프라인 {offline}건",
    ])


def queue_due_summaries(now=None):
    """가장 최근 발송 시각이 지난 일간·주간 요약을 장치별 한 번만 큐에 넣는다."""
    now = now or datetime.now()
    conn = _connect()
    queued = 0
    try:
        _ensure_tables(conn)
        devices = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT device_id FROM sensor_data WHERE device_id != ''"
            ).fetchall()
        }
        devices.update(
            row[0]
            for row in conn.execute(
                "SELECT device_id FROM alert_settings WHERE device_id != ?",
                (GLOBAL_SETTINGS_ID,),
            ).fetchall()
        )
        try:
            try:
                from services.device_service import list_devices
            except ModuleNotFoundError:
                from app.services.device_service import list_devices
            devices.update(item["device_id"] for item in list_devices(devices))
        except Exception as e:
            # 설정 DB 장애가 센서 DB에 있는 장치의 요약까지 막지는 않게 한다.
            print(f"[Alert] Registered devices omitted from summary: {e}")

        for device_id in sorted(devices):
            settings = _resolve_settings(conn, device_id)
            for summary_type, enabled_field in (
                ("daily", "daily_summary"), ("weekly", "weekly_summary")
            ):
                if not settings.get(enabled_field):
                    continue
                scheduled, start, end = _scheduled_period(now, summary_type, settings)
                if scheduled > now:
                    continue
                period_key = end.strftime("%Y-%m-%d")
                exists = conn.execute(
                    """
                    SELECT 1 FROM summary_delivery
                    WHERE device_id = ? AND summary_type = ? AND period_key = ?
                    """,
                    (device_id, summary_type, period_key),
                ).fetchone()
                if exists is not None:
                    continue
                message = build_summary_message(device_id, summary_type, start, end)
                inserted = conn.execute(
                    """
                    INSERT OR IGNORE INTO summary_delivery
                        (device_id, summary_type, period_key, queued_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (device_id, summary_type, period_key, _now_string()),
                ).rowcount
                if not inserted:
                    continue
                _enqueue_notification(conn, device_id, message)
                queued += 1
                # 발송 이력과 outbox를 함께 확정하고 다음 요약 조회 전에 쓰기 잠금을 푼다.
                conn.commit()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return queued


def _offline_check_seconds():
    raw = os.environ.get(
        "DEVICE_OFFLINE_CHECK_SECONDS", str(DEFAULT_OFFLINE_CHECK_SECONDS)
    )
    try:
        seconds = finite_number(raw)
    except (TypeError, ValueError):
        return DEFAULT_OFFLINE_CHECK_SECONDS
    # 너무 짧은 주기는 SQLite 접근만 늘리고 얻는 것이 없다.
    return max(5, seconds)


_watchdog_started = False
_watchdog_lock = threading.Lock()
_watchdog_last_run_at = None


def delivery_health():
    conn = _connect()
    try:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM notification_outbox "
            "WHERE status != 'sent' GROUP BY status"
        ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
    finally:
        conn.close()
    return {
        "watchdog_started": _watchdog_started,
        "watchdog_last_run_at": _watchdog_last_run_at,
        "notifications_pending": counts.get("pending", 0) + counts.get("sending", 0),
        "notifications_failed": counts.get("failed", 0),
    }


def start_offline_watchdog():
    """오프라인 감시 스레드를 프로세스당 한 번만 기동한다.

    Flask 개발 서버는 reloader 때문에 모듈이 두 프로세스에서 실행된다.
    중복 기동을 막기 위해 전역 플래그와 lock으로 한 번만 시작하도록 보장한다.
    데몬 스레드이므로 서버를 종료하면 함께 정리된다.
    """
    global _watchdog_started

    with _watchdog_lock:
        if _watchdog_started:
            return False
        _watchdog_started = True

    interval = _offline_check_seconds()

    def _run():
        global _watchdog_last_run_at
        # 기동 직후에는 아직 첫 센서 데이터가 없을 수 있어 한 주기 쉬고 시작한다.
        stop = threading.Event()
        while not stop.wait(interval):
            try:
                check_device_offline()
                queue_due_summaries()
                deliver_pending_notifications()
                _watchdog_last_run_at = _now_string()
            except Exception as e:
                # 감시 스레드는 어떤 예외에도 죽으면 안 된다.
                # 죽으면 그 이후 오프라인 감지가 조용히 사라지기 때문이다.
                print(f"[Alert] Offline watchdog iteration failed: {e}")

    thread = threading.Thread(target=_run, name="offline-watchdog", daemon=True)
    thread.start()
    print(f"[Alert] Offline watchdog started (every {interval:.0f}s)")
    return True
