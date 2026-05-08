from flask import Flask, jsonify, request, redirect
from datetime import datetime
try:
    from routes.dashboard import dashboard_bp
    from services.sensor_service import (
        append_sensor_data,
        filter_sensor_data,
        latest_sensor_record,
        load_sensor_data,
        normalize_device_filter,
    )
    from services.threshold_service import (
        THRESHOLD_FIELDS,
        get_or_create_thresholds,
        upsert_thresholds,
    )
except ModuleNotFoundError:
    from app.routes.dashboard import dashboard_bp
    from app.services.sensor_service import (
        append_sensor_data,
        filter_sensor_data,
        latest_sensor_record,
        load_sensor_data,
        normalize_device_filter,
    )
    from app.services.threshold_service import (
        THRESHOLD_FIELDS,
        get_or_create_thresholds,
        upsert_thresholds,
    )

app = Flask(__name__)
app.register_blueprint(dashboard_bp)

SENSOR_RANGES = {
    "temperature": (-40, 85),
    "humidity": (0, 100),
    "soil_moisture": (0, 100),
    "light": (0, 200000),
}

OPTIONAL_SENSOR_RANGES = {
    "light_digital": (0, 1),
    "soil_digital": (0, 1),
    "soil_raw": (0, 4095),
}

THRESHOLD_PAIRS = [
    ("temperature_min", "temperature_max"),
    ("humidity_min", "humidity_max"),
    ("soil_moisture_min", "soil_moisture_max"),
    ("light_min", "light_max"),
]


def _coerce_number(value):
    if isinstance(value, bool):
        raise ValueError
    return float(value)


def _store_number(payload, key, value):
    if key in {"temperature", "humidity"}:
        payload[key] = value
    elif value.is_integer():
        payload[key] = int(value)
    else:
        payload[key] = value


def _validate_number(payload, key, minimum, maximum, required=True):
    if key not in payload:
        return "required" if required else None

    try:
        value = _coerce_number(payload[key])
    except (TypeError, ValueError):
        return "must be a number"

    if value < minimum or value > maximum:
        return f"must be between {minimum} and {maximum}"

    _store_number(payload, key, value)
    return None


def validate_sensor_payload(payload):
    errors = {}

    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        errors["device_id"] = "required"
    else:
        payload["device_id"] = device_id.strip()

    if "light" not in payload and "light_digital" in payload:
        payload["light"] = payload["light_digital"]

    for key, (minimum, maximum) in SENSOR_RANGES.items():
        error = _validate_number(payload, key, minimum, maximum, required=True)
        if error:
            errors[key] = error

    for key, (minimum, maximum) in OPTIONAL_SENSOR_RANGES.items():
        error = _validate_number(payload, key, minimum, maximum, required=False)
        if error:
            errors[key] = error

    return errors


def _normalize_required_device_id(value):
    if not isinstance(value, str):
        return None

    value = value.strip()
    return value or None


def _store_threshold_number(values, key, value):
    values[key] = int(value) if value.is_integer() else value


def validate_threshold_payload(payload):
    errors = {}
    values = {}

    device_id = _normalize_required_device_id(payload.get("device_id"))
    if device_id is None:
        errors["device_id"] = "required"

    for key in THRESHOLD_FIELDS:
        if key not in payload:
            errors[key] = "required"
            continue

        try:
            value = _coerce_number(payload[key])
        except (TypeError, ValueError):
            errors[key] = "must be a number"
            continue

        _store_threshold_number(values, key, value)

    for min_key, max_key in THRESHOLD_PAIRS:
        if min_key in values and max_key in values and values[min_key] > values[max_key]:
            errors[min_key] = "must be less than or equal to max"
            errors[max_key] = "must be greater than or equal to min"

    return device_id, values, errors


@app.route("/")
def home():
    # http://라즈베리파이IP:5000 으로 접속하면 대시보드로 이동
    return redirect("/dashboard")


@app.route("/api/status", methods=["GET"])
def status():
    # 기존 / 에서 보여주던 서버 상태 확인용 JSON
    data = load_sensor_data()
    latest = latest_sensor_record(data)

    return jsonify({
        "message": "Smart Farm Server Running",
        "latest": latest
    })


@app.route("/api/sensor", methods=["GET"])
def get_sensor_data():
    device_id = normalize_device_filter(request.args.get("device_id"))
    data = filter_sensor_data(load_sensor_data(), device_id)
    # sensor_data.json is append-only, so GET keeps chronological order: oldest to newest.
    return jsonify(data)


@app.route("/api/sensor", methods=["POST"])
def receive_sensor_data():
    new_data = request.get_json(silent=True)

    if not isinstance(new_data, dict):
        return jsonify({"error": "No JSON received"}), 400

    new_data = dict(new_data)
    errors = validate_sensor_payload(new_data)
    if errors:
        return jsonify({"error": "Invalid sensor data", "details": errors}), 400

    # ESP32가 보낸 측정 시간이 없을 때만 기본값 처리
    if "timestamp" not in new_data:
        new_data["timestamp"] = "time_not_set"

    # 라즈베리파이 서버가 받은 시간은 따로 저장
    new_data["server_received_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    append_sensor_data(new_data)

    print(
        f"[{new_data['server_received_at']}] "
        f"device={new_data.get('device_id')} "
        f"esp_time={new_data.get('timestamp')} "
        f"temp={new_data.get('temperature')} "
        f"hum={new_data.get('humidity')} "
        f"soil={new_data.get('soil_moisture')} "
        f"light={new_data.get('light_digital') or new_data.get('light')}"
    )

    return jsonify({
        "message": "Data received",
        "data": new_data
    }), 201


@app.route("/api/thresholds", methods=["GET"])
def get_thresholds():
    device_id = _normalize_required_device_id(request.args.get("device_id"))
    if device_id is None:
        return jsonify({"error": "device_id is required"}), 400

    return jsonify(get_or_create_thresholds(device_id))


@app.route("/api/thresholds", methods=["POST", "PUT"])
def save_thresholds():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "No JSON received"}), 400

    device_id, values, errors = validate_threshold_payload(payload)
    if errors:
        return jsonify({"error": "Invalid threshold settings", "details": errors}), 400

    return jsonify(upsert_thresholds(device_id, values))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
