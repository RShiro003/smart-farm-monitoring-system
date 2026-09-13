import gc
import os
import sqlite3
import sys
import tempfile
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


_TEMP_DIR = tempfile.TemporaryDirectory()
_SENSOR_DB = os.path.join(_TEMP_DIR.name, "sensor_data.db")
_SETTINGS_DB = os.path.join(_TEMP_DIR.name, "smart_farm.db")
_PREVIOUS_SENSOR_ENV = os.environ.get("SMART_FARM_SENSOR_DB_FILE")
_PREVIOUS_SETTINGS_ENV = os.environ.get("SMART_FARM_DB_FILE")
_PREVIOUS_DONT_WRITE_BYTECODE = sys.dont_write_bytecode
os.environ["SMART_FARM_SENSOR_DB_FILE"] = _SENSOR_DB
os.environ["SMART_FARM_DB_FILE"] = _SETTINGS_DB
sys.dont_write_bytecode = True

from app.main import app  # noqa: E402
from app.services import (  # noqa: E402
    alert_service,
    crop_service,
    cultivation_service,
    sensor_service,
    threshold_service,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SENSOR_DB = str(_REPO_ROOT / "app" / "data" / "sensor_data.db")
_DEFAULT_SETTINGS_DB = str(_REPO_ROOT / "app" / "data" / "smart_farm.db")
_RESTORE_SENSOR_DB = _PREVIOUS_SENSOR_ENV or _DEFAULT_SENSOR_DB
_RESTORE_SETTINGS_DB = _PREVIOUS_SETTINGS_ENV or _DEFAULT_SETTINGS_DB

# 다른 테스트가 서비스를 먼저 import했더라도 이 모듈의 모든 쓰기는 임시 DB로 고정한다.
sensor_service.DB_FILE = _SENSOR_DB
cultivation_service.DB_FILE = _SENSOR_DB
alert_service.DB_FILE = _SENSOR_DB
threshold_service.DB_FILE = _SETTINGS_DB
crop_service.DB_FILE = _SETTINGS_DB


def tearDownModule():
    sensor_service.DB_FILE = _RESTORE_SENSOR_DB
    cultivation_service.DB_FILE = _RESTORE_SENSOR_DB
    alert_service.DB_FILE = _RESTORE_SENSOR_DB
    threshold_service.DB_FILE = _RESTORE_SETTINGS_DB
    crop_service.DB_FILE = _RESTORE_SETTINGS_DB
    if _PREVIOUS_SENSOR_ENV is None:
        os.environ.pop("SMART_FARM_SENSOR_DB_FILE", None)
    else:
        os.environ["SMART_FARM_SENSOR_DB_FILE"] = _PREVIOUS_SENSOR_ENV
    if _PREVIOUS_SETTINGS_ENV is None:
        os.environ.pop("SMART_FARM_DB_FILE", None)
    else:
        os.environ["SMART_FARM_DB_FILE"] = _PREVIOUS_SETTINGS_ENV
    sys.dont_write_bytecode = _PREVIOUS_DONT_WRITE_BYTECODE
    # 기존 설정 서비스의 context manager 연결을 Windows 파일 정리 전에 수거한다.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()
    _TEMP_DIR.cleanup()


class CultivationFeatureTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        alert_patcher = mock.patch.object(
            sensor_service,
            "_process_alerts_after_save",
        )
        self.alert_handler = alert_patcher.start()
        self.addCleanup(alert_patcher.stop)
        self.assertEqual(Path(sensor_service.DB_FILE).resolve(), Path(_SENSOR_DB).resolve())
        self.assertEqual(
            Path(cultivation_service.DB_FILE).resolve(),
            Path(_SENSOR_DB).resolve(),
        )
        self.assertNotEqual(Path(_SENSOR_DB).resolve(), Path(_DEFAULT_SENSOR_DB).resolve())
        cultivation_service.initialize_database()
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            for table in (
                "watering_events",
                "growth_records",
                "sensor_data",
                "event_log",
                "alert_state",
            ):
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if exists:
                    conn.execute(f'DELETE FROM "{table}"')
            conn.commit()
        finally:
            conn.close()

    def _append_sequence(
        self,
        device_id,
        values,
        start=None,
        spacing=5,
        raw_values=None,
    ):
        start = start or datetime(2026, 8, 13, 9, 30, 0)
        for index, value in enumerate(values):
            received_at = start + timedelta(seconds=index * spacing)
            record = {
                "device_id": device_id,
                "temperature": 24,
                "humidity": 65,
                "soil_moisture": value,
                "light": 3000,
                "timestamp": received_at.strftime("%Y-%m-%d %H:%M:%S"),
                "server_received_at": received_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if raw_values is not None:
                record["soil_raw"] = raw_values[index]
            sensor_service.append_sensor_data(record)

    def _events(self, device_id):
        return cultivation_service.list_watering_events(device_id)

    def test_normal_watering_creates_one_event(self):
        self._append_sequence("esp32_01", [42, 43, 44, 52, 58, 62, 64])

        events = self._events("esp32_01")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["moisture_before"], 43.0)
        self.assertEqual(events[0]["moisture_after"], 62.0)
        self.assertEqual(events[0]["increase_amount"], 19.0)
        self.assertEqual(events[0]["detection_method"], "soil_moisture_jump")
        self.assertGreaterEqual(events[0]["confidence"], 0)
        self.assertLessEqual(events[0]["confidence"], 1)

    def test_single_sensor_spike_is_ignored(self):
        self._append_sequence("esp32_01", [50, 51, 77, 51, 50])
        self.assertEqual(self._events("esp32_01"), [])

    def test_gradual_change_is_ignored(self):
        self._append_sequence("esp32_01", [50, 51, 52, 53, 54])
        self.assertEqual(self._events("esp32_01"), [])

    def test_minimum_increase_boundary(self):
        start = datetime(2026, 8, 13, 9, 0, 0)
        self._append_sequence("below", [40, 40, 47.9, 47.9, 47.9], start)
        self._append_sequence("exact", [40, 40, 48, 48, 48], start)

        self.assertEqual(self._events("below"), [])
        exact = self._events("exact")
        self.assertEqual(len(exact), 1)
        self._append_sequence("high", [40, 40, 55, 55, 55], start)
        self.assertGreater(self._events("high")[0]["confidence"], exact[0]["confidence"])

    def test_one_abnormal_baseline_sample_is_ignored(self):
        self._append_sequence("esp32_01", [0, 50, 51, 52, 53])
        self.assertEqual(self._events("esp32_01"), [])

    def test_sustained_rise_creates_only_one_event(self):
        self._append_sequence("esp32_01", [40, 48, 55, 62, 66])
        self.assertEqual(len(self._events("esp32_01")), 1)

    def test_calibration_only_percentage_jump_is_ignored(self):
        start = datetime(2026, 8, 13, 9, 0, 0)
        values = [40, 40, 50, 55, 58]
        self._append_sequence(
            "calibration_change",
            values,
            start,
            raw_values=[2400, 2400, 2400, 2400, 2400],
        )
        self._append_sequence(
            "real_watering",
            values,
            start,
            raw_values=[2500, 2490, 2400, 2350, 2300],
        )

        self.assertEqual(self._events("calibration_change"), [])
        self.assertEqual(len(self._events("real_watering")), 1)

    def test_long_data_gap_is_not_watering(self):
        start = datetime(2026, 8, 13, 8, 0, 0)
        self._append_sequence("esp32_01", [40, 41], start)
        self._append_sequence(
            "esp32_01",
            [65, 66, 67],
            start + timedelta(minutes=30),
        )
        self.assertEqual(self._events("esp32_01"), [])

    def test_second_watering_after_cooldown_creates_second_event(self):
        start = datetime(2026, 8, 13, 8, 0, 0)
        self._append_sequence("esp32_01", [40, 42, 50, 58, 62], start)
        self._append_sequence(
            "esp32_01",
            [35, 36, 45, 53, 57],
            start + timedelta(hours=2),
        )
        self.assertEqual(len(self._events("esp32_01")), 2)

    def test_second_rise_inside_cooldown_is_ignored(self):
        start = datetime(2026, 8, 13, 8, 0, 0)
        self._append_sequence("esp32_01", [40, 42, 50, 58, 62], start)
        self._append_sequence(
            "esp32_01",
            [35, 36, 45, 53, 57],
            start + timedelta(minutes=10),
        )
        self.assertEqual(len(self._events("esp32_01")), 1)

    def test_devices_are_evaluated_independently(self):
        start = datetime(2026, 8, 13, 9, 0, 0)
        values_a = [42, 43, 52, 58, 62]
        values_b = [50, 51, 77, 51, 50]
        for index, (value_a, value_b) in enumerate(zip(values_a, values_b)):
            at = start + timedelta(seconds=index * 5)
            self._append_sequence("esp32_a", [value_a], at)
            self._append_sequence("esp32_b", [value_b], at)

        self.assertEqual(len(self._events("esp32_a")), 1)
        self.assertEqual(self._events("esp32_b"), [])

    def test_concurrent_detection_inserts_one_event(self):
        start = datetime(2026, 8, 13, 9, 0, 0)
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            sensor_row_id = None
            for index, value in enumerate([40, 40, 50, 55, 58]):
                at = start + timedelta(seconds=index * 5)
                cursor = conn.execute(
                    """
                    INSERT INTO sensor_data (
                        device_id, temperature, humidity, soil_moisture,
                        light, server_received_at
                    ) VALUES ('concurrent', 24, 65, ?, 3000, ?)
                    """,
                    (value, at.strftime("%Y-%m-%d %H:%M:%S")),
                )
                sensor_row_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        record = {
            "device_id": "concurrent",
            "server_received_at": (start + timedelta(seconds=20)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(
                lambda _: cultivation_service.detect_watering_event(
                    record,
                    sensor_row_id=sensor_row_id,
                ),
                range(8),
            ))

        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self._events("concurrent")), 1)

    def test_growth_api_validates_sorts_and_filters_by_device(self):
        invalid = self.client.post("/api/growth", json={
            "device_id": "esp32_01",
            "height_cm": -1,
            "leaf_count": 1.5,
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(
            set(invalid.get_json()["details"]),
            {"height_cm", "leaf_count"},
        )

        for recorded_at, height, device in (
            ("2026-08-13T09:00:00", 13.5, "esp32_01"),
            ("2026-08-12 09:00:00", 12.8, "esp32_01"),
            ("2026-08-11 09:00:00", 9.0, "esp32_02"),
        ):
            response = self.client.post("/api/growth", json={
                "device_id": device,
                "recorded_at": recorded_at,
                "height_cm": height,
                "leaf_count": 12,
                "note": "새 잎 확인",
            })
            self.assertEqual(response.status_code, 201)

        response = self.client.get("/api/growth?device_id=esp32_01")
        records = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["height_cm"] for row in records], [12.8, 13.5])
        self.assertTrue(all(row["device_id"] == "esp32_01" for row in records))

    def test_growth_api_rejects_missing_device_and_invalid_time(self):
        self.assertEqual(self.client.get("/api/growth").status_code, 400)
        response = self.client.post("/api/growth", json={
            "device_id": "esp32_01",
            "recorded_at": "not-a-time",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("recorded_at", response.get_json()["details"])

    def test_growth_api_handles_large_numbers_without_server_error(self):
        too_many_leaves = self.client.post("/api/growth", json={
            "device_id": "esp32_01",
            "leaf_count": 1e100,
        })
        self.assertEqual(too_many_leaves.status_code, 400)
        self.assertIn("leaf_count", too_many_leaves.get_json()["details"])

        large_height = self.client.post("/api/growth", json={
            "device_id": "esp32_01",
            "height_cm": 1e308,
        })
        self.assertEqual(large_height.status_code, 201)

    def test_watering_api_is_filtered_and_newest_first(self):
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            for device_id, detected_at in (
                ("esp32_01", "2026-08-12 09:00:00"),
                ("esp32_01", "2026-08-13 09:00:00"),
                ("esp32_02", "2026-08-14 09:00:00"),
            ):
                conn.execute(
                    """
                    INSERT INTO watering_events (
                        device_id, detected_at, moisture_before, moisture_after,
                        increase_amount, confidence, detection_method
                    ) VALUES (?, ?, 40, 55, 15, 0.9, 'soil_moisture_jump')
                    """,
                    (device_id, detected_at),
                )
            conn.commit()
        finally:
            conn.close()

        response = self.client.get("/api/watering?device_id=esp32_01")
        events = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [event["detected_at"] for event in events],
            ["2026-08-13 09:00:00", "2026-08-12 09:00:00"],
        )
        self.assertEqual(self.client.get("/api/watering").status_code, 400)

    def test_daily_analysis_uses_sql_aggregation_without_join_multiplication(self):
        day = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            for offset, temperature, humidity, soil in (
                (0, 20, 60, 40),
                (5, 24, 70, 60),
            ):
                at = (day + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """
                    INSERT INTO sensor_data (
                        device_id, temperature, humidity, soil_moisture,
                        light, server_received_at
                    ) VALUES ('esp32_01', ?, ?, ?, 3000, ?)
                    """,
                    (temperature, humidity, soil, at),
                )
            for minute in (1, 2):
                at = day.replace(minute=minute).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """
                    INSERT INTO watering_events (
                        device_id, detected_at, moisture_before, moisture_after,
                        increase_amount, confidence, detection_method
                    ) VALUES ('esp32_01', ?, 40, 55, 15, 0.9, 'soil_moisture_jump')
                    """,
                    (at,),
                )
            conn.commit()
        finally:
            conn.close()

        response = self.client.get("/api/analysis/daily?device_id=esp32_01&days=1")
        self.assertEqual(response.status_code, 200)
        rows = response.get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["temperature_avg"], 22.0)
        self.assertEqual(rows[0]["temperature_min"], 20.0)
        self.assertEqual(rows[0]["temperature_max"], 24.0)
        self.assertEqual(rows[0]["soil_moisture_avg"], 50.0)
        self.assertEqual(rows[0]["sample_count"], 2)
        self.assertEqual(rows[0]["watering_count"], 2)

    def test_daily_analysis_rejects_invalid_arguments(self):
        self.assertEqual(self.client.get("/api/analysis/daily").status_code, 400)
        self.assertEqual(
            self.client.get(
                "/api/analysis/daily?device_id=esp32_01&days=0"
            ).status_code,
            400,
        )

    def test_sensor_post_and_get_contract_remain_compatible(self):
        response = self.client.post("/api/sensor", json={
            "device_id": "esp32_01",
            "temperature": 24.5,
            "humidity": 65,
            "soil_moisture": 52,
            "light": 3000,
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["message"], "Data received")
        self.alert_handler.assert_called_once()

        response = self.client.get("/api/sensor?device_id=esp32_01")
        self.assertEqual(response.status_code, 200)
        rows = response.get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device_id"], "esp32_01")

    def test_watering_failure_does_not_break_sensor_post(self):
        with mock.patch.object(
            cultivation_service,
            "detect_watering_event",
            side_effect=RuntimeError("detector failed"),
        ):
            response = self.client.post("/api/sensor", json={
                "device_id": "esp32_01",
                "temperature": 24.5,
                "humidity": 65,
                "soil_moisture": 52,
                "light": 3000,
            })

        self.assertEqual(response.status_code, 201)
        rows = self.client.get("/api/sensor?device_id=esp32_01").get_json()
        self.assertEqual(len(rows), 1)

    def test_existing_alert_service_uses_the_temporary_sensor_db(self):
        with mock.patch.object(alert_service, "send_discord_message"):
            alert_service.process_sensor_alerts({
                "device_id": "alert_device",
                "temperature": 24,
                "humidity": 65,
                "soil_moisture": 20,
                "light": 3000,
                "server_received_at": "2026-08-13 09:00:00",
            })

        conn = sqlite3.connect(_SENSOR_DB)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE device_id = 'alert_device'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(count, 1)

    def test_dashboard_renders_growth_and_watering_sections(self):
        self._append_sequence("esp32_01", [50])
        response = self.client.get("/dashboard?device_id=esp32_01")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="growth-form"', html)
        self.assertIn('id="growthChart"', html)
        self.assertIn('id="growth-tbody"', html)
        self.assertIn('id="watering-tbody"', html)
        self.assertIn('const SELECTED_DEVICE_ID = "esp32_01";', html)

    def test_schema_initialization_is_idempotent_and_preserves_sensor_rows(self):
        self._append_sequence("esp32_01", [50])
        cultivation_service.initialize_database()
        cultivation_service.initialize_database()

        conn = sqlite3.connect(_SENSOR_DB)
        try:
            count = conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0]
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        finally:
            conn.close()

        self.assertEqual(count, 1)
        self.assertIn("idx_growth_records_device_recorded", indexes)
        self.assertIn("idx_watering_events_device_detected", indexes)


if __name__ == "__main__":
    unittest.main()
