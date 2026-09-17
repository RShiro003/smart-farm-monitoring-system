"""장치 관리, 알림 설정, CSV 내보내기, 저장소 유지보수 API.

대시보드의 "설정/관리" 성격 엔드포인트를 한 Blueprint에 모은다.
센서 수집(sensor.py), 조회(dashboard.py), 생육/관수(cultivation.py)와
목적이 다르므로 파일을 분리해 어떤 라우트가 어디에 있는지 유지한다.
"""
import re
import threading
import time
from datetime import datetime
from flask import Blueprint, Response, jsonify, make_response, request
from .query_filters import event_filters

try:
    from services.validation import finite_number
    from services import device_service, export_service, retention_service, work_log_service
    from services.discord_alert_service import discord_webhook_url_is_allowed
    from services.alert_service import (
        ALERT_TOGGLE_FIELDS,
        ALERT_TOGGLE_LABELS,
        ALERT_RULE_BOOLEAN_FIELDS,
        GLOBAL_SETTINGS_ID,
        delete_alert_settings,
        get_alert_settings,
        list_configured_devices,
        save_alert_settings,
    )
    from services.auth_service import (
        AUTH_COOKIE_NAME,
        auth_enabled,
        auth_status,
        key_is_valid,
        session_cookie_value,
    )
    from services.sensor_service import list_device_ids_from_db, normalize_device_filter
except ModuleNotFoundError:
    from app.services.validation import finite_number
    from app.services import device_service, export_service, retention_service, work_log_service
    from app.services.discord_alert_service import discord_webhook_url_is_allowed
    from app.services.alert_service import (
        ALERT_TOGGLE_FIELDS,
        ALERT_TOGGLE_LABELS,
        ALERT_RULE_BOOLEAN_FIELDS,
        GLOBAL_SETTINGS_ID,
        delete_alert_settings,
        get_alert_settings,
        list_configured_devices,
        save_alert_settings,
    )
    from app.services.auth_service import (
        AUTH_COOKIE_NAME,
        auth_enabled,
        auth_status,
        key_is_valid,
        session_cookie_value,
    )
    from app.services.sensor_service import (
        list_device_ids_from_db,
        normalize_device_filter,
    )


management_bp = Blueprint("management", __name__)
_login_attempts = {}
_login_lock = threading.Lock()
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_FAILURES = 5


def _login_rate_limited(client):
    now = time.monotonic()
    with _login_lock:
        attempts = [
            stamp for stamp in _login_attempts.get(client, [])
            if now - stamp < _LOGIN_WINDOW_SECONDS
        ]
        _login_attempts[client] = attempts
        return len(attempts) >= _LOGIN_MAX_FAILURES


def _record_login_failure(client):
    with _login_lock:
        _login_attempts.setdefault(client, []).append(time.monotonic())


def _required_device_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


# ── 장치 관리 ──────────────────────────────────────────────────────────────────

@management_bp.route("/api/devices", methods=["GET"])
def get_devices():
    # 등록된 장치와 센서 데이터에만 있는 장치를 합쳐서 보여준다.
    return jsonify({
        "devices": device_service.list_devices(list_device_ids_from_db()),
    })


