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
except ModuleNotFoundError:
    from app.routes.dashboard import dashboard_bp
    from app.services.sensor_service import (
        append_sensor_data,
        filter_sensor_data,
        latest_sensor_record,
        load_sensor_data,
        normalize_device_filter,
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
