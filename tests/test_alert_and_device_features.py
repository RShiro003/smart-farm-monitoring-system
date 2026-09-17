"""조도 단위, 알림 기록 조회, 장치 오프라인 감지, 작물→임계값 적용 테스트.

기존 tests/test_cultivation_features.py와 같은 방식으로 임시 DB를 사용해
저장소의 app/data/*.db를 건드리지 않는다.
"""
import gc
import os
import sqlite3
import sys
import tempfile
import unittest
import warnings
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

# 서비스 모듈의 DB_FILE은 프로세스 전역이라 여러 테스트 모듈이 같은 값을 공유한다.
# unittest discover는 모든 테스트 모듈을 먼저 import한 뒤 실행하므로,
# import 시점에 한 번만 값을 박아 두면 나중에 import된 모듈이 그 값을 덮어쓴다.
# 그래서 매 테스트 setUp에서 다시 고정하고, 모듈이 끝나면 직전 값으로 되돌린다.
_PINNED = {
    sensor_service: _SENSOR_DB,
    cultivation_service: _SENSOR_DB,
    alert_service: _SENSOR_DB,
    threshold_service: _SETTINGS_DB,
    crop_service: _SETTINGS_DB,
}
_PRE_PIN = {}


def _pin_databases():
    # 값을 기억하는 시점은 import 시점이 아니라 첫 setUp이어야 한다.
    # discover는 모든 모듈을 import한 뒤 실행하므로, import 시점에 기억하면
    # 나중에 import된 모듈이 설정한 경로가 아니라 그 이전 값을 되돌려 주게 된다.
    for module, path in _PINNED.items():
        _PRE_PIN.setdefault(module, module.DB_FILE)
        module.DB_FILE = path


# import 시점의 안전은 위에서 설정한 환경변수가 이미 보장한다.
# 서비스가 import되며 실행하는 _init_db()는 환경변수 경로를 사용하므로
# 여기서 미리 DB_FILE을 건드릴 필요가 없다.


def tearDownModule():
    for module, previous in _PRE_PIN.items():
        module.DB_FILE = previous

    # 경로만 복원한다. 여기서 스키마 초기화를 호출하면 운영 DB가 바뀔 수 있으므로
    # 각 테스트 모듈이 자신의 setUp에서 임시 DB를 직접 준비한다.
    if _PREVIOUS_SENSOR_ENV is None:
        os.environ.pop("SMART_FARM_SENSOR_DB_FILE", None)
    else:
        os.environ["SMART_FARM_SENSOR_DB_FILE"] = _PREVIOUS_SENSOR_ENV
    if _PREVIOUS_SETTINGS_ENV is None:
        os.environ.pop("SMART_FARM_DB_FILE", None)
    else:
        os.environ["SMART_FARM_DB_FILE"] = _PREVIOUS_SETTINGS_ENV
    sys.dont_write_bytecode = _PREVIOUS_DONT_WRITE_BYTECODE
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()
    _TEMP_DIR.cleanup()


class _BaseCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

        # 다른 테스트 모듈이 같은 서비스 전역을 건드렸을 수 있어 매번 다시 고정한다.
        _pin_databases()

        # Discord 발송은 네트워크를 타므로 항상 막는다.
        discord_patcher = mock.patch.object(alert_service, "send_discord_message")
        self.discord = discord_patcher.start()
        self.addCleanup(discord_patcher.stop)

        # 관수 감지는 이 테스트의 관심사가 아니고 센서 저장 경로를 느리게 만든다.
        watering_patcher = mock.patch.object(
            sensor_service, "_detect_watering_after_save"
        )
        watering_patcher.start()
        self.addCleanup(watering_patcher.stop)

        self.assertEqual(
            Path(sensor_service.DB_FILE).resolve(), Path(_SENSOR_DB).resolve()
        )
        self.assertNotEqual(
            Path(_SENSOR_DB).resolve(), Path(_DEFAULT_SENSOR_DB).resolve()
        )

        sensor_service._init_db()
        self._clear_tables()

    def _clear_tables(self):
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            for table in ("sensor_data", "event_log", "alert_state"):
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if exists:
                    conn.execute(f'DELETE FROM "{table}"')
            conn.commit()
        finally:
            conn.close()

        settings = sqlite3.connect(_SETTINGS_DB)
        try:
            exists = settings.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threshold_settings'"
            ).fetchone()
            if exists:
                settings.execute("DELETE FROM threshold_settings")
            settings.commit()
        finally:
            settings.close()

    def _post_sensor(self, **overrides):
        payload = {
            "device_id": "esp32_01",
            "temperature": 22,
            "humidity": 65,
            "soil_moisture": 55,
            "light": 3000,
        }
        payload.update(overrides)
        return self.client.post("/api/sensor", json=payload)

    def _insert_row_at(self, device_id, received_at, **columns):
        """임의 시각의 센서 row를 직접 넣는다(오프라인 판정 테스트용)."""
        if isinstance(received_at, datetime):
            received_at = received_at.strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            cols = ["device_id", "server_received_at"] + list(columns)
            values = [device_id, received_at] + list(columns.values())
            conn.execute(
                f"INSERT INTO sensor_data ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                values,
            )
            conn.commit()
        finally:
            conn.close()


