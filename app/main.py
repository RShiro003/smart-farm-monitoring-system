from flask import Flask, jsonify, request, redirect

# 이 파일은 Flask 애플리케이션의 진입점이다.
# 센서 수집 API는 app/routes/sensor.py의 Blueprint가 담당하고,
# 대시보드 화면/조회 API는 app/routes/dashboard.py의 Blueprint가 담당한다.
# main.py에는 앱 생성, Blueprint 등록, 공통 상태 확인, 임계값 저장 API만 남겨
# "어떤 라우트가 어디에 있는지"를 명확히 분리한다.
try:
    from routes.dashboard import dashboard_bp
    from routes.sensor import sensor_bp
    from services.sensor_service import (
        latest_sensor_record,
        load_sensor_data,
    )
    from services.threshold_service import (
        THRESHOLD_FIELDS,
        get_or_create_thresholds,
        upsert_thresholds,
    )
except ModuleNotFoundError:
    from app.routes.dashboard import dashboard_bp
    from app.routes.sensor import sensor_bp
    from app.services.sensor_service import (
        latest_sensor_record,
        load_sensor_data,
    )
    from app.services.threshold_service import (
        THRESHOLD_FIELDS,
        get_or_create_thresholds,
        upsert_thresholds,
    )

app = Flask(__name__)
# /dashboard, /api/dashboard/* 라우트 등록
app.register_blueprint(dashboard_bp)
# /api/sensor GET/POST 라우트 등록
app.register_blueprint(sensor_bp)

# 임계값은 최소/최대가 한 쌍으로 들어온다.
# 저장 전 검증 단계에서 최소값이 최대값보다 큰 잘못된 설정을 막기 위해
# 각 센서 항목의 min/max 필드명을 한 곳에 모아 둔다.
THRESHOLD_PAIRS = [
    ("temperature_min", "temperature_max"),
    ("humidity_min", "humidity_max"),
    ("soil_moisture_min", "soil_moisture_max"),
    ("light_min", "light_max"),
]


def _coerce_number(value):
    # JSON에서는 true/false도 숫자처럼 처리될 수 있으므로 임계값에는 허용하지 않는다.
    # ESP32와 대시보드는 임계값을 실제 센서 범위 비교에 사용하므로 명시적인 숫자만 받는다.
    if isinstance(value, bool):
        raise ValueError
    return float(value)


def _normalize_required_device_id(value):
    # 임계값은 장치별로 따로 저장된다.
    # 빈 문자열을 허용하면 어떤 ESP32에 적용해야 하는 설정인지 알 수 없기 때문에
    # 공백 제거 후 값이 없으면 None으로 돌려 라우트에서 400 응답을 만든다.
    if not isinstance(value, str):
        return None

    value = value.strip()
    return value or None


def _store_threshold_number(values, key, value):
    # 화면에 다시 내려줄 때 18.0처럼 불필요한 소수점이 붙지 않도록
    # 정수로 표현 가능한 값은 int로 보관한다. 비교/저장 의미는 그대로 숫자다.
    values[key] = int(value) if value.is_integer() else value


def validate_threshold_payload(payload):
    # /api/thresholds PUT/POST에서 들어온 JSON을 DB에 저장하기 전에 검증한다.
    # 서버에서도 한 번 더 검증해야 브라우저 우회 요청이나 잘못된 ESP32 설정값으로
    # 임계값 테이블이 깨지는 것을 막을 수 있다.
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
    # Raspberry Pi 주소만 입력해도 모니터링 화면으로 들어가게 한다.
    # 실제 화면 렌더링은 dashboard Blueprint의 /dashboard 라우트가 담당한다.
    return redirect("/dashboard")


@app.route("/api/status", methods=["GET"])
def status():
    # 서버가 살아 있는지 간단히 확인하는 상태 API다.
    # 최신 센서 row도 함께 내려주기 때문에 ESP32 수신 여부를 빠르게 점검할 수 있다.
    data = load_sensor_data()
    latest = latest_sensor_record(data)

    return jsonify({
        "message": "Smart Farm Server Running",
        "latest": latest
    })


@app.route("/api/thresholds", methods=["GET"])
def get_thresholds():
    # 대시보드와 실제 ESP32 노드는 device_id를 기준으로 임계값을 조회한다.
    # 장치마다 설치 위치나 센서 보정값이 다를 수 있으므로 전역 설정 하나를 공유하지 않는다.
    device_id = _normalize_required_device_id(request.args.get("device_id"))
    if device_id is None:
        return jsonify({"error": "device_id is required"}), 400

    return jsonify(get_or_create_thresholds(device_id))


@app.route("/api/thresholds", methods=["POST", "PUT"])
def save_thresholds():
    # 대시보드의 임계값 입력 폼이 호출하는 저장 API다.
    # 성공하면 DB에 반영된 최종 설정을 다시 반환하여 화면의 기준선/가이드도 같은 값으로 갱신한다.
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "No JSON received"}), 400

    device_id, values, errors = validate_threshold_payload(payload)
    if errors:
        return jsonify({"error": "Invalid threshold settings", "details": errors}), 400

    return jsonify(upsert_thresholds(device_id, values))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
