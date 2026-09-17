from datetime import datetime
import re
from time import perf_counter

from flask import Blueprint, jsonify, request

# 센서 수집 전용 Blueprint다.
# main.py에 직접 라우트를 두지 않고 이 파일로 분리해 두면,
# ESP32가 호출하는 /api/sensor 흐름과 대시보드/임계값 흐름을 쉽게 구분할 수 있다.
try:
    from services.validation import finite_number as _coerce_number
    from services.sensor_service import (
        LIGHT_UNITS,
        LIGHT_UNIT_DIGITAL,
        LIGHT_UNIT_LUX,
        append_sensor_data,
        list_sensor_records,
        normalize_device_filter,
        normalize_light_unit,
    )
except ModuleNotFoundError:
    from app.services.validation import finite_number as _coerce_number
    from app.services.sensor_service import (
        LIGHT_UNITS,
        LIGHT_UNIT_DIGITAL,
        LIGHT_UNIT_LUX,
        append_sensor_data,
        list_sensor_records,
        normalize_device_filter,
        normalize_light_unit,
    )


sensor_bp = Blueprint("sensor", __name__)

# ESP32가 반드시 보내야 하는 기본 센서값과 허용 범위다.
# 잘못된 값이 SQLite에 누적되면 평균/그래프/임계값 판단이 모두 흔들리기 때문에
# DB insert 전에 서버에서 한 번 더 범위를 확인한다.
SENSOR_RANGES = {
    "temperature": (-40, 85),
    "humidity": (0, 100),
    "soil_moisture": (0, 100),
    "light": (0, 200000),
}

# 실제 센서 노드에서는 디지털 출력이나 원시 ADC 값처럼 보조 진단값도 보낸다.
# 이 값들은 있으면 저장하지만, 더 단순한 더미 노드와의 호환을 위해 필수로 요구하지 않는다.
OPTIONAL_SENSOR_RANGES = {
    "light_digital": (0, 1),
    "soil_digital": (0, 1),
    "soil_raw": (0, 4095),
}


def _store_number(payload, key, value):
    # 온도/습도는 소수 측정값을 자주 사용하므로 float 형태를 유지한다.
    # 토양수분/조도/디지털 값은 정수로 들어오는 경우가 많아, 정수 표현 가능 시 int로 정리한다.
    if key in {"temperature", "humidity"}:
        payload[key] = value
    elif value.is_integer():
        payload[key] = int(value)
    else:
        payload[key] = value


def _validate_number(payload, key, minimum, maximum, required=True):
    # 한 센서 항목에 대해 존재 여부, 숫자 여부, 허용 범위를 순서대로 확인한다.
    # 반환값은 라우트에서 그대로 details에 담아 ESP32/테스트 클라이언트가 원인을 알 수 있게 한다.
    if key not in payload:
        return "required" if required else None
    if payload[key] is None and key in {"temperature", "humidity"}:
        return None

    try:
        value = _coerce_number(payload[key])
    except (TypeError, ValueError):
        return "must be a number"

    if value < minimum or value > maximum:
        return f"must be between {minimum} and {maximum}"

    _store_number(payload, key, value)
    return None


def _infer_light_unit(payload):
    # light_unit을 보내지 않는 구형 펌웨어를 위한 추론이다.
    # light_digital이 있고 light가 그 값과 같으면 비교기 모듈의 0/1 출력이므로 digital,
    # 그 외에 light 값이 있으면 조도계가 측정한 lux로 본다.
    light = payload.get("light")
    light_digital = payload.get("light_digital")
    if light_digital is not None and (light is None or light == light_digital):
        return LIGHT_UNIT_DIGITAL
    if light is not None:
        return LIGHT_UNIT_LUX
    return None