class LightUnitTests(_BaseCase):
    def test_explicit_lux_is_stored(self):
        response = self._post_sensor(light=3000, light_unit="lux")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["data"]["light_unit"], "lux")

    def test_explicit_digital_is_stored(self):
        response = self._post_sensor(light=1, light_digital=1, light_unit="digital")
        self.assertEqual(response.get_json()["data"]["light_unit"], "digital")

    def test_missing_unit_infers_digital_from_light_digital(self):
        # 구형 펌웨어는 light_unit을 보내지 않는다.
        response = self._post_sensor(light=1, light_digital=1)
        self.assertEqual(response.get_json()["data"]["light_unit"], "digital")

    def test_missing_unit_infers_lux_from_plain_light(self):
        response = self._post_sensor(light=4200)
        self.assertEqual(response.get_json()["data"]["light_unit"], "lux")

    def test_light_only_supplied_as_digital_still_infers_digital(self):
        # light 없이 light_digital만 오면 라우트가 light를 복사한 뒤 digital로 판정해야 한다.
        response = self.client.post("/api/sensor", json={
            "device_id": "esp32_01", "temperature": 22,
            "humidity": 65, "soil_moisture": 55, "light_digital": 0,
        })
        data = response.get_json()["data"]
        self.assertEqual(data["light_unit"], "digital")
        self.assertEqual(data["light"], 0)

    def test_unknown_unit_is_rejected(self):
        response = self._post_sensor(light_unit="candela")
        self.assertEqual(response.status_code, 400)
        self.assertIn("light_unit", response.get_json()["details"])

    def test_unit_is_case_insensitive(self):
        response = self._post_sensor(light_unit="LUX")
        self.assertEqual(response.get_json()["data"]["light_unit"], "lux")

    def test_unit_is_persisted_and_returned_by_queries(self):
        self._post_sensor(light=3000, light_unit="lux")
        latest = self.client.get("/api/dashboard/latest").get_json()
        self.assertEqual(latest["light_unit"], "lux")

    def test_migration_adds_column_and_classifies_old_rows(self):
        # light_unit 컬럼이 없던 시절의 DB를 그대로 재현한다.
        legacy_db = os.path.join(_TEMP_DIR.name, "legacy_sensor.db")
        if os.path.exists(legacy_db):
            os.remove(legacy_db)
        conn = sqlite3.connect(legacy_db)
        try:
            conn.execute(
                """
                CREATE TABLE sensor_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL DEFAULT 'legacy',
                    temperature REAL, humidity REAL, soil_moisture REAL,
                    light REAL, light_digital INTEGER, soil_digital INTEGER,
                    soil_raw INTEGER, timestamp TEXT,
                    server_received_at TEXT, time TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO sensor_data (device_id, light, light_digital,"
                " server_received_at) VALUES ('dig', 1, 1, '2026-01-01 10:00:00')"
            )
            conn.execute(
                "INSERT INTO sensor_data (device_id, light, server_received_at)"
                " VALUES ('lux', 5000, '2026-01-01 10:00:00')"
            )
            conn.execute(
                "INSERT INTO sensor_data (device_id, temperature,"
                " server_received_at) VALUES ('nolight', 21, '2026-01-01 10:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        original = sensor_service.DB_FILE
        try:
            sensor_service.DB_FILE = legacy_db
            sensor_service._init_db()
            sensor_service._init_db()  # 두 번 실행해도 안전해야 한다(멱등).
        finally:
            sensor_service.DB_FILE = original

        conn = sqlite3.connect(legacy_db)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(sensor_data)")]
            self.assertEqual(columns.count("light_unit"), 1)
            units = dict(
                conn.execute("SELECT device_id, light_unit FROM sensor_data")
            )
            self.assertEqual(units["dig"], "digital")
            self.assertEqual(units["lux"], "lux")
            # 조도값이 아예 없던 row는 단위를 지어내지 않는다.
            self.assertIsNone(units["nolight"])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0], 3
            )
        finally:
            conn.close()


class LightAlertTests(_BaseCase):
    def _set_light_thresholds(self, device_id):
        threshold_service.upsert_thresholds(
            device_id, {"light_min": 2000, "light_max": 8000}
        )

    def _light_events(self, device_id):
        return [
            event
            for event in alert_service.list_events(device_id)
            if event["metric"] == "light"
        ]

    def test_lux_device_out_of_range_creates_alert(self):
        self._set_light_thresholds("esp32_lux")
        self._post_sensor(device_id="esp32_lux", light=10, light_unit="lux")
        events = self._light_events("esp32_lux")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "threshold_below")
        self.assertEqual(events[0]["status"], "abnormal")

    def test_lux_device_in_range_creates_no_alert(self):
        self._set_light_thresholds("esp32_lux")
        self._post_sensor(device_id="esp32_lux", light=3000, light_unit="lux")
        self.assertEqual(self._light_events("esp32_lux"), [])

    def test_digital_device_is_skipped(self):
        # 0은 2000~8000 lux 범위 밖이지만 단위가 달라 검사 대상이 아니다.
        self._set_light_thresholds("esp32_dig")
        self._post_sensor(
            device_id="esp32_dig", light=0, light_digital=0, light_unit="digital"
        )
        self.assertEqual(self._light_events("esp32_dig"), [])

    def test_digital_device_still_alerts_on_other_metrics(self):
        # 조도만 건너뛸 뿐 온도/습도/토양수분 알림은 그대로 동작해야 한다.
        threshold_service.upsert_thresholds(
            "esp32_dig", {"temperature_min": 18, "temperature_max": 25}
        )
        self._post_sensor(
            device_id="esp32_dig", temperature=40, light=0,
            light_digital=0, light_unit="digital",
        )
        metrics = {e["metric"] for e in alert_service.list_events("esp32_dig")}
        self.assertIn("temperature", metrics)
        self.assertNotIn("light", metrics)

    def test_light_recovery_is_recorded(self):
        self._set_light_thresholds("esp32_lux")
        self._post_sensor(device_id="esp32_lux", light=10, light_unit="lux")
        self._post_sensor(device_id="esp32_lux", light=3000, light_unit="lux")
        statuses = [e["status"] for e in self._light_events("esp32_lux")]
        self.assertIn("recovered", statuses)


class DefaultLightThresholdTests(_BaseCase):
    """조도 알림을 켜면서 기본 조도 범위가 오탐을 내지 않는지 확인한다."""

    def test_default_range_does_not_alert_on_normal_lux(self):
        # 기본값 row를 만든 뒤 일반적인 실내 조도를 보낸다.
        threshold_service.get_or_create_thresholds("esp32_01")
        self._post_sensor(light=3000, light_unit="lux")
        light_events = [
            e for e in alert_service.list_events("esp32_01")
            if e["metric"] == "light"
        ]
        self.assertEqual(light_events, [])

    def test_default_range_covers_sensor_accepted_range(self):
        # 라우트가 허용하는 최대 조도까지는 기본 설정에서 이상으로 보지 않아야 한다.
        threshold_service.get_or_create_thresholds("esp32_01")
        self._post_sensor(light=200000, light_unit="lux")
        light_events = [
            e for e in alert_service.list_events("esp32_01")
            if e["metric"] == "light"
        ]
        self.assertEqual(light_events, [])

    def test_legacy_placeholder_range_is_migrated_once(self):
        legacy_db = os.path.join(_TEMP_DIR.name, "legacy_settings.db")
        if os.path.exists(legacy_db):
            os.remove(legacy_db)
        conn = sqlite3.connect(legacy_db)
        try:
            conn.execute(
                """
                CREATE TABLE threshold_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL UNIQUE,
                    temperature_min REAL NOT NULL DEFAULT 18,
                    temperature_max REAL NOT NULL DEFAULT 25,
                    humidity_min REAL NOT NULL DEFAULT 60,
                    humidity_max REAL NOT NULL DEFAULT 80,
                    soil_moisture_min REAL NOT NULL DEFAULT 40,
                    soil_moisture_max REAL NOT NULL DEFAULT 70,
                    light_min REAL NOT NULL DEFAULT 0,
                    light_max REAL NOT NULL DEFAULT 100,
                    soil_dry_raw INTEGER NOT NULL DEFAULT 4095,
                    soil_wet_raw INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # 자리표시자를 그대로 쓰던 장치와, 사용자가 직접 설정한 장치를 함께 넣는다.
            conn.execute(
                "INSERT INTO threshold_settings (device_id, light_min, light_max,"
                " updated_at) VALUES ('placeholder', 0, 100, '2026-01-01 00:00:00')"
            )
            conn.execute(
                "INSERT INTO threshold_settings (device_id, light_min, light_max,"
                " updated_at) VALUES ('configured', 1500, 9000, '2026-01-01 00:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        original = threshold_service.DB_FILE
        try:
            threshold_service.DB_FILE = legacy_db
            threshold_service.init_threshold_db()

            migrated = threshold_service.get_or_create_thresholds("placeholder")
            self.assertEqual(
                migrated["light_max"], threshold_service.DEFAULT_THRESHOLDS["light_max"]
            )
            # 사용자가 직접 넣은 값은 건드리지 않는다.
            kept = threshold_service.get_or_create_thresholds("configured")
            self.assertEqual(kept["light_min"], 1500)
            self.assertEqual(kept["light_max"], 9000)

            # 마이그레이션 이후 사용자가 의도적으로 0~100을 넣으면 유지되어야 한다.
            threshold_service.upsert_thresholds(
                "placeholder", {"light_min": 0, "light_max": 100}
            )
            threshold_service.init_threshold_db()
            again = threshold_service.get_or_create_thresholds("placeholder")
            self.assertEqual(again["light_max"], 100)
        finally:
            threshold_service.DB_FILE = original


class EventLogApiTests(_BaseCase):
    def _insert_event_at(self, device_id, created_at, value=40):
        conn = alert_service._connect()
        try:
            alert_service._ensure_tables(conn)
            conn.execute(
                """
                INSERT INTO event_log (
                    device_id, event_type, metric, value,
                    threshold_min, threshold_max, message,
                    status, severity, created_at
                ) VALUES (?, 'threshold_above', 'temperature', ?,
                          18, 25, 'historical event',
                          'abnormal', 'warning', ?)
                """,
                (device_id, value, created_at),
            )
            conn.commit()
        finally:
            conn.close()

    def _make_events(self):
        threshold_service.upsert_thresholds(
            "esp32_01", {"temperature_min": 18, "temperature_max": 25}
        )
        self._post_sensor(temperature=40)   # 이상
        self._post_sensor(temperature=22)   # 복구

    def test_endpoint_returns_pagination_envelope(self):
        self._make_events()
        body = self.client.get("/api/dashboard/events").get_json()
        for key in ("items", "page", "per_page", "pages", "total", "active"):
            self.assertIn(key, body)

    def test_public_events_alias_returns_same_data(self):
        self._make_events()
        dashboard = self.client.get("/api/dashboard/events").get_json()
        public = self.client.get("/api/events").get_json()
        self.assertEqual(public["items"], dashboard["items"])
        self.assertEqual(public["total"], dashboard["total"])

    def test_events_are_listed_newest_first(self):
        self._make_events()
        items = self.client.get("/api/dashboard/events").get_json()["items"]
        self.assertEqual(items[0]["status"], "recovered")
        self.assertEqual(items[1]["status"], "abnormal")

    def test_korean_labels_are_included(self):
        self._make_events()
        item = self.client.get("/api/dashboard/events").get_json()["items"][-1]
        self.assertEqual(item["metric_label"], "온도")
        self.assertEqual(item["event_label"], "기준값 초과")
        self.assertEqual(item["unit"], "°C")

    def test_status_filter(self):
        self._make_events()
        abnormal = self.client.get(
            "/api/dashboard/events?status=abnormal"
        ).get_json()
        self.assertEqual(abnormal["total"], 1)
        self.assertTrue(all(i["status"] == "abnormal" for i in abnormal["items"]))

    def test_unknown_status_filter_is_ignored(self):
        self._make_events()
        body = self.client.get("/api/dashboard/events?status=bogus").get_json()
        self.assertEqual(body["total"], 2)

    def test_metric_filter(self):
        self._make_events()
        body = self.client.get("/api/dashboard/events?metric=humidity").get_json()
        self.assertEqual(body["total"], 0)

    def test_date_and_time_filters(self):
        self._make_events()
        today = datetime.now().strftime("%Y-%m-%d")
        current_time = datetime.now().strftime("%H:%M")
        matching = self.client.get(
            f"/api/events?date={today}&time_from={current_time}&time_to={current_time}"
        ).get_json()
        old = self.client.get("/api/events?date=1900-01-01").get_json()
        self.assertEqual(matching["total"], 2)
        self.assertEqual(old["total"], 0)

    def test_date_range_returns_july_and_august_events_in_time_order(self):
        # 과거 데이터를 나중에 이관해 id 순서가 발생 시각과 달라도
        # 화면에는 실제 발생 시각 기준으로 나타나야 한다.
        self._insert_event_at("esp32_sensor", "2025-09-01 09:00:00", 43)
        self._insert_event_at("esp32_sensor", "2025-07-08 10:00:00", 41)
        self._insert_event_at("esp32_sensor", "2025-08-19 11:00:00", 42)

        body = self.client.get(
            "/api/events?device_id=esp32_sensor"
            "&date_from=2025-07-01&date_to=2025-08-31"
        ).get_json()

        self.assertEqual(body["total"], 2)
        self.assertEqual(
            [item["created_at"] for item in body["items"]],
            ["2025-08-19 11:00:00", "2025-07-08 10:00:00"],
        )

    def test_reversed_or_invalid_event_date_range_returns_no_rows(self):
        self._insert_event_at("esp32_sensor", "2025-07-08 10:00:00")
        reversed_range = self.client.get(
            "/api/events?date_from=2025-08-31&date_to=2025-07-01"
        ).get_json()
        invalid_range = self.client.get(
            "/api/events?date_from=not-a-date&date_to=2025-08-31"
        ).get_json()
        self.assertEqual(reversed_range["total"], 0)
        self.assertEqual(invalid_range["total"], 0)

    def test_invalid_date_filter_returns_no_rows(self):
        self._make_events()
        body = self.client.get("/api/events?date=not-a-date").get_json()
        self.assertEqual(body["total"], 0)

    def test_device_filter(self):
        self._make_events()
        body = self.client.get("/api/dashboard/events?device_id=other").get_json()
        self.assertEqual(body["total"], 0)

    def test_active_count_tracks_unresolved_alerts(self):
        threshold_service.upsert_thresholds(
            "esp32_01", {"temperature_min": 18, "temperature_max": 25}
        )
        self._post_sensor(temperature=40)
        self.assertEqual(
            self.client.get("/api/dashboard/events").get_json()["active"], 1
        )
        self._post_sensor(temperature=22)
        self.assertEqual(
            self.client.get("/api/dashboard/events").get_json()["active"], 0
        )

    def test_pagination_clamps_page_to_last(self):
        self._make_events()
        body = self.client.get(
            "/api/dashboard/events?page=999&per_page=1"
        ).get_json()
        self.assertEqual(body["pages"], 2)
        self.assertEqual(body["page"], 2)

    def test_empty_log_reports_one_page(self):
        body = self.client.get("/api/dashboard/events").get_json()
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["pages"], 1)
        self.assertEqual(body["items"], [])


class DeviceStatusTests(_BaseCase):
    def test_recent_device_is_online(self):
        self._post_sensor()
        body = self.client.get("/api/dashboard/device-status").get_json()
        self.assertEqual(body["offline_count"], 0)
        self.assertEqual(body["devices"][0]["status"], "online")

    def test_stale_device_is_offline(self):
        self._insert_row_at(
            "dead", datetime.now() - timedelta(hours=2), soil_moisture=50
        )
        body = self.client.get("/api/dashboard/device-status").get_json()
        entry = body["devices"][0]
        self.assertEqual(entry["status"], "offline")
        self.assertEqual(body["offline_count"], 1)
        self.assertGreater(entry["age_seconds"], 7000)

    def test_unparseable_time_is_unknown(self):
        self._insert_row_at("odd", "time_not_set", soil_moisture=50)
        body = self.client.get("/api/dashboard/device-status").get_json()
        self.assertEqual(body["devices"][0]["status"], "unknown")
        # 판정할 수 없는 장치는 오프라인 카운트에 포함되지 않는다.
        self.assertEqual(body["offline_count"], 0)

    def test_current_is_returned_for_selected_device(self):
        self._post_sensor(device_id="esp32_01")
        body = self.client.get(
            "/api/dashboard/device-status?device_id=esp32_01"
        ).get_json()
        self.assertEqual(body["current"]["device_id"], "esp32_01")
        self.assertEqual(len(body["devices"]), 1)

    def test_current_is_none_for_all_devices_view(self):
        self._post_sensor()
        body = self.client.get("/api/dashboard/device-status").get_json()
        self.assertIsNone(body["current"])

    def test_never_seen_device_reports_unknown(self):
        body = self.client.get(
            "/api/dashboard/device-status?device_id=never_seen"
        ).get_json()
        self.assertEqual(body["current"]["status"], "unknown")
        self.assertEqual(body["current"]["total"], 0)

    def test_threshold_is_configurable(self):
        self._insert_row_at(
            "slow", datetime.now() - timedelta(seconds=300), soil_moisture=50
        )
        with mock.patch.dict(os.environ, {"DEVICE_OFFLINE_SECONDS": "600"}):
            entries = sensor_service.list_device_status("slow")
            self.assertEqual(entries[0]["status"], "online")
        with mock.patch.dict(os.environ, {"DEVICE_OFFLINE_SECONDS": "60"}):
            entries = sensor_service.list_device_status("slow")
            self.assertEqual(entries[0]["status"], "offline")

    def test_invalid_threshold_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"DEVICE_OFFLINE_SECONDS": "not-a-number"}):
            self.assertEqual(
                sensor_service.offline_after_seconds(),
                sensor_service.DEFAULT_OFFLINE_SECONDS,
            )

    def test_zero_threshold_is_clamped(self):
        # 0을 그대로 쓰면 정상 동작 중인 장치까지 즉시 오프라인이 된다.
        with mock.patch.dict(os.environ, {"DEVICE_OFFLINE_SECONDS": "0"}):
            self.assertGreaterEqual(sensor_service.offline_after_seconds(), 10)


