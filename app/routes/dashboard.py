from collections import defaultdict
from datetime import timedelta
from time import perf_counter

from flask import Blueprint, jsonify, render_template, request

try:
    from services.sensor_service import (
        count_sensor_history,
        get_latest_sensor_record,
        get_sensor_history,
        get_sensor_rows_for_chart,
        get_sensor_stats,
        list_device_ids_from_db,
        normalize_device_filter,
        parse_record_time,
        sensor_data_mtime,
    )
except ModuleNotFoundError:
    from app.services.sensor_service import (
        count_sensor_history,
        get_latest_sensor_record,
        get_sensor_history,
        get_sensor_rows_for_chart,
        get_sensor_stats,
        list_device_ids_from_db,
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

    return {
        "labels": labels,
        "temperature": [_avg(groups[label], "temperature") for label in labels],
        "humidity": [_avg(groups[label], "humidity") for label in labels],
        "soil_moisture": [_avg(groups[label], "soil_moisture") for label in labels],
        "light": [_avg(groups[label], "light", "light_digital") for label in labels],
    }


@dashboard_bp.route("/dashboard")
def dashboard():
    selected_device_id = _selected_device_id()
    latest = get_latest_sensor_record(selected_device_id)
    return render_template(
        "index.html",
        latest=latest,
        devices=_device_options(selected_device_id),
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
