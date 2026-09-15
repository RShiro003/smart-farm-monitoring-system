"""센서 기록과 알림 기록을 CSV로 내보낸다.

대시보드 표는 페이지 단위로만 볼 수 있어 분석용으로 통째로 받아갈 방법이 없었다.
여기서는 전체 결과를 한 번에 메모리에 올리지 않고 행 단위로 흘려보낸다.
5초 주기 수집이면 하루 17,000행이 넘기 때문에, 기간을 넓게 잡으면
리스트로 모으는 방식은 라즈베리파이의 메모리를 쉽게 압박한다.
"""
import csv
import io
import sqlite3
from datetime import datetime

try:
    from services import sensor_service
    from services.alert_service import (
        ALERT_METRICS,
        DEVICE_METRIC,
        EVENT_LABELS,
        _connect as _alert_connect,
        _ensure_tables as _ensure_alert_tables,
        _event_where,
    )
except ModuleNotFoundError:
    from app.services import sensor_service
    from app.services.alert_service import (
        ALERT_METRICS,
        DEVICE_METRIC,
        EVENT_LABELS,
        _connect as _alert_connect,
        _ensure_tables as _ensure_alert_tables,
        _event_where,
    )


# 내보내기 한 번에 허용하는 최대 행 수다.
# 브라우저와 라즈베리파이 양쪽을 보호하기 위한 상한이며, 초과분은 잘린다.
MAX_EXPORT_ROWS = 200000

SENSOR_COLUMNS = [
    ("server_received_at", "수신 시각"),
    ("device_id", "장치 ID"),
    ("device_label", "장치 이름"),
    ("timestamp", "장치 측정 시각"),
    ("temperature", "온도(°C)"),
    ("humidity", "습도(%)"),
    ("soil_moisture", "토양수분(%)"),
    ("soil_raw", "토양 원시값"),
    ("light", "조도"),
    ("light_unit", "조도 단위"),
    ("light_digital", "조도 디지털"),
]

EVENT_COLUMNS = [
    ("created_at", "발생 시각"),
    ("device_id", "장치 ID"),
    ("device_label", "장치 이름"),
    ("metric_label", "항목"),
    ("status_label", "상태"),
    ("severity_label", "심각도"),
    ("event_label", "유형"),
    ("value", "측정값"),
    ("threshold_min", "기준 최소"),
    ("threshold_max", "기준 최대"),
    ("unit", "단위"),
]


def _writer_row(values):
    # csv 모듈은 파일 객체를 요구하므로 한 줄짜리 버퍼에 쓰고 문자열만 꺼낸다.
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(
        [_spreadsheet_safe(value) for value in values]
    )
    return buffer.getvalue()


def _spreadsheet_safe(value):
    """Excel/Sheets가 외부 입력을 수식으로 실행하지 못하게 한다."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def export_filename(prefix, device_id=None):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    scope = (device_id or "all").replace(" ", "_")
    return f"{prefix}_{scope}_{stamp}.csv"


def _bom():
    # Excel은 BOM 없는 UTF-8 CSV의 한글을 깨뜨린다.
    # 이 파일의 주 사용처가 Excel이므로 UTF-8 BOM을 앞에 붙인다.
    return "﻿"


def stream_sensor_csv(device_id=None, date=None, time_from=None, time_to=None,
                      labels=None):
    """센서 기록을 CSV 행 단위로 생성한다.

    대시보드 히스토리 표와 같은 필터 함수를 재사용해, 화면에서 보던 범위와
    내려받은 파일의 범위가 어긋나지 않게 한다.
    """
    labels = labels or {}
    where, params = sensor_service._history_where(device_id, date, time_from, time_to)

    yield _bom() + _writer_row([title for _, title in SENSOR_COLUMNS])

    conn = sensor_service._connect()
    try:
        # fetchall() 대신 커서를 순회해 한 행씩 흘려보낸다.
        cursor = conn.execute(
            f"SELECT * FROM sensor_data{where} ORDER BY id ASC LIMIT ?",
            params + [MAX_EXPORT_ROWS],
        )
        for row in cursor:
            record = dict(row)
            record["device_label"] = labels.get(record.get("device_id"), "")
            yield _writer_row([
                "" if record.get(key) is None else record.get(key)
                for key, _ in SENSOR_COLUMNS
            ])
    except sqlite3.Error as e:
        print(f"[Export] Sensor export failed: {e}")
    finally:
        conn.close()


def stream_events_csv(device_id=None, status=None, metric=None, date=None,
                      time_from=None, time_to=None, labels=None):
    """알림 기록을 CSV 행 단위로 생성한다."""
    labels = labels or {}
    where, params = _event_where(device_id, status, metric, date, time_from, time_to)

    yield _bom() + _writer_row([title for _, title in EVENT_COLUMNS])

    conn = _alert_connect()
    try:
        _ensure_alert_tables(conn)
        cursor = conn.execute(
            f"SELECT * FROM event_log{where} ORDER BY id DESC LIMIT ?",
            params + [MAX_EXPORT_ROWS],
        )
        for row in cursor:
            metric_name = row["metric"]
            metric_info = ALERT_METRICS.get(metric_name)
            if metric_info is not None:
                metric_label = metric_info["label"]
                unit = metric_info["unit"].strip()
            else:
                metric_label = "장치" if metric_name == DEVICE_METRIC else metric_name
                unit = ""

            record = {
                "created_at": row["created_at"],
                "device_id": row["device_id"],
                "device_label": labels.get(row["device_id"], ""),
                "metric_label": metric_label,
                "status_label": "복구" if row["status"] == "recovered" else "이상",
                "severity_label": (
                    "위험" if row["severity"] == "danger" else "경고"
                ),
                "event_label": EVENT_LABELS.get(row["event_type"], row["event_type"]),
                "value": row["value"],
                "threshold_min": row["threshold_min"],
                "threshold_max": row["threshold_max"],
                "unit": unit,
            }
            yield _writer_row([
                "" if record.get(key) is None else record.get(key)
                for key, _ in EVENT_COLUMNS
            ])
    except sqlite3.Error as e:
        print(f"[Export] Event export failed: {e}")
    finally:
        conn.close()
