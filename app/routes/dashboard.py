from collections import defaultdict
from datetime import timedelta
from time import perf_counter

from flask import Blueprint, jsonify, render_template, request

try:
    from services.auth_service import auth_enabled, protect_reads, request_is_authorized
    from services.alert_service import (
        count_active_alerts,
        count_events,
        list_events,
    )
    from services.sensor_service import (
        count_sensor_history,
        get_latest_sensor_record,
        get_sensor_history,
        get_sensor_rows_for_chart,
        get_sensor_stats,
        list_device_ids_from_db,
        list_device_status,
        normalize_device_filter,
        parse_record_time,
        sensor_data_mtime,
    )
except ModuleNotFoundError:
    from app.services.auth_service import (
        auth_enabled,
        protect_reads,
        request_is_authorized,
    )
    from app.services.alert_service import (
        count_active_alerts,
        count_events,
        list_events,
    )
    from app.services.sensor_service import (
        count_sensor_history,
        get_latest_sensor_record,
        get_sensor_history,
        get_sensor_rows_for_chart,
        get_sensor_stats,
        list_device_ids_from_db,
        list_device_status,
        normalize_device_filter,
        parse_record_time,
        sensor_data_mtime,
    )


dashboard_bp = Blueprint("dashboard", __name__)


def _selected_device_id():
    return normalize_device_filter(request.args.get("device_id"))


