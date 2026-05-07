import json
import os
import tempfile
import threading
from datetime import datetime


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(_BASE_DIR, "data", "sensor_data.json")
LEGACY_DEVICE_ID = "legacy"
ALL_DEVICES_VALUE = "all"
_lock = threading.Lock()


def normalize_device_id(device_id, default=LEGACY_DEVICE_ID):
    if isinstance(device_id, str):
        value = device_id.strip()
        if value:
            return value
    return default


def normalize_device_filter(device_id):
    if not isinstance(device_id, str):
        return None

    value = device_id.strip()
    if not value or value.lower() == ALL_DEVICES_VALUE:
        return None

    return value


def normalize_sensor_record(record):
    normalized = dict(record)
    normalized["device_id"] = normalize_device_id(normalized.get("device_id"))
    return normalized


def load_sensor_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    if not isinstance(raw_data, list):
        return []

    return [
        normalize_sensor_record(row)
        for row in raw_data
        if isinstance(row, dict)
    ]


def _atomic_write(data):
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
    with _lock:
        data = load_sensor_data()
        data.append(normalize_sensor_record(new_record))
        _atomic_write(data)


def filter_sensor_data(data, device_id=None):
    selected_device_id = normalize_device_filter(device_id)
    if selected_device_id is None:
        return list(data)

    return [
        row for row in data
        if normalize_device_id(row.get("device_id")) == selected_device_id
    ]


def list_device_ids(data):
    return sorted({
        normalize_device_id(row.get("device_id"))
        for row in data
        if isinstance(row, dict)
    })


def latest_sensor_record(data):
    return data[-1] if data else None


def parse_record_time(row):
    ts = row.get("server_received_at") or row.get("time") or row.get("timestamp")
    if not ts or ts in {"time_not_set", "time_sync_failed"}:
        return None

    if isinstance(ts, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                continue

        try:
            parsed = datetime.fromisoformat(ts)
            if parsed.tzinfo is not None:
                return parsed.astimezone().replace(tzinfo=None)
            return parsed
        except ValueError:
            return None

    return None


def sensor_data_mtime():
    try:
        return os.path.getmtime(DATA_FILE)
    except OSError:
        return 0