def validate_sensor_payload(payload):
    # /api/sensor POST의 핵심 검증 단계다.
    # 여기서 payload를 정리한 뒤 append_sensor_data()로 넘기면,
    # sensor_service는 JSON 전체가 아니라 SQLite 컬럼에 맞는 값만 저장한다.
    errors = {}

    device_id = payload.get("device_id")
    # 여러 ESP32가 같은 Flask 서버로 데이터를 보내므로 device_id는 필수다.
    # 대시보드의 장치 선택, /api/sensor?device_id=... 필터, 장치별 임계값 조회가 모두 이 값을 기준으로 한다.
    if not isinstance(device_id, str) or not device_id.strip():
        errors["device_id"] = "required"
    elif len(device_id.strip()) > 64 or any(ord(ch) < 32 for ch in device_id.strip()):
        errors["device_id"] = "must be 1-64 printable characters"
    else:
        payload["device_id"] = device_id.strip()

    sample_id = payload.get("sample_id")
    if sample_id is not None:
        if not isinstance(sample_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", sample_id.strip()
        ):
            errors["sample_id"] = "must be 1-96 safe identifier characters"
        else:
            payload["sample_id"] = sample_id.strip()

    sensor_errors = payload.get("sensor_errors")
    if sensor_errors is not None:
        if (
            not isinstance(sensor_errors, list)
            or len(sensor_errors) > 10
            or any(not isinstance(item, str) or len(item) > 80 for item in sensor_errors)
        ):
            errors["sensor_errors"] = "must be a list of up to 10 short strings"

    if "light" not in payload and "light_digital" in payload:
        # 실제 조도 센서가 디지털 출력만 제공하는 경우에도 기존 대시보드의 light 컬럼/그래프가
        # 동작하도록 light 값이 없으면 light_digital을 대표 조도값으로 사용한다.
        payload["light"] = payload["light_digital"]

    # light 값의 단위를 확정한다. 노드가 명시적으로 보내면 그 값을 쓰고,
    # 보내지 않는 구형 펌웨어는 light_digital과 light가 같은지로 추론한다.
    # 단위를 모른 채 lux 기준 임계값과 0/1을 비교하면 조도 판정이 의미를 잃는다.
    if "light_unit" in payload:
        light_unit = normalize_light_unit(payload["light_unit"])
        if light_unit is None:
            errors["light_unit"] = f"must be one of {', '.join(LIGHT_UNITS)}"
        else:
            payload["light_unit"] = light_unit
    else:
        inferred_unit = _infer_light_unit(payload)
        if inferred_unit is not None:
            payload["light_unit"] = inferred_unit

    for key, (minimum, maximum) in SENSOR_RANGES.items():
        error = _validate_number(payload, key, minimum, maximum, required=True)
        if error:
            errors[key] = error

    for key, (minimum, maximum) in OPTIONAL_SENSOR_RANGES.items():
        error = _validate_number(payload, key, minimum, maximum, required=False)
        if error:
            errors[key] = error

    return errors


@sensor_bp.route("/api/sensor", methods=["GET"])
def get_sensor_data():
    # 대시보드의 현재값 카드가 전체 row를 받아 마지막 row를 사용하고,
    # 외부 테스트에서도 같은 엔드포인트로 저장 결과를 확인한다.
    # device_id 쿼리가 있으면 특정 ESP32 데이터만 내려주고, 없거나 all이면 전체 장치를 내려준다.
    start = perf_counter()
    device_id = normalize_device_filter(request.args.get("device_id"))
    include_all = (request.args.get("all") or "").strip().lower() == "true"
    try:
        limit = int(request.args.get("limit", 500))
    except (TypeError, ValueError):
        limit = 500
    data = list_sensor_records(device_id, limit=limit, include_all=include_all)
    suffix = " all=true" if include_all else f" limit={max(1, min(limit, 5000))}"
    print(f"[sensor.get] {perf_counter() - start:.3f}s rows={len(data)}{suffix}")
    # SQLite의 autoincrement id 기준 오름차순으로 읽기 때문에 응답은 오래된 값에서 최신 값 순서다.
    return jsonify(data)


@sensor_bp.route("/api/sensor", methods=["POST"])
def receive_sensor_data():
    # ESP32가 5초 주기로 JSON을 POST하는 수집 지점이다.
    # request.get_json(silent=True)를 사용해 JSON이 아니거나 파싱 실패한 요청도 서버 예외 대신 400으로 처리한다.
    new_data = request.get_json(silent=True)

    if not isinstance(new_data, dict):
        return jsonify({"error": "No JSON received"}), 400

    new_data = dict(new_data)
    errors = validate_sensor_payload(new_data)
    if errors:
        return jsonify({"error": "Invalid sensor data", "details": errors}), 400

    # timestamp는 ESP32가 센서를 읽은 시각이다.
    # NTP 동기화 실패 또는 더미/테스트 요청처럼 시간이 없을 수 있어 기본 문자열로 남겨 둔다.
    if "timestamp" not in new_data:
        new_data["timestamp"] = "time_not_set"

    # server_received_at은 Raspberry Pi Flask 서버가 요청을 받은 시각이다.
    # ESP32 시간이 틀려도 서버 수신 기준으로 정렬/검색할 수 있도록 timestamp와 별도 컬럼에 저장한다.
    new_data["server_received_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 실제 SQLite INSERT는 서비스 계층에서 수행한다.
    # 라우트는 HTTP/JSON 검증과 응답 형식만 담당하고, 저장소 세부 구현은 sensor_service.py에 둔다.
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
