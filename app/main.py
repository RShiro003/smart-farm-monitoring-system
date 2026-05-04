from flask import Flask, jsonify, request, redirect
import json
import os
import tempfile
import threading
from datetime import datetime
from routes.dashboard import dashboard_bp

app = Flask(__name__)
app.register_blueprint(dashboard_bp)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(_BASE_DIR, "data", "sensor_data.json")
_lock = threading.Lock()


def load_sensor_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []


def _atomic_write(data):
    """임시 파일에 쓴 뒤 rename — 부분 쓰기로 인한 파일 손상 방지."""
    data_dir = os.path.dirname(DATA_FILE)
    os.makedirs(data_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, DATA_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def append_sensor_data(new_record):
    """Lock 안에서 읽기 → 추가 → 쓰기를 원자적으로 수행."""
    with _lock:
        data = load_sensor_data()
        data.append(new_record)
        _atomic_write(data)


@app.route("/")
def home():
    # http://라즈베리파이IP:5000 으로 접속하면 대시보드로 이동
    return redirect("/dashboard")


@app.route("/api/status", methods=["GET"])
def status():
    # 기존 / 에서 보여주던 서버 상태 확인용 JSON
    data = load_sensor_data()
    latest = data[-1] if data else None

    return jsonify({
        "message": "Smart Farm Server Running",
        "latest": latest
    })


@app.route("/api/sensor", methods=["GET"])
def get_sensor_data():
    data = load_sensor_data()
    return jsonify(data)


@app.route("/api/sensor", methods=["POST"])
def receive_sensor_data():
    new_data = request.get_json()

    if not new_data:
        return jsonify({"error": "No JSON received"}), 400

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