@management_bp.route("/api/devices", methods=["POST", "PUT"])
def save_device():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "No JSON received"}), 400

    device_id = _required_device_id(payload.get("device_id"))
    if device_id is None:
        return jsonify({"error": "device_id is required"}), 400
    if len(device_id) > 64 or any(ord(ch) < 32 for ch in device_id):
        return jsonify({"error": "device_id must be 1-64 printable characters"}), 400

    errors = {}
    for field in ("label", "location", "note"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            errors[field] = "must be a string"
    if errors:
        return jsonify({"error": "Invalid device data", "details": errors}), 400

    device = device_service.upsert_device(
        device_id,
        label=payload.get("label"),
        location=payload.get("location"),
        note=payload.get("note"),
    )
    return jsonify(device)


@management_bp.route("/api/devices/<device_id>", methods=["DELETE"])
def remove_device(device_id):
    # 등록 정보만 지운다. 센서 기록은 남으므로 목록에는 미등록 장치로 다시 나타난다.
    if not device_service.delete_device(device_id):
        return jsonify({"error": "Device not found"}), 404
    return jsonify({"ok": True, "device_id": device_id})


# ── 알림 설정 ──────────────────────────────────────────────────────────────────

@management_bp.route("/api/alert-settings", methods=["GET"])
def get_alert_settings_route():
    # device_id가 없으면 전역 기본 설정을 돌려준다.
    device_id = (request.args.get("device_id") or "").strip()
    settings = get_alert_settings(device_id)
    public_settings = dict(settings)
    public_settings["webhook_configured"] = bool(public_settings.get("webhook_url"))
    public_settings.pop("webhook_url", None)
    return jsonify({
        "settings": public_settings,
        "fields": [
            {"key": key, "label": ALERT_TOGGLE_LABELS[key]}
            for key in ALERT_TOGGLE_FIELDS
        ],
        "global_id": GLOBAL_SETTINGS_ID,
        "configured_devices": list_configured_devices(),
    })


@management_bp.route("/api/alert-settings", methods=["POST", "PUT"])
def save_alert_settings_route():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "No JSON received"}), 400

    errors = {}
    values = {}

    for field in (*ALERT_TOGGLE_FIELDS, *ALERT_RULE_BOOLEAN_FIELDS):
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, bool):
            errors[field] = "must be a boolean"
            continue
        values[field] = value

    integer_rules = {
        "abnormal_count": (1, 100),
        "recovery_count": (1, 100),
        "abnormal_duration_seconds": (0, 86400),
        "summary_hour": (0, 23),
        "summary_weekday": (0, 6),
    }
    for field, (minimum, maximum) in integer_rules.items():
        if field not in payload:
            continue
        value = payload[field]
        if isinstance(value, bool):
            errors[field] = "must be an integer"
            continue
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            errors[field] = "must be an integer"
            continue
        if number < minimum or number > maximum:
            errors[field] = f"must be between {minimum} and {maximum}"
        else:
            values[field] = number

    if "danger_deviation_percent" in payload:
        value = payload["danger_deviation_percent"]
        if isinstance(value, bool):
            errors["danger_deviation_percent"] = "must be a number"
        else:
            try:
                number = finite_number(value)
            except (TypeError, ValueError):
                errors["danger_deviation_percent"] = "must be a number"
            else:
                if number < 0 or number > 1000:
                    errors["danger_deviation_percent"] = "must be between 0 and 1000"
                else:
                    values["danger_deviation_percent"] = number

    if "cooldown_minutes" in payload:
        value = payload["cooldown_minutes"]
        if value is None:
            # None은 "환경변수 기본값을 따른다"는 의미로 허용한다.
            values["cooldown_minutes"] = None
        elif isinstance(value, bool):
            errors["cooldown_minutes"] = "must be a number"
        else:
            try:
                number = finite_number(value)
            except (TypeError, ValueError):
                errors["cooldown_minutes"] = "must be a number"
            else:
                if number < 0 or number > 1440:
                    errors["cooldown_minutes"] = "must be between 0 and 1440"
                else:
                    values["cooldown_minutes"] = number

    if "webhook_url" in payload:
        value = payload["webhook_url"]
        if value is None or (isinstance(value, str) and not value.strip()):
            values["webhook_url"] = None
        elif not isinstance(value, str):
            errors["webhook_url"] = "must be a string"
        elif not discord_webhook_url_is_allowed(value.strip()):
            errors["webhook_url"] = "must be a Discord HTTPS webhook URL"
        else:
            values["webhook_url"] = value.strip()

    if errors:
        return jsonify({"error": "Invalid alert settings", "details": errors}), 400

    device_id = payload.get("device_id")
    if device_id is None:
        device_id = ""
    if not isinstance(device_id, str):
        return jsonify({"error": "device_id must be a string"}), 400
    device_id = device_id.strip()
    return jsonify(save_alert_settings(device_id, values))


@management_bp.route("/api/alert-settings/<device_id>", methods=["DELETE"])
def reset_alert_settings(device_id):
    # 장치별 예외를 지워 전역 기본값으로 되돌린다.
    if not delete_alert_settings(device_id):
        return jsonify({"error": "No device-specific settings"}), 404
    return jsonify({"ok": True, "device_id": device_id})


# ── 사용자 작업 기록 ────────────────────────────────────────────────────────────

@management_bp.route("/api/work-logs", methods=["GET"])
def get_work_logs():
    device_id = (request.args.get("device_id") or "").strip() or None
    start_at = (request.args.get("start_at") or "").strip() or None
    end_at = (request.args.get("end_at") or "").strip() or None
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400
    return jsonify({
        "items": work_log_service.list_work_logs(
            device_id=device_id, start_at=start_at, end_at=end_at, limit=limit
        ),
        "types": [
            {"key": key, "label": label}
            for key, label in work_log_service.WORK_TYPES.items()
        ],
    })