class OfflineWatchdogTests(_BaseCase):
    def test_offline_event_is_recorded_and_sent(self):
        self._insert_row_at(
            "dead", datetime.now() - timedelta(hours=2), soil_moisture=50
        )
        messages = alert_service.check_device_offline()
        self.assertTrue(any("dead" in m for m in messages))
        self.discord.assert_called()

        event = alert_service.list_events("dead")[0]
        self.assertEqual(event["event_type"], "device_offline")
        self.assertEqual(event["event_label"], "장치 무응답")
        self.assertEqual(event["metric_label"], "장치")
        self.assertEqual(event["status"], "abnormal")

    def test_online_device_creates_no_event(self):
        self._post_sensor()
        self.assertEqual(alert_service.check_device_offline(), [])
        self.assertEqual(alert_service.list_events("esp32_01"), [])

    def test_repeat_check_respects_cooldown(self):
        self._insert_row_at(
            "dead", datetime.now() - timedelta(hours=2), soil_moisture=50
        )
        alert_service.check_device_offline()
        with mock.patch.dict(os.environ, {"ALERT_COOLDOWN_MINUTES": "60"}):
            self.assertEqual(alert_service.check_device_offline(), [])
        self.assertEqual(alert_service.count_events("dead"), 1)

    def test_recovery_event_after_data_resumes(self):
        self._insert_row_at(
            "dead", datetime.now() - timedelta(hours=2), soil_moisture=50
        )
        alert_service.check_device_offline()
        self._post_sensor(device_id="dead")
        messages = alert_service.check_device_offline()
        self.assertTrue(any("복구" in m for m in messages))
        self.assertEqual(
            alert_service.count_events("dead", status="recovered"), 1
        )

    def test_recovery_is_not_repeated(self):
        self._insert_row_at(
            "dead", datetime.now() - timedelta(hours=2), soil_moisture=50
        )
        alert_service.check_device_offline()
        self._post_sensor(device_id="dead")
        alert_service.check_device_offline()
        self.assertEqual(alert_service.check_device_offline(), [])
        self.assertEqual(
            alert_service.count_events("dead", status="recovered"), 1
        )

    def test_unknown_time_device_is_skipped(self):
        self._insert_row_at("odd", "time_not_set", soil_moisture=50)
        self.assertEqual(alert_service.check_device_offline(), [])

    def test_watchdog_starts_only_once(self):
        with mock.patch.object(alert_service, "_watchdog_started", False):
            with mock.patch.object(alert_service.threading, "Thread") as thread:
                self.assertTrue(alert_service.start_offline_watchdog())
                self.assertFalse(alert_service.start_offline_watchdog())
                self.assertEqual(thread.call_count, 1)
                self.assertTrue(thread.call_args.kwargs["daemon"])


