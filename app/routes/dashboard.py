from flask import Blueprint, render_template, request, jsonify
import json
import os
from datetime import datetime, timedelta
from collections import defaultdict

dashboard_bp = Blueprint("dashboard", __name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FARMS = [
    {"id": "farm_1", "name": "농장 1"},
    {"id": "farm_2", "name": "농장 2"},
    {"id": "farm_3", "name": "농장 3"},
]
_FARM_IDS = {f["id"] for f in FARMS}


def _data_file(farm_id):
    if farm_id == "farm_1":
        return os.path.normpath(os.path.join(_BASE_DIR, "..", "data", "sensor_data.json"))
    return os.path.normpath(os.path.join(_BASE_DIR, "..", "data", f"{farm_id}_sensor_data.json"))


def _load_data(farm_id):
    try:
        with open(_data_file(farm_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _parse_time(row):
    ts = row.get("server_received_at") or row.get("time") or row.get("timestamp")
    if not ts or ts == "time_not_set":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


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
    farm_id = request.args.get("farm", FARMS[0]["id"])
    if farm_id not in _FARM_IDS:
        farm_id = FARMS[0]["id"]
    data = _load_data(farm_id)
    latest = data[-1] if data else None
    return render_template(
        "index.html",
        latest=latest,
        farms=FARMS,
        current_farm=farm_id,
    )


@dashboard_bp.route("/api/dashboard/stats")
def dashboard_stats():
    farm_id = request.args.get("farm", FARMS[0]["id"])
    period = request.args.get("period", "daily")
    if farm_id not in _FARM_IDS:
        farm_id = FARMS[0]["id"]
    data = _load_data(farm_id)
    return jsonify(_calculate_averages(data, period))


@dashboard_bp.route("/api/dashboard/chart")
def dashboard_chart():
    farm_id = request.args.get("farm", FARMS[0]["id"])
    period = request.args.get("period", "hourly")
    if farm_id not in _FARM_IDS:
        farm_id = FARMS[0]["id"]
    data = _load_data(farm_id)
    return jsonify(_aggregate_chart(data, period))


@dashboard_bp.route("/api/dashboard/latest")
def dashboard_latest():
    farm_id = request.args.get("farm", FARMS[0]["id"])
    if farm_id not in _FARM_IDS:
        farm_id = FARMS[0]["id"]
    data = _load_data(farm_id)
    return jsonify(data[-1] if data else None)


@dashboard_bp.route("/api/dashboard/history")
def dashboard_history():
    farm_id = request.args.get("farm", FARMS[0]["id"])
    if farm_id not in _FARM_IDS:
        farm_id = FARMS[0]["id"]

    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = max(1, int(request.args.get("per_page", 10)))
    except ValueError:
        page, per_page = 1, 10

    date_str  = request.args.get("date", "").strip()       # YYYY-MM-DD
    time_from = request.args.get("time_from", "").strip()  # HH:MM
    time_to   = request.args.get("time_to",   "").strip()  # HH:MM

    data = list(reversed(_load_data(farm_id)))  # newest first

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

    return jsonify({"items": items, "page": page, "pages": pages, "total": total})


@dashboard_bp.route("/api/dashboard/mtime")
def dashboard_mtime():
    farm_id = request.args.get("farm", FARMS[0]["id"])
    if farm_id not in _FARM_IDS:
        farm_id = FARMS[0]["id"]
    try:
        mtime = os.path.getmtime(_data_file(farm_id))
    except OSError:
        mtime = 0
    return jsonify({"mtime": mtime})