def _positive_int(value, default, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    number = max(1, number)
    if maximum is not None:
        number = min(number, maximum)
    return number


def _log_api(name, start, **details):
    suffix = "".join(f" {key}={value}" for key, value in details.items())
    print(f"[dashboard.{name}] {perf_counter() - start:.3f}s{suffix}")


def _device_options(selected_device_id):
    devices = list_device_ids_from_db()
    if selected_device_id and selected_device_id not in devices:
        devices.append(selected_device_id)
    return sorted(devices)


def _parse_time(row):
    return parse_record_time(row)


def _avg(rows, key, alt_key=None):
    vals = []
    for row in rows:
        value = row.get(key)
        if value is None and alt_key:
            value = row.get(alt_key)
        if value is not None:
            try:
                vals.append(float(value))
            except (TypeError, ValueError):
                pass
    return round(sum(vals) / len(vals), 1) if vals else None


def _aggregate_chart(rows, period):
    if period == "hourly":
        def key_fn(t):
            return t.strftime("%m/%d %H:00")
    elif period == "daily":
        def key_fn(t):
            return t.strftime("%m/%d")
    elif period == "weekly":
        def key_fn(t):
            start = t - timedelta(days=t.weekday())
            return start.strftime("%m/%d~")
    else:
        def key_fn(t):
            return t.strftime("%Y/%m")

    labels = []
    groups = defaultdict(list)
    for row in rows:
        parsed_at = _parse_time(row)
        if not parsed_at:
            continue
        label = key_fn(parsed_at)
        if label not in groups:
            labels.append(label)
        groups[label].append(row)

    lux_count = sum(int(row.get("lux_count") or 0) for row in rows)
    digital_count = sum(int(row.get("digital_count") or 0) for row in rows)
    if lux_count and digital_count:
        light_mode = "mixed"
    elif lux_count:
        light_mode = "lux"
    elif digital_count:
        light_mode = "digital"
    else:
        light_mode = "none"

    return {
        "labels": labels,
        "temperature": [_avg(groups[label], "temperature") for label in labels],
        "humidity": [_avg(groups[label], "humidity") for label in labels],
        "soil_moisture": [_avg(groups[label], "soil_moisture") for label in labels],
        "light": [_avg(groups[label], "light") for label in labels],
        "light_mode": light_mode,
        "light_count": lux_count,
        "digital_light_count": digital_count,
    }


@dashboard_bp.route("/dashboard")
def dashboard():
    selected_device_id = _selected_device_id()
    may_embed_data = not (auth_enabled() and protect_reads()) or request_is_authorized()
    latest = get_latest_sensor_record(selected_device_id) if may_embed_data else None
    devices = _device_options(selected_device_id) if may_embed_data else []
    return render_template(
        "index.html",
        latest=latest,
        devices=devices,
        current_device_id=selected_device_id,
    )


@dashboard_bp.route("/api/dashboard/stats")
def dashboard_stats():
    start = perf_counter()
    period = request.args.get("period", "daily")
    result = get_sensor_stats(_selected_device_id(), period)
    _log_api("stats", start, period=period, count=result.get("count", 0))
    return jsonify(result)


@dashboard_bp.route("/api/dashboard/chart")
def dashboard_chart():
    start = perf_counter()
    period = request.args.get("period", "hourly")
    limit = _positive_int(request.args.get("limit"), 300, maximum=5000)
    rows = get_sensor_rows_for_chart(_selected_device_id(), period, limit)
    result = _aggregate_chart(rows, period)
    _log_api("chart", start, period=period, rows=len(rows))
    return jsonify(result)


@dashboard_bp.route("/api/dashboard/latest")
def dashboard_latest():
    start = perf_counter()
    latest = get_latest_sensor_record(_selected_device_id())
    _log_api("latest", start)
    return jsonify(latest)


@dashboard_bp.route("/api/dashboard/history")
def dashboard_history():
    start = perf_counter()
    page = _positive_int(request.args.get("page"), 1)
    per_page = _positive_int(request.args.get("per_page"), 10, maximum=200)
    date_str = request.args.get("date", "").strip()
    time_from = request.args.get("time_from", "").strip()
    time_to = request.args.get("time_to", "").strip()
    device_id = _selected_device_id()

    total = count_sensor_history(device_id, date_str, time_from, time_to)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    items = get_sensor_history(device_id, page, per_page, date_str, time_from, time_to)

    _log_api("history", start, total=total, page=page)
    return jsonify({
        "items": items,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "total": total,
    })


@dashboard_bp.route("/api/dashboard/mtime")
def dashboard_mtime():
    start = perf_counter()
    mtime = sensor_data_mtime()
    _log_api("mtime", start)
    return jsonify({"mtime": mtime})


@dashboard_bp.route("/api/dashboard/devices")
def dashboard_devices():
    start = perf_counter()
    selected_device_id = _selected_device_id()
    devices = _device_options(selected_device_id)
    _log_api("devices", start, count=len(devices))
    return jsonify({
        "devices": devices,
        "current_device_id": selected_device_id,
    })


@dashboard_bp.route("/api/dashboard/device-status")
def dashboard_device_status():
    # 장치별 마지막 수신 시각과 온라인/오프라인 상태를 내려준다.
    # 대시보드 헤더 배지가 이 응답으로 갱신된다.
    start = perf_counter()
    device_id = _selected_device_id()
    statuses = list_device_status(device_id)

    # 특정 장치를 보고 있으면 그 장치의 상태를 요약으로 함께 내려 화면에서 바로 쓰게 한다.
    current = None
    if device_id:
        current = next(
            (entry for entry in statuses if entry["device_id"] == device_id),
            # 아직 한 건도 수신하지 않은 장치는 집계 결과에 나오지 않는다.
            {
                "device_id": device_id,
                "last_seen_at": None,
                "age_seconds": None,
                "status": "unknown",
                "total": 0,
            },
        )

    offline = [entry for entry in statuses if entry["status"] == "offline"]
    _log_api("device-status", start, count=len(statuses), offline=len(offline))
    return jsonify({
        "devices": statuses,
        "current": current,
        "offline_count": len(offline),
    })


@dashboard_bp.route("/api/events")
@dashboard_bp.route("/api/dashboard/events")
def dashboard_events():
    # event_log에 쌓인 이상/복구 알림 기록을 페이지 단위로 조회한다.
    # 지금까지는 Discord로 한 번 보내고 끝이라 서버에만 남아 있던 데이터다.
    start = perf_counter()
    page = _positive_int(request.args.get("page"), 1)
    per_page = _positive_int(request.args.get("per_page"), 10, maximum=200)
    status = (request.args.get("status") or "").strip() or None
    metric = (request.args.get("metric") or "").strip() or None
    date_str = (request.args.get("date") or "").strip()
    time_from = (request.args.get("time_from") or "").strip()
    time_to = (request.args.get("time_to") or "").strip()
    device_id = _selected_device_id()

    total = count_events(
        device_id, status, metric, date_str, time_from, time_to
    )
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    items = list_events(
        device_id,
        page,
        per_page,
        status,
        metric,
        date_str,
        time_from,
        time_to,
    )

    _log_api("events", start, total=total, page=page)
    return jsonify({
        "items": items,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "total": total,
        "active": count_active_alerts(device_id),
    })