class CropThresholdApplyTests(_BaseCase):
    def _basil(self):
        crops = self.client.get("/api/crops").get_json()
        return next(crop for crop in crops if crop["name"] == "바질")

    def test_crop_ranges_are_copied_to_thresholds(self):
        basil = self._basil()
        response = self.client.post(
            "/api/thresholds/from-crop",
            json={"device_id": "esp32_01", "crop_id": basil["id"]},
        )
        self.assertEqual(response.status_code, 200)
        thresholds = response.get_json()["thresholds"]
        for field in (
            "temperature_min", "temperature_max",
            "humidity_min", "humidity_max",
            "soil_moisture_min", "soil_moisture_max",
            "light_min", "light_max",
        ):
            self.assertEqual(thresholds[field], basil[field], field)

    def test_esp32_read_path_sees_the_same_values(self):
        basil = self._basil()
        self.client.post(
            "/api/thresholds/from-crop",
            json={"device_id": "esp32_01", "crop_id": basil["id"]},
        )
        # ESP32가 실제로 호출하는 경로로도 같은 값이 나와야 의미가 있다.
        thresholds = self.client.get(
            "/api/thresholds?device_id=esp32_01"
        ).get_json()
        self.assertEqual(thresholds["temperature_max"], basil["temperature_max"])

    def test_device_crop_mapping_is_saved(self):
        basil = self._basil()
        self.client.post(
            "/api/thresholds/from-crop",
            json={"device_id": "esp32_01", "crop_id": basil["id"]},
        )
        assigned = self.client.get(
            "/api/crops/device?device_id=esp32_01"
        ).get_json()
        self.assertEqual(assigned["id"], basil["id"])

    def test_soil_calibration_is_preserved(self):
        # 보정값은 하드웨어 특성이라 작물을 바꿔도 유지되어야 한다.
        basil = self._basil()
        threshold_service.upsert_thresholds(
            "esp32_01", {"soil_dry_raw": 3000, "soil_wet_raw": 1200}
        )
        thresholds = self.client.post(
            "/api/thresholds/from-crop",
            json={"device_id": "esp32_01", "crop_id": basil["id"]},
        ).get_json()["thresholds"]
        self.assertEqual(thresholds["soil_dry_raw"], 3000)
        self.assertEqual(thresholds["soil_wet_raw"], 1200)

    def test_applied_thresholds_drive_alerts(self):
        crops = self.client.get("/api/crops").get_json()
        lettuce = next(crop for crop in crops if crop["name"] == "상추")
        self.client.post(
            "/api/thresholds/from-crop",
            json={"device_id": "esp32_01", "crop_id": lettuce["id"]},
        )
        # 상추 기준(15~20도)을 벗어난 값이면 알림이 새 기준으로 생겨야 한다.
        self._post_sensor(temperature=31)
        events = [
            e for e in alert_service.list_events("esp32_01")
            if e["metric"] == "temperature"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["threshold_max"], lettuce["temperature_max"])

    def test_digital_light_device_gets_mismatch_warning(self):
        basil = self._basil()
        self._post_sensor(
            device_id="esp32_dig", light=1, light_digital=1, light_unit="digital"
        )
        body = self.client.post(
            "/api/thresholds/from-crop",
            json={"device_id": "esp32_dig", "crop_id": basil["id"]},
        ).get_json()
        self.assertTrue(body["light_unit_mismatch"])
        self.assertEqual(body["light_unit"], "digital")
        self.assertEqual(body["thresholds"]["light_min"], 0)
        self.assertEqual(body["thresholds"]["light_max"], 1)

    def test_mixed_light_statistics_exclude_digital_values(self):
        self._post_sensor(device_id="esp32_lux", light=3000, light_unit="lux")
        self._post_sensor(
            device_id="esp32_dig", light=1, light_digital=1, light_unit="digital"
        )
        body = self.client.get("/api/dashboard/stats").get_json()
        self.assertEqual(body["light"], 3000)
        self.assertEqual(body["light_mode"], "mixed")
        self.assertEqual(body["light_count"], 1)
        self.assertEqual(body["digital_light_count"], 1)

    def test_digital_only_chart_does_not_report_fake_lux(self):
        self._post_sensor(
            device_id="esp32_dig", light=1, light_digital=1, light_unit="digital"
        )
        body = self.client.get(
            "/api/dashboard/chart?device_id=esp32_dig&period=hourly"
        ).get_json()
        self.assertEqual(body["light_mode"], "digital")
        self.assertTrue(all(value is None for value in body["light"]))

    def test_lux_device_has_no_mismatch_warning(self):
        basil = self._basil()
        self._post_sensor(device_id="esp32_lux", light=3000, light_unit="lux")
        body = self.client.post(
            "/api/thresholds/from-crop",
            json={"device_id": "esp32_lux", "crop_id": basil["id"]},
        ).get_json()
        self.assertFalse(body["light_unit_mismatch"])

    def test_unknown_crop_returns_404(self):
        response = self.client.post(
            "/api/thresholds/from-crop",
            json={"device_id": "esp32_01", "crop_id": 999999},
        )
        self.assertEqual(response.status_code, 404)

    def test_missing_device_id_returns_400(self):
        basil = self._basil()
        response = self.client.post(
            "/api/thresholds/from-crop", json={"crop_id": basil["id"]}
        )
        self.assertEqual(response.status_code, 400)

    def test_blank_device_id_returns_400(self):
        basil = self._basil()
        response = self.client.post(
            "/api/thresholds/from-crop",
            json={"device_id": "   ", "crop_id": basil["id"]},
        )
        self.assertEqual(response.status_code, 400)

    def test_non_integer_crop_id_returns_400(self):
        response = self.client.post(
            "/api/thresholds/from-crop",
            json={"device_id": "esp32_01", "crop_id": "1"},
        )
        self.assertEqual(response.status_code, 400)

    def test_boolean_crop_id_returns_400(self):
        # bool은 파이썬에서 int의 하위 타입이라 명시적으로 막아야 한다.
        response = self.client.post(
            "/api/thresholds/from-crop",
            json={"device_id": "esp32_01", "crop_id": True},
        )
        self.assertEqual(response.status_code, 400)

    def test_non_json_body_returns_400(self):
        response = self.client.post(
            "/api/thresholds/from-crop", data="nope", content_type="text/plain"
        )
        self.assertEqual(response.status_code, 400)