@management_bp.route("/api/work-logs", methods=["POST"])
def create_work_log():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "No JSON received"}), 400

    errors = {}
    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", device_id.strip()
    ):
        errors["device_id"] = "must be a valid 1-96 character device id"
    else:
        device_id = device_id.strip()

    work_type = payload.get("work_type")
    if not isinstance(work_type, str) or work_type not in work_log_service.WORK_TYPES:
        errors["work_type"] = (
            "must be one of " + ", ".join(work_log_service.WORK_TYPES)
        )

    note = payload.get("note", "")
    if not isinstance(note, str):
        errors["note"] = "must be a string"
    elif len(note.strip()) > 500:
        errors["note"] = "must be at most 500 characters"
    else:
        note = note.strip()

    occurred_at = payload.get("occurred_at")
    if occurred_at in (None, ""):
        occurred_at = None
    elif not isinstance(occurred_at, str):
        errors["occurred_at"] = "must be an ISO date-time string"
    else:
        try:
            parsed = datetime.fromisoformat(occurred_at.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            occurred_at = parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            errors["occurred_at"] = "must be an ISO date-time string"

    if errors:
        return jsonify({"error": "Invalid work log", "details": errors}), 400
    return jsonify(work_log_service.create_work_log(
        device_id, work_type, note, occurred_at
    )), 201


@management_bp.route("/api/work-logs/<int:entry_id>", methods=["DELETE"])
def remove_work_log(entry_id):
    if not work_log_service.delete_work_log(entry_id):
        return jsonify({"error": "Work log not found"}), 404
    return jsonify({"ok": True, "id": entry_id})


# ── CSV 내보내기 ───────────────────────────────────────────────────────────────

def _csv_response(generator, filename):
    # Content-Disposition을 붙여야 브라우저가 파일로 저장한다.
    return Response(
        generator,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@management_bp.route("/api/export/sensor.csv", methods=["GET"])
def export_sensor_csv():
    device_id = normalize_device_filter(request.args.get("device_id"))
    labels = device_service.device_labels(list_device_ids_from_db())
    generator = export_service.stream_sensor_csv(
        device_id,
        (request.args.get("date") or "").strip(),
        (request.args.get("time_from") or "").strip(),
        (request.args.get("time_to") or "").strip(),
        labels=labels,
    )
    return _csv_response(
        generator, export_service.export_filename("sensor", device_id)
    )


@management_bp.route("/api/export/events.csv", methods=["GET"])
def export_events_csv():
    device_id = normalize_device_filter(request.args.get("device_id"))
    labels = device_service.device_labels(list_device_ids_from_db())
    generator = export_service.stream_events_csv(
        device_id,
        labels=labels,
        **event_filters(request.args),
    )
    return _csv_response(
        generator, export_service.export_filename("events", device_id)
    )


# ── 저장소 유지보수 ────────────────────────────────────────────────────────────

@management_bp.route("/api/maintenance/storage", methods=["GET"])
def get_storage_stats():
    return jsonify(retention_service.storage_stats())


@management_bp.route("/api/maintenance/rollup", methods=["POST"])
def run_rollup():
    # 삭제 없이 집계만 수행한다. 보존 정책을 적용하기 전에 안전하게 시험할 수 있다.
    return jsonify({"rolled_up_buckets": retention_service.rollup_hourly()})


@management_bp.route("/api/maintenance/retention", methods=["POST"])
def run_retention():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"error": "No JSON received"}), 400
    errors = {}

    def _optional_days(key):
        if key not in payload or payload[key] is None:
            return None
        value = payload[key]
        if isinstance(value, bool):
            errors[key] = "must be an integer"
            return None
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            errors[key] = "must be an integer"
            return None
        if number < 1 or number > 3650:
            errors[key] = "must be between 1 and 3650"
            return None
        return number

    raw_days = _optional_days("raw_days")
    hourly_days = _optional_days("hourly_days")
    vacuum = bool(payload.get("vacuum", False))

    if errors:
        return jsonify({"error": "Invalid retention request", "details": errors}), 400

    return jsonify(
        retention_service.run_maintenance(raw_days, hourly_days, vacuum=vacuum)
    )


# ── 인증 상태 ──────────────────────────────────────────────────────────────────

@management_bp.route("/api/auth/status", methods=["GET"])
def get_auth_status():
    # 키 값 자체는 절대 내려보내지 않는다. 켜져 있는지와 헤더 이름만 알려준다.
    return jsonify(auth_status())


@management_bp.route("/api/auth/login", methods=["POST"])
def login():
    if not auth_enabled():
        return jsonify({"ok": True, "enabled": False})

    client = request.remote_addr or "unknown"
    if _login_rate_limited(client):
        return jsonify({"error": "Too many login attempts"}), 429

    payload = request.get_json(silent=True)
    candidate = payload.get("api_key") if isinstance(payload, dict) else None
    if not isinstance(candidate, str) or not key_is_valid(candidate.strip()):
        _record_login_failure(client)
        return jsonify({"error": "Invalid API key"}), 401

    with _login_lock:
        _login_attempts.pop(client, None)

    response = make_response(jsonify({"ok": True, "enabled": True}))
    response.set_cookie(
        AUTH_COOKIE_NAME,
        session_cookie_value(),
        httponly=True,
        secure=request.is_secure,
        samesite="Strict",
        max_age=12 * 60 * 60,
        path="/",
    )
    return response


@management_bp.route("/api/auth/logout", methods=["POST"])
def logout():
    response = make_response(jsonify({"ok": True}))
    response.delete_cookie(AUTH_COOKIE_NAME, path="/", samesite="Strict")
    return response
