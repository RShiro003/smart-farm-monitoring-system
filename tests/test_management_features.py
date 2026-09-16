"""운영 기능 테스트: 장치 관리, 알림 설정, CSV 내보내기,
데이터 보존/다운샘플링, API 키 인증.

기존 테스트 모듈과 같은 방식으로 임시 DB를 쓴다.
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
    auth_service,
    crop_service,
    cultivation_service,
    device_service,
    retention_service,
    sensor_service,
    threshold_service,
    work_log_service,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SENSOR_DB = str(_REPO_ROOT / "app" / "data" / "sensor_data.db")

# 서비스 모듈의 DB_FILE은 프로세스 전역이라 여러 테스트 모듈이 공유한다.
# import 시점이 아니라 첫 setUp에서 값을 기억했다가 모듈이 끝나면 되돌린다.
_PINNED = {
    sensor_service: _SENSOR_DB,
    cultivation_service: _SENSOR_DB,
    alert_service: _SENSOR_DB,
    threshold_service: _SETTINGS_DB,
    crop_service: _SETTINGS_DB,
    device_service: _SETTINGS_DB,
}
_PRE_PIN = {}


def _pin_databases():
    for module, path in _PINNED.items():
        _PRE_PIN.setdefault(module, module.DB_FILE)
        module.DB_FILE = path


def tearDownModule():
    for module, previous in _PRE_PIN.items():
        module.DB_FILE = previous
    # 운영 DB에는 손대지 않고 경로만 복원한다. 임시 스키마는 각 setUp이 준비한다.
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
        _pin_databases()

        discord = mock.patch.object(alert_service, "send_discord_message")
        self.discord = discord.start()
        self.addCleanup(discord.stop)

        watering = mock.patch.object(sensor_service, "_detect_watering_after_save")
        watering.start()
        self.addCleanup(watering.stop)

        self.assertNotEqual(
            Path(_SENSOR_DB).resolve(), Path(_DEFAULT_SENSOR_DB).resolve()
        )
        sensor_service._init_db()
        retention_service.ensure_tables()
        work_log_service.ensure_tables()
        self._clear()

    def _clear(self):
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            for table in ("sensor_data", "event_log", "alert_state",
                          "alert_settings", "sensor_data_hourly",
                          "notification_outbox", "summary_delivery", "work_log"):
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone():
                    conn.execute(f'DELETE FROM "{table}"')
            conn.commit()
        finally:
            conn.close()

        conn = sqlite3.connect(_SETTINGS_DB)
        try:
            for table in ("devices", "threshold_settings"):
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone():
                    conn.execute(f'DELETE FROM "{table}"')
            conn.commit()
        finally:
            conn.close()

    def _post_sensor(self, **overrides):
        payload = {
            "device_id": "esp32_01", "temperature": 22, "humidity": 65,
            "soil_moisture": 55, "light": 3000, "light_unit": "lux",
        }
        payload.update(overrides)
        return self.client.post("/api/sensor", json=payload)

    def _insert_row_at(self, device_id, received_at, **columns):
        if isinstance(received_at, datetime):
            received_at = received_at.strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            cols = ["device_id", "server_received_at"] + list(columns)
            values = [device_id, received_at] + list(columns.values())
            conn.execute(
                f"INSERT INTO sensor_data ({', '.join(cols)})"
                f" VALUES ({', '.join('?' * len(cols))})",
                values,
            )
            conn.commit()
        finally:
            conn.close()


# ── ⑧ 장치 관리 ────────────────────────────────────────────────────────────────

class DeviceManagementTests(_BaseCase):
    def test_register_device_before_any_data(self):
        # 데이터가 오기 전에 미리 등록할 수 있어야 한다.
        response = self.client.post("/api/devices", json={
            "device_id": "esp32_new", "label": "북쪽 온실", "location": "A동 3열",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["label"], "북쪽 온실")

        devices = self.client.get("/api/devices").get_json()["devices"]
        entry = next(d for d in devices if d["device_id"] == "esp32_new")
        self.assertTrue(entry["registered"])
        self.assertFalse(entry["has_data"])
        self.assertEqual(entry["display_name"], "북쪽 온실 (esp32_new)")

    def test_unregistered_device_with_data_still_listed(self):
        # 등록을 강제하면 기존 장치가 목록에서 사라진다. 그래서는 안 된다.
        self._post_sensor(device_id="esp32_legacy")
        devices = self.client.get("/api/devices").get_json()["devices"]
        entry = next(d for d in devices if d["device_id"] == "esp32_legacy")
        self.assertFalse(entry["registered"])
        self.assertTrue(entry["has_data"])
        self.assertEqual(entry["display_name"], "esp32_legacy")

    def test_update_keeps_created_at(self):
        first = self.client.post("/api/devices", json={
            "device_id": "esp32_01", "label": "처음"}).get_json()
        second = self.client.post("/api/devices", json={
            "device_id": "esp32_01", "label": "나중"}).get_json()
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(second["label"], "나중")

    def test_blank_label_clears_to_none(self):
        self.client.post("/api/devices",
                         json={"device_id": "esp32_01", "label": "이름"})
        result = self.client.post("/api/devices",
                                  json={"device_id": "esp32_01", "label": "   "})
        self.assertIsNone(result.get_json()["label"])

    def test_long_text_is_truncated(self):
        result = self.client.post("/api/devices", json={
            "device_id": "esp32_01", "label": "가" * 200}).get_json()
        self.assertEqual(len(result["label"]), device_service.MAX_LABEL_LENGTH)

    def test_delete_removes_metadata_but_keeps_sensor_rows(self):
        self._post_sensor(device_id="esp32_01")
        self.client.post("/api/devices",
                         json={"device_id": "esp32_01", "label": "이름"})
        response = self.client.delete("/api/devices/esp32_01")
        self.assertEqual(response.status_code, 200)

        # 센서 기록은 남아야 한다. 메타데이터 삭제가 데이터 손실이 되면 안 된다.
        rows = sensor_service.list_sensor_records("esp32_01")
        self.assertEqual(len(rows), 1)
        devices = self.client.get("/api/devices").get_json()["devices"]
        entry = next(d for d in devices if d["device_id"] == "esp32_01")
        self.assertFalse(entry["registered"])

    def test_delete_unknown_returns_404(self):
        self.assertEqual(self.client.delete("/api/devices/nope").status_code, 404)

    def test_missing_device_id_returns_400(self):
        self.assertEqual(
            self.client.post("/api/devices", json={"label": "x"}).status_code, 400
        )

    def test_non_string_label_returns_400(self):
        response = self.client.post("/api/devices",
                                    json={"device_id": "a", "label": 5})
        self.assertEqual(response.status_code, 400)


# ── ⑨ 알림 설정 ────────────────────────────────────────────────────────────────

class AlertSettingsTests(_BaseCase):
    def test_defaults_are_all_enabled(self):
        body = self.client.get("/api/alert-settings").get_json()
        settings = body["settings"]
        for field in alert_service.ALERT_TOGGLE_FIELDS:
            self.assertTrue(settings[field], field)
        self.assertEqual(settings["source"], "default")

    def test_disabling_metric_suppresses_alert(self):
        threshold_service.upsert_thresholds(
            "esp32_01", {"temperature_min": 18, "temperature_max": 25})
        self.client.put("/api/alert-settings",
                        json={"device_id": "esp32_01", "temperature": False})
        self._post_sensor(temperature=40)
        events = [e for e in alert_service.list_events("esp32_01")
                  if e["metric"] == "temperature"]
        self.assertEqual(events, [])

    def test_enabled_metric_still_alerts(self):
        threshold_service.upsert_thresholds(
            "esp32_01", {"temperature_min": 18, "temperature_max": 25})
        self.client.put("/api/alert-settings",
                        json={"device_id": "esp32_01", "humidity": False})
        self._post_sensor(temperature=40)
        events = [e for e in alert_service.list_events("esp32_01")
                  if e["metric"] == "temperature"]
        self.assertEqual(len(events), 1)

    def test_global_setting_applies_to_unconfigured_device(self):
        threshold_service.upsert_thresholds(
            "esp32_01", {"temperature_min": 18, "temperature_max": 25})
        self.client.put("/api/alert-settings", json={"temperature": False})
        self._post_sensor(temperature=40)
        self.assertEqual(alert_service.count_events("esp32_01"), 0)

    def test_device_setting_overrides_global(self):
        threshold_service.upsert_thresholds(
            "esp32_01", {"temperature_min": 18, "temperature_max": 25})
        self.client.put("/api/alert-settings", json={"temperature": False})
        self.client.put("/api/alert-settings",
                        json={"device_id": "esp32_01", "temperature": True})
        self._post_sensor(temperature=40)
        self.assertEqual(alert_service.count_events("esp32_01"), 1)

    def test_delete_device_settings_falls_back_to_global(self):
        self.client.put("/api/alert-settings",
                        json={"device_id": "esp32_01", "temperature": False})
        self.assertEqual(
            self.client.delete("/api/alert-settings/esp32_01").status_code, 200)
        settings = self.client.get(
            "/api/alert-settings?device_id=esp32_01").get_json()["settings"]
        self.assertTrue(settings["temperature"])

    def test_partial_update_keeps_other_fields(self):
        self.client.put("/api/alert-settings",
                        json={"device_id": "esp32_01", "temperature": False,
                              "humidity": False})
        self.client.put("/api/alert-settings",
                        json={"device_id": "esp32_01", "humidity": True})
        settings = self.client.get(
            "/api/alert-settings?device_id=esp32_01").get_json()["settings"]
        self.assertFalse(settings["temperature"])
        self.assertTrue(settings["humidity"])

    def test_custom_cooldown_is_used(self):
        self.client.put("/api/alert-settings",
                        json={"device_id": "esp32_01", "cooldown_minutes": 0})
        threshold_service.upsert_thresholds(
            "esp32_01", {"temperature_min": 18, "temperature_max": 25})
        self._post_sensor(temperature=40)
        self._post_sensor(temperature=41)
        # 쿨타임 0이면 지속 중에도 매번 다시 기록된다.
        self.assertEqual(alert_service.count_events("esp32_01"), 2)

    def test_custom_webhook_is_passed_to_sender(self):
        self.client.put("/api/alert-settings", json={
            "device_id": "esp32_01",
            "webhook_url": "https://discord.com/api/webhooks/abc"})
        threshold_service.upsert_thresholds(
            "esp32_01", {"temperature_min": 18, "temperature_max": 25})
        self._post_sensor(temperature=40)
        self.discord.assert_called()
        self.assertEqual(
            self.discord.call_args.kwargs["webhook_url"],
            "https://discord.com/api/webhooks/abc",
        )

    def test_http_webhook_is_rejected(self):
        response = self.client.put(
            "/api/alert-settings", json={"webhook_url": "http://example.com/x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("webhook_url", response.get_json()["details"])

    def test_non_discord_https_webhook_is_rejected(self):
        response = self.client.put(
            "/api/alert-settings",
            json={"webhook_url": "https://example.com/api/webhooks/abc"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("webhook_url", response.get_json()["details"])

    def test_non_boolean_toggle_is_rejected(self):
        response = self.client.put("/api/alert-settings",
                                   json={"temperature": "yes"})
        self.assertEqual(response.status_code, 400)

    def test_out_of_range_cooldown_is_rejected(self):
        response = self.client.put("/api/alert-settings",
                                   json={"cooldown_minutes": 99999})
        self.assertEqual(response.status_code, 400)

    def test_offline_alert_can_be_disabled(self):
        self._insert_row_at("dead", datetime.now() - timedelta(hours=2),
                            soil_moisture=50)
        self.client.put("/api/alert-settings",
                        json={"device_id": "dead", "device_offline": False})
        self.assertEqual(alert_service.check_device_offline(), [])


# ── ⑦ CSV 내보내기 ─────────────────────────────────────────────────────────────

class CsvExportTests(_BaseCase):
    def test_sensor_csv_has_header_and_rows(self):
        self.client.post("/api/devices",
                         json={"device_id": "esp32_01", "label": "북쪽"})
        self._post_sensor(temperature=21.5)
        response = self.client.get("/api/export/sensor.csv")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("수신 시각", text)
        self.assertIn("21.5", text)
        # 장치 별칭이 함께 들어가야 분석할 때 식별자만 보고 헤매지 않는다.
        self.assertIn("북쪽 (esp32_01)", text)

    def test_sensor_csv_is_attachment_with_bom(self):
        self._post_sensor()
        response = self.client.get("/api/export/sensor.csv")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertTrue(response.get_data().startswith(b"\xef\xbb\xbf"))

    def test_sensor_csv_respects_device_filter(self):
        self._post_sensor(device_id="esp32_01")
        self._post_sensor(device_id="esp32_02")
        text = self.client.get(
            "/api/export/sensor.csv?device_id=esp32_01").get_data(as_text=True)
        self.assertIn("esp32_01", text)
        self.assertNotIn("esp32_02", text)

    def test_sensor_csv_respects_date_filter(self):
        self._insert_row_at("esp32_01", "2020-01-01 10:00:00", temperature=5)
        self._post_sensor(temperature=30)
        text = self.client.get(
            "/api/export/sensor.csv?date=2020-01-01").get_data(as_text=True)
        self.assertIn("2020-01-01", text)
        self.assertNotIn("30", text.split("\n", 1)[1] or "")

    def test_events_csv_contains_korean_labels(self):
        threshold_service.upsert_thresholds(
            "esp32_01", {"temperature_min": 18, "temperature_max": 25})
        self._post_sensor(temperature=40)
        text = self.client.get("/api/export/events.csv").get_data(as_text=True)
        self.assertIn("발생 시각", text)
        self.assertIn("온도", text)
        self.assertIn("이상", text)

    def test_events_csv_empty_still_has_header(self):
        text = self.client.get("/api/export/events.csv").get_data(as_text=True)
        self.assertIn("발생 시각", text)

    def test_export_streams_without_loading_all_rows(self):
        # 생성기를 그대로 전달하므로 응답이 스트리밍 모드여야 한다.
        self._post_sensor()
        response = self.client.get("/api/export/sensor.csv")
        self.assertTrue(response.is_streamed)

    def test_sensor_csv_neutralizes_spreadsheet_formulas(self):
        self._post_sensor(device_id="=1+1")
        text = self.client.get("/api/export/sensor.csv").get_data(as_text=True)
        self.assertIn("'=1+1", text)


# ── ⑤ 데이터 보존 / 다운샘플링 ─────────────────────────────────────────────────

class RetentionTests(_BaseCase):
    def _old(self, hours):
        return datetime.now() - timedelta(hours=hours)

    def test_rollup_aggregates_into_hourly_buckets(self):
        base = self._old(5).replace(minute=0, second=0, microsecond=0)
        for i in range(3):
            self._insert_row_at("esp32_01", base + timedelta(minutes=i * 5),
                                temperature=20 + i, humidity=60,
                                soil_moisture=50, light=1000,
                                light_unit="lux")
        retention_service.rollup_hourly()

        conn = sqlite3.connect(_SENSOR_DB)
        try:
            row = conn.execute(
                "SELECT sample_count, temperature_avg, temperature_min,"
                " temperature_max, lux_samples FROM sensor_data_hourly"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 3)
        self.assertAlmostEqual(row[1], 21.0)
        self.assertEqual(row[2], 20)
        self.assertEqual(row[3], 22)
        self.assertEqual(row[4], 3)

    def test_rollup_excludes_current_hour(self):
        # 진행 중인 시간대를 집계하면 아직 안 들어온 데이터 때문에 평균이 틀어진다.
        self._post_sensor()
        retention_service.rollup_hourly()
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM sensor_data_hourly").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_rollup_is_idempotent(self):
        self._insert_row_at("esp32_01", self._old(5), temperature=20)
        retention_service.rollup_hourly()
        retention_service.rollup_hourly()
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM sensor_data_hourly").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_rollup_excludes_digital_light_from_average(self):
        base = self._old(5).replace(minute=0, second=0, microsecond=0)
        self._insert_row_at("mix", base, light=5000, light_unit="lux")
        self._insert_row_at("mix", base + timedelta(minutes=1),
                            light=1, light_unit="digital")
        retention_service.rollup_hourly()
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            row = conn.execute(
                "SELECT light_avg, lux_samples, digital_samples,"
                " sample_count FROM sensor_data_hourly").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 5000)   # 디지털 1이 평균을 끌어내리지 않는다
        self.assertEqual((row[1], row[2], row[3]), (1, 1, 2))

    def test_purge_requires_rollup_first(self):
        # 집계되지 않은 오래된 행은 지우지 않는다. 지우면 영구 손실이다.
        self._insert_row_at("esp32_01", datetime.now() - timedelta(days=200),
                            temperature=20)
        deleted = retention_service.purge_raw(days=90)
        self.assertEqual(deleted, 0)
        self.assertEqual(len(sensor_service.list_sensor_records("esp32_01")), 1)

    def test_purge_after_rollup_removes_old_rows(self):
        self._insert_row_at("esp32_01", datetime.now() - timedelta(days=200),
                            temperature=20)
        retention_service.rollup_hourly()
        deleted = retention_service.purge_raw(days=90)
        self.assertEqual(deleted, 1)
        self.assertEqual(len(sensor_service.list_sensor_records("esp32_01")), 0)

    def test_purge_keeps_recent_rows(self):
        self._post_sensor()
        retention_service.rollup_hourly()
        retention_service.purge_raw(days=90)
        self.assertEqual(len(sensor_service.list_sensor_records("esp32_01")), 1)

    def test_maintenance_endpoint_runs_full_cycle(self):
        self._insert_row_at("esp32_01", datetime.now() - timedelta(days=200),
                            temperature=20)
        body = self.client.post("/api/maintenance/retention",
                                json={"raw_days": 90}).get_json()
        self.assertGreaterEqual(body["rolled_up_buckets"], 1)
        self.assertEqual(body["deleted_raw_rows"], 1)

    def test_hourly_purge_removes_ancient_buckets(self):
        self._insert_row_at("esp32_01", datetime.now() - timedelta(days=900),
                            temperature=20)
        retention_service.rollup_hourly()
        removed = retention_service.purge_hourly(days=730)
        self.assertEqual(removed, 1)

    def test_storage_stats_reports_counts(self):
        self._post_sensor()
        stats = self.client.get("/api/maintenance/storage").get_json()
        self.assertEqual(stats["raw_rows"], 1)
        self.assertGreater(stats["database_bytes"], 0)
        self.assertEqual(stats["raw_retention_days"],
                         retention_service.DEFAULT_RAW_RETENTION_DAYS)

    def test_invalid_retention_days_rejected(self):
        response = self.client.post("/api/maintenance/retention",
                                    json={"raw_days": 0})
        self.assertEqual(response.status_code, 400)

    def test_rollup_endpoint_does_not_delete(self):
        self._insert_row_at("esp32_01", datetime.now() - timedelta(days=200),
                            temperature=20)
        self.client.post("/api/maintenance/rollup")
        self.assertEqual(len(sensor_service.list_sensor_records("esp32_01")), 1)


# ── ⑥ 인증 / API 키 ────────────────────────────────────────────────────────────

class AuthTests(_BaseCase):
    def test_disabled_by_default(self):
        status = self.client.get("/api/auth/status").get_json()
        self.assertFalse(status["enabled"])
        # 키가 없으면 쓰기도 그대로 통과해야 한다(기존 환경 보호).
        self.assertEqual(self._post_sensor().status_code, 201)

    def test_write_blocked_without_key(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            self.assertEqual(self._post_sensor().status_code, 401)

    def test_write_allowed_with_header(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            response = self.client.post(
                "/api/sensor",
                json={"device_id": "esp32_01", "temperature": 22,
                      "humidity": 65, "soil_moisture": 55, "light": 3000},
                headers={"X-API-Key": "secret"})
            self.assertEqual(response.status_code, 201)

    def test_bearer_token_accepted(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            response = self.client.post(
                "/api/sensor",
                json={"device_id": "esp32_01", "temperature": 22,
                      "humidity": 65, "soil_moisture": 55, "light": 3000},
                headers={"Authorization": "Bearer secret"})
            self.assertEqual(response.status_code, 201)

    def test_query_string_key_is_rejected(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            response = self.client.post(
                "/api/sensor?api_key=secret",
                json={"device_id": "esp32_01", "temperature": 22,
                      "humidity": 65, "soil_moisture": 55, "light": 3000})
            self.assertEqual(response.status_code, 401)

    def test_dashboard_login_cookie_allows_writes(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            login = self.client.post(
                "/api/auth/login", json={"api_key": "secret"}
            )
            self.assertEqual(login.status_code, 200)
            self.assertIn("HttpOnly", login.headers["Set-Cookie"])
            self.assertIn("SameSite=Strict", login.headers["Set-Cookie"])
            self.assertEqual(self._post_sensor().status_code, 201)

    def test_invalid_dashboard_login_is_rejected(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            response = self.client.post(
                "/api/auth/login", json={"api_key": "wrong"}
            )
            self.assertEqual(response.status_code, 401)

    def test_logout_removes_dashboard_session(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            self.client.post("/api/auth/login", json={"api_key": "secret"})
            self.assertEqual(self._post_sensor().status_code, 201)
            self.client.post("/api/auth/logout")
            self.assertEqual(self._post_sensor().status_code, 401)

    def test_wrong_key_rejected(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            response = self.client.post(
                "/api/sensor",
                json={"device_id": "esp32_01", "temperature": 22,
                      "humidity": 65, "soil_moisture": 55, "light": 3000},
                headers={"X-API-Key": "wrong"})
            self.assertEqual(response.status_code, 401)

    def test_reads_open_by_default_when_key_set(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            self.assertEqual(
                self.client.get("/api/dashboard/latest").status_code, 200)

    def test_reads_can_be_protected(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret",
                                          "SMART_FARM_PROTECT_READS": "true"}):
            self.assertEqual(
                self.client.get("/api/dashboard/latest").status_code, 401)
            self.assertEqual(
                self.client.get("/api/dashboard/latest",
                                headers={"X-API-Key": "secret"}).status_code, 200)

    def test_status_endpoint_always_open(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret",
                                          "SMART_FARM_PROTECT_READS": "true"}):
            self.assertEqual(self.client.get("/api/status").status_code, 200)

    def test_protected_status_omits_sensor_data_without_auth(self):
        self._post_sensor()
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret",
                                          "SMART_FARM_PROTECT_READS": "true"}):
            body = self.client.get("/api/status").get_json()
            self.assertNotIn("latest", body)
            authorized = self.client.get(
                "/api/status", headers={"X-API-Key": "secret"}
            ).get_json()
            self.assertIn("latest", authorized)

    def test_protected_dashboard_shell_does_not_embed_sensor_data(self):
        self._post_sensor(device_id="private-device-marker")
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret",
                                          "SMART_FARM_PROTECT_READS": "true"}):
            anonymous = self.client.get("/dashboard").get_data(as_text=True)
            self.assertIn("auth-modal", anonymous)
            self.assertNotIn("private-device-marker", anonymous)

            self.client.post("/api/auth/login", json={"api_key": "secret"})
            authorized = self.client.get("/dashboard").get_data(as_text=True)
            self.assertIn("private-device-marker", authorized)

    def test_key_is_never_exposed(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "topsecret"}):
            body = self.client.get("/api/auth/status").get_data(as_text=True)
            self.assertNotIn("topsecret", body)


class DashboardRenderTests(_BaseCase):
    def test_new_panels_are_rendered(self):
        html = self.client.get("/dashboard").get_data(as_text=True)
        for marker in ('id="device-manage-btn"', 'id="alert-settings-card"',
                       'id="export-sensor-btn"', 'id="storage-card"',
                       'id="work-log-card"'):
            self.assertIn(marker, html, marker)

    def test_remote_device_control_is_not_exposed(self):
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertNotIn('id="actuator-card"', html)
        self.assertNotIn("/api/actuators", html)
        self.assertEqual(
            self.client.get("/api/actuators?device_id=esp32_01").status_code,
            404,
        )


class AlertConfirmationSummaryAndWorkLogTests(_BaseCase):
    def _events(self, status=None):
        return alert_service.list_events(
            "esp32_01", per_page=100, status=status
        )

    def _process(self, temperature, timestamp):
        alert_service.process_sensor_alerts({
            "device_id": "esp32_01",
            "temperature": temperature,
            "humidity": 65,
            "soil_moisture": 55,
            "light": 3000,
            "light_unit": "lux",
            "server_received_at": timestamp,
        })

    def test_consecutive_and_duration_confirmation(self):
        alert_service.save_alert_settings("esp32_01", {
            "abnormal_count": 3,
            "abnormal_duration_seconds": 60,
            "danger_immediate": False,
        })
        self._process(26, "2026-09-16 10:00:00")
        self._process(26, "2026-09-16 10:00:30")
        self.assertEqual(self._events("abnormal"), [])
        self._process(26, "2026-09-16 10:01:00")
        self.assertEqual(len(self._events("abnormal")), 1)

    def test_recovery_requires_configured_consecutive_samples(self):
        alert_service.save_alert_settings("esp32_01", {
            "recovery_count": 2,
            "danger_immediate": False,
        })
        self._process(26, "2026-09-16 10:00:00")
        self._process(22, "2026-09-16 10:00:05")
        self.assertEqual(self._events("recovered"), [])
        self._process(22, "2026-09-16 10:00:10")
        self.assertEqual(len(self._events("recovered")), 1)

    def test_danger_can_bypass_confirmation_delay(self):
        alert_service.save_alert_settings("esp32_01", {
            "abnormal_count": 10,
            "abnormal_duration_seconds": 600,
            "danger_deviation_percent": 25,
            "danger_immediate": True,
        })
        self._process(40, "2026-09-16 10:00:00")
        event = self._events("abnormal")[0]
        self.assertEqual(event["severity"], "danger")
        self.assertIn("위험", event["message"])

    def test_alert_rule_and_summary_settings_api(self):
        response = self.client.put("/api/alert-settings", json={
            "device_id": "esp32_01",
            "abnormal_count": 4,
            "recovery_count": 2,
            "abnormal_duration_seconds": 90,
            "danger_deviation_percent": 30,
            "danger_immediate": True,
            "daily_summary": True,
            "weekly_summary": True,
            "summary_hour": 9,
            "summary_weekday": 2,
        })
        self.assertEqual(response.status_code, 200)
        body = self.client.get(
            "/api/alert-settings?device_id=esp32_01"
        ).get_json()["settings"]
        self.assertEqual(body["abnormal_count"], 4)
        self.assertTrue(body["daily_summary"])
        self.assertEqual(body["summary_weekday"], 2)

        invalid = self.client.put("/api/alert-settings", json={
            "abnormal_count": 0, "summary_hour": 24,
        })
        self.assertEqual(invalid.status_code, 400)

    def test_daily_summary_is_queued_once(self):
        self._insert_row_at(
            "esp32_01", datetime(2026, 9, 15, 12),
            temperature=24, humidity=66, soil_moisture=52,
            light=3500, light_unit="lux",
        )
        alert_service.save_alert_settings("esp32_01", {
            "daily_summary": True, "summary_hour": 8,
        })
        now = datetime(2026, 9, 16, 9)
        self.assertEqual(alert_service.queue_due_summaries(now), 1)
        self.assertEqual(alert_service.queue_due_summaries(now), 0)
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            message = conn.execute(
                "SELECT message FROM notification_outbox"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIn("스마트팜 일간 요약", message)
        self.assertIn("평균 24", message)

    def test_work_log_crud_api(self):
        created = self.client.post("/api/work-logs", json={
            "device_id": "esp32_01", "work_type": "watering",
            "note": "물 300mL", "occurred_at": "2026-09-16T10:30",
        })
        self.assertEqual(created.status_code, 201)
        entry = created.get_json()
        self.assertEqual(entry["work_type_label"], "급수")

        items = self.client.get(
            "/api/work-logs?device_id=esp32_01"
        ).get_json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["note"], "물 300mL")

        self.assertEqual(
            self.client.delete(f"/api/work-logs/{entry['id']}").status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/api/work-logs?device_id=esp32_01")
            .get_json()["items"], []
        )

    def test_work_log_rejects_invalid_type(self):
        response = self.client.post("/api/work-logs", json={
            "device_id": "esp32_01", "work_type": "remote_pump",
        })
        self.assertEqual(response.status_code, 400)


class HardeningRegressionTests(_BaseCase):
    def test_sensitive_get_routes_require_auth_when_key_is_configured(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            self.assertEqual(self.client.get("/api/alert-settings").status_code, 401)

    def test_device_key_is_bound_to_its_device(self):
        env = {
            "SMART_FARM_API_KEY": "admin-secret",
            "SMART_FARM_DEVICE_KEYS": '{"esp32_01":"node-one","esp32_02":"node-two"}',
        }
        with mock.patch.dict(os.environ, env):
            own = self.client.post(
                "/api/sensor",
                json={"device_id": "esp32_01", "temperature": 22,
                      "humidity": 65, "soil_moisture": 55, "light": 3000},
                headers={"X-API-Key": "node-one"},
            )
            wrong = self.client.post(
                "/api/sensor",
                json={"device_id": "esp32_02", "temperature": 22,
                      "humidity": 65, "soil_moisture": 55, "light": 3000},
                headers={"X-API-Key": "node-one"},
            )
        self.assertEqual(own.status_code, 201)
        self.assertEqual(wrong.status_code, 401)

    def test_alert_settings_never_return_webhook_secret(self):
        alert_service.save_alert_settings(
            "", {"webhook_url": "https://discord.com/api/webhooks/1/secret"}
        )
        body = self.client.get("/api/alert-settings").get_json()["settings"]
        self.assertNotIn("webhook_url", body)
        self.assertTrue(body["webhook_configured"])

    def test_non_finite_values_are_rejected(self):
        self.assertEqual(self._post_sensor(temperature=float("nan")).status_code, 400)
        payload = {
            "device_id": "esp32_01",
            "temperature_min": float("inf"), "temperature_max": 25,
            "humidity_min": 60, "humidity_max": 80,
            "soil_moisture_min": 40, "soil_moisture_max": 70,
            "light_min": 0, "light_max": 100,
        }
        self.assertEqual(self.client.put("/api/thresholds", json=payload).status_code, 400)

    def test_partial_dht_failure_preserves_other_metrics(self):
        response = self._post_sensor(
            temperature=None, humidity=None, sensor_errors=["dht_read_failed"]
        )
        self.assertEqual(response.status_code, 201)
        saved = sensor_service.get_latest_sensor_record("esp32_01")
        self.assertNotIn("temperature", saved)
        self.assertEqual(saved["soil_moisture"], 55)
        self.assertEqual(saved["sensor_errors"], ["dht_read_failed"])

    def test_sensor_sample_id_is_idempotent(self):
        self.assertEqual(self._post_sensor(sample_id="boot-1").status_code, 201)
        self.assertEqual(self._post_sensor(sample_id="boot-1").status_code, 201)
        self.assertEqual(len(sensor_service.list_sensor_records("esp32_01")), 1)

    def test_failed_notification_remains_in_outbox(self):
        self.discord.return_value = False
        self._post_sensor(temperature=40)
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            row = conn.execute(
                "SELECT status, attempts FROM notification_outbox"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("pending", 1))

    def test_registered_never_seen_device_becomes_offline(self):
        device_service.upsert_device("new-node")
        conn = sqlite3.connect(_SETTINGS_DB)
        try:
            conn.execute(
                "UPDATE devices SET created_at = '2020-01-01 00:00:00' "
                "WHERE device_id = 'new-node'"
            )
            conn.commit()
        finally:
            conn.close()
        messages = alert_service.check_device_offline()
        self.assertTrue(any("new-node" in message for message in messages))

    def test_vendored_chart_library_is_served(self):
        response = self.client.get("/static/vendor/chart.umd.min.js")
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.data), 100000)
        response.close()

    def test_long_term_chart_reads_hourly_rollup_after_raw_purge(self):
        old = datetime.now() - timedelta(days=120)
        self._insert_row_at(
            "esp32_01", old, temperature=23, humidity=64,
            soil_moisture=51, light=4000, light_unit="lux",
        )
        retention_service.rollup_hourly()
        retention_service.purge_raw(days=90)
        rows = sensor_service.get_sensor_rows_for_chart(
            "esp32_01", period="monthly"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["temperature"], 23)

    def test_daily_analysis_reads_hourly_rollup_after_raw_purge(self):
        old = datetime.now() - timedelta(days=120)
        self._insert_row_at(
            "esp32_01", old, temperature=23, humidity=64,
            soil_moisture=51, light=4000, light_unit="lux",
        )
        retention_service.rollup_hourly()
        retention_service.purge_raw(days=90)
        rows = cultivation_service.get_daily_analysis("esp32_01", days=365)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["temperature_avg"], 23)


if __name__ == "__main__":
    unittest.main()