class DashboardRenderTests(_BaseCase):
    def test_new_markup_is_rendered(self):
        html = self.client.get("/dashboard").get_data(as_text=True)
        for marker in (
            'id="device-status-badge"',
            'id="events-tbody"',
            'id="events-pagination"',
            'id="events-page-input"',
            'id="events-total"',
            'id="events-active"',
            'id="crop-apply-btn"',
            'id="threshold-sync-note"',
            'id="live-light-unit"',
            'id="live-light-note"',
            'id="events-filter-metric"',
            'id="events-filter-date-from"',
            'id="events-filter-date-to"',
            'id="avg-light-unit"',
            'id="light-chart-title"',
        ):
            self.assertIn(marker, html, marker)

    def test_event_page_jump_validates_and_loads_requested_page(self):
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("function jumpToEventsPage(event)", html)
        self.assertIn("targetPage > lastPage", html)
        self.assertIn("loadEvents(targetPage)", html)

    def test_digital_device_renders_digital_unit(self):
        self._post_sensor(
            device_id="esp32_dig", light=1, light_digital=1, light_unit="digital"
        )
        html = self.client.get(
            "/dashboard?device_id=esp32_dig"
        ).get_data(as_text=True)
        self.assertIn("(디지털)", html)

    def test_lux_device_renders_lux_unit(self):
        self._post_sensor(device_id="esp32_lux", light=3000, light_unit="lux")
        html = self.client.get(
            "/dashboard?device_id=esp32_lux"
        ).get_data(as_text=True)
        self.assertIn(">lux</span>", html)


if __name__ == "__main__":
    unittest.main()
