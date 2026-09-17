from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request

try:
    from services.validation import finite_number
    from services.cultivation_service import (
        backfill_watering_events,
        create_growth_record,
        format_recorded_at,
        get_daily_analysis,
        list_growth_records,
        list_watering_events,
    )
except ModuleNotFoundError:
    from app.services.validation import finite_number
    from app.services.cultivation_service import (
        backfill_watering_events,
        create_growth_record,
        format_recorded_at,
        get_daily_analysis,
        list_growth_records,
        list_watering_events,
    )


cultivation_bp = Blueprint("cultivation", __name__)
SQLITE_INTEGER_MAX = 2**63 - 1


def _required_device_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _optional_non_negative_number(payload, key, errors):
    if key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if isinstance(value, bool):
        errors[key] = "must be a non-negative number"
        return None
    try:
        value = finite_number(value)
    except (TypeError, ValueError):
        errors[key] = "must be a non-negative number"
        return None
    if value < 0:
        errors[key] = "must be a non-negative number"
        return None
    return value


def _optional_non_negative_integer(payload, key, errors):
    if key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if isinstance(value, bool):
        errors[key] = "must be a non-negative integer"
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        errors[key] = "must be a non-negative integer"
        return None
    if (
        not number.is_finite()
        or number < 0
        or number != number.to_integral_value()
        or number > SQLITE_INTEGER_MAX
    ):
        errors[key] = (
            "must be a non-negative integer no greater than "
            f"{SQLITE_INTEGER_MAX}"
        )
        return None
    return int(number)


def _positive_days(value):
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    if days < 1 or days > 365:
        return None
    return days


@cultivation_bp.route("/api/growth", methods=["GET"])
def get_growth_records():
    device_id = _required_device_id(request.args.get("device_id"))
    if device_id is None:
        return jsonify({"error": "device_id is required"}), 400
    return jsonify(list_growth_records(device_id, request.args.get("limit", 1000)))


@cultivation_bp.route("/api/growth", methods=["POST"])
def post_growth_record():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "No JSON received"}), 400

    errors = {}
    device_id = _required_device_id(payload.get("device_id"))
    if device_id is None:
        errors["device_id"] = "required"

    height_cm = _optional_non_negative_number(payload, "height_cm", errors)
    leaf_count = _optional_non_negative_integer(payload, "leaf_count", errors)

    note = payload.get("note")
    if note is not None:
        if not isinstance(note, str):
            errors["note"] = "must be a string"
        else:
            note = note.strip() or None

    recorded_at = payload.get("recorded_at")
    try:
        recorded_at = format_recorded_at(recorded_at)
    except ValueError:
        errors["recorded_at"] = "must be a valid ISO datetime"

    if errors:
        return jsonify({"error": "Invalid growth record", "details": errors}), 400

    record = create_growth_record(
        device_id=device_id,
        height_cm=height_cm,
        leaf_count=leaf_count,
        note=note,
        recorded_at=recorded_at,
    )
    return jsonify(record), 201


@cultivation_bp.route("/api/watering", methods=["GET"])
def get_watering_events():
    device_id = _required_device_id(request.args.get("device_id"))
    if device_id is None:
        return jsonify({"error": "device_id is required"}), 400
    return jsonify(list_watering_events(device_id, request.args.get("limit", 100)))


@cultivation_bp.route("/api/watering/backfill", methods=["POST"])
def post_watering_backfill():
    device_id = None
    if "device_id" in request.args:
        device_id = _required_device_id(request.args.get("device_id"))
        if device_id is None:
            return jsonify({"error": "device_id must not be empty"}), 400
    return jsonify(backfill_watering_events(device_id))


@cultivation_bp.route("/api/analysis/daily", methods=["GET"])
def get_daily_environment_analysis():
    device_id = _required_device_id(request.args.get("device_id"))
    if device_id is None:
        return jsonify({"error": "device_id is required"}), 400

    days = _positive_days(request.args.get("days", 30))
    if days is None:
        return jsonify({"error": "days must be an integer between 1 and 365"}), 400
    return jsonify(get_daily_analysis(device_id, days))
