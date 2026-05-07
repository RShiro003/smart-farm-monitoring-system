from flask import Blueprint, render_template, request, jsonify
from datetime import datetime, timedelta
from collections import defaultdict
try:
    from services.sensor_service import (
        filter_sensor_data,
        latest_sensor_record,
        list_device_ids,
        load_sensor_data,
        normalize_device_filter,
        parse_record_time,
        sensor_data_mtime,
    )
except ModuleNotFoundError:
    from app.services.sensor_service import (
        filter_sensor_data,
        latest_sensor_record,
        list_device_ids,
        load_sensor_data,
        normalize_device_filter,
        parse_record_time,
        sensor_data_mtime,
    )

dashboard_bp = Blueprint("dashboard", __name__)


def _selected_device_id():
    return normalize_device_filter(request.args.get("device_id"))


def _load_data(device_id=None):
    return filter_sensor_data(load_sensor_data(), device_id)


def _device_options(all_data, selected_device_id):
    devices = list_device_ids(all_data)
    if selected_device_id and selected_device_id not in devices:
        devices.append(selected_device_id)
    return sorted(devices)


def _parse_time(row):
    return parse_record_time(row)


def _avg(rows, key, alt_key=None):
    vals = []
    for r in rows:
        v = r.get(key)
        if v is None and alt_key:
            v = r.get(alt_key)
        if v is not None:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
    return round(sum(vals) / len(vals), 1) if vals else None


def _calculate_averages(data, period):
    now = datetime.now()
    delta = {
        "daily": timedelta(days=1),
        "weekly": timedelta(weeks=1),
        "monthly": timedelta(days=30),
    }
    cutoff = now - delta.get(period, timedelta(days=1))

    filtered = []
    for r in data:
        t = _parse_time(r)
        if t and t >= cutoff:
            filtered.append(r)

    return {
        "temperature": _avg(filtered, "temperature"),
        "humidity": _avg(filtered, "humidity"),
        "soil_moisture": _avg(filtered, "soil_moisture"),
        "light": _avg(filtered, "light", "light_digital"),
        "count": len(filtered),
    }


def _aggregate_chart(data, period):
    now = datetime.now()

    if period == "hourly":
        cutoff = now - timedelta(hours=24)
        def key_fn(t): return t.strftime("%m/%d %H:00")
    elif period == "daily":
        cutoff = now - timedelta(days=7)
        def key_fn(t): return t.strftime("%m/%d")
    elif period == "weekly":
        cutoff = now - timedelta(weeks=8)
        def key_fn(t):
            start = t - timedelta(days=t.weekday())
            return start.strftime("%m/%d~")
    else:  # monthly
        cutoff = now - timedelta(days=365)
        def key_fn(t): return t.strftime("%Y/%m")

    groups = defaultdict(list)
    for row in data:
        t = _parse_time(row)
        if t and t >= cutoff:
            groups[key_fn(t)].append(row)

    labels = sorted(groups.keys())
    return {
        "labels": labels,
        "temperature": [_avg(groups[l], "temperature") for l in labels],
        "humidity": [_avg(groups[l], "humidity") for l in labels],
        "soil_moisture": [_avg(groups[l], "soil_moisture") for l in labels],
        "light": [_avg(groups[l], "light", "light_digital") for l in labels],
    }


@dashboard_bp.route("/dashboard")
def dashboard():
    selected_device_id = _selected_device_id()
    all_data = load_sensor_data()
    data = filter_sensor_data(all_data, selected_device_id)
    latest = latest_sensor_record(data)
    return render_template(
        "index.html",
        latest=latest,
        devices=_device_options(all_data, selected_device_id),
        current_device_id=selected_device_id,
    )


@dashboard_bp.route("/api/dashboard/stats")
def dashboard_stats():
    period = request.args.get("period", "daily")
    data = _load_data(_selected_device_id())
    return jsonify(_calculate_averages(data, period))


@dashboard_bp.route("/api/dashboard/chart")
def dashboard_chart():
    period = request.args.get("period", "hourly")
    data = _load_data(_selected_device_id())
    return jsonify(_aggregate_chart(data, period))


@dashboard_bp.route("/api/dashboard/latest")
def dashboard_latest():
    data = _load_data(_selected_device_id())
    return jsonify(latest_sensor_record(data))


@dashboard_bp.route("/api/dashboard/history")
def dashboard_history():
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = max(1, int(request.args.get("per_page", 10)))
    except ValueError:
        page, per_page = 1, 10

    date_str  = request.args.get("date", "").strip()       # YYYY-MM-DD
    time_from = request.args.get("time_from", "").strip()  # HH:MM
    time_to   = request.args.get("time_to",   "").strip()  # HH:MM

    data = list(reversed(_load_data(_selected_device_id())))  # newest first

    filtered = []
    has_filter = bool(date_str or time_from or time_to)
    for row in data:
        t = _parse_time(row)
        if t is None:
            if not has_filter:
                filtered.append(row)
            continue

        if date_str and t.strftime("%Y-%m-%d") != date_str:
            continue
        if time_from:
            try:
                tf = datetime.strptime(time_from, "%H:%M").time()
                if t.time() < tf:
                    continue
            except ValueError:
                pass
        if time_to:
            try:
                tt = datetime.strptime(time_to, "%H:%M").time()
                if t.time() > tt:
                    continue
            except ValueError:
                pass
        filtered.append(row)

    total  = len(filtered)
    pages  = max(1, (total + per_page - 1) // per_page)
    page   = min(page, pages)
    start  = (page - 1) * per_page
    items  = filtered[start:start + per_page]

    return jsonify({
        "items": items,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "total": total,
    })


@dashboard_bp.route("/api/dashboard/mtime")
def dashboard_mtime():
    return jsonify({"mtime": sensor_data_mtime()})
