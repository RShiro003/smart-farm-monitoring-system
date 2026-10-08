"""운영 기능 테스트: 장치 관리, 알림 설정, CSV 내보내기,
데이터 보존/다운샘플링, API 키 인증.

기존 테스트 모듈과 같은 방식으로 임시 DB를 쓴다.
"""
import gc
import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
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
    export_service,
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

class SeparateWebhookTests(_BaseCase):
    ALERT_URL = "https://discord.com/api/webhooks/alert/token"
    SUMMARY_URL = "https://discord.com/api/webhooks/summary/token"

    def test_concurrent_partial_saves_preserve_both_webhooks(self):
        for already_configured in (False, True):
            with self.subTest(already_configured=already_configured):
                self._clear()
                if already_configured:
                    alert_service.save_alert_settings("esp32_01", {"daily_summary": True})
                first_read = threading.Event()
                release_first = threading.Event()
                second_finished = threading.Event()
                errors = []
                read_settings = alert_service._read_settings

                def paused_read(conn, target):
                    row = read_settings(conn, target)
                    if threading.current_thread().name == "alert-webhook-save" and not first_read.is_set():
                        first_read.set()
                        if not release_first.wait(5):
                            raise RuntimeError("test did not release first save")
                    return row

                def save(values, finished=None):
                    try:
                        alert_service.save_alert_settings("esp32_01", values)
                    except Exception as error:
                        errors.append(error)
                    finally:
                        if finished is not None:
                            finished.set()

                first = threading.Thread(target=save, name="alert-webhook-save",
                                         args=({"webhook_url": self.ALERT_URL},))
                second = threading.Thread(target=save, name="summary-webhook-save",
                                          args=({"summary_webhook_url": self.SUMMARY_URL}, second_finished))
                with mock.patch.object(alert_service, "_read_settings", side_effect=paused_read):
                    try:
                        first.start()
                        self.assertTrue(first_read.wait(5))
                        second.start()
                        # Before the fix, the second save can commit after the
                        # first read but before its write. Afterward it waits.
                        second_finished.wait(0.5)
                    finally:
                        release_first.set()
                        first.join(5)
                        if second.ident is not None:
                            second.join(5)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(errors, [])
                settings = alert_service.get_alert_settings("esp32_01")
                self.assertEqual(settings["webhook_url"], self.ALERT_URL)
                self.assertEqual(settings["summary_webhook_url"], self.SUMMARY_URL)

    def test_reset_failure_is_not_reported_as_missing_settings(self):
        alert_service.save_alert_settings("esp32_01", {"summary_webhook_url": self.SUMMARY_URL})
        with mock.patch.object(alert_service, "_ensure_tables", side_effect=sqlite3.OperationalError("locked")), \
                mock.patch.object(app.logger, "exception"):
            response = self.client.delete("/api/alert-settings/esp32_01")
        self.assertEqual(response.status_code, 503)
        settings = alert_service.get_alert_settings("esp32_01")
        self.assertEqual(settings["summary_webhook_url"], self.SUMMARY_URL)

    def test_failed_save_rolls_back_and_releases_write_lock(self):
        alert_service.save_alert_settings("esp32_01", {"webhook_url": self.ALERT_URL})
        with mock.patch.object(alert_service, "_read_settings", side_effect=sqlite3.OperationalError("read failed")), \
                mock.patch.object(app.logger, "exception"):
            response = self.client.put("/api/alert-settings", json={
                "device_id": "esp32_01", "webhook_url": None,
                "summary_webhook_url": self.SUMMARY_URL,
            })
        self.assertEqual(response.status_code, 503)
        settings = alert_service.get_alert_settings("esp32_01")
        self.assertEqual(settings["webhook_url"], self.ALERT_URL)
        self.assertIsNone(settings["summary_webhook_url"])
        response = self.client.put("/api/alert-settings", json={
            "device_id": "esp32_01", "summary_webhook_url": self.SUMMARY_URL,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(alert_service.get_alert_settings("esp32_01")["summary_webhook_url"], self.SUMMARY_URL)

    def test_successful_reset_routes_queued_jobs_using_global_destinations(self):
        alert_service.save_alert_settings(None, {
            "webhook_url": self.ALERT_URL, "summary_webhook_url": self.SUMMARY_URL,
        })
        alert_service.save_alert_settings("esp32_01", {
            "webhook_url": self.ALERT_URL + "-override",
            "summary_webhook_url": self.SUMMARY_URL + "-override",
        })
        conn = alert_service._connect()
        try:
            alert_service._enqueue_notification(conn, "esp32_01", "ordinary alert")
            alert_service._enqueue_notification(conn, "esp32_01", "[스마트팜 일간 요약]\nreport",
                                                notification_type="summary")
            conn.commit()
        finally:
            conn.close()
        response = self.client.delete("/api/alert-settings/esp32_01")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(alert_service.deliver_pending_notifications(), 2)
        self.assertEqual([call.kwargs["webhook_url"] for call in self.discord.call_args_list],
                         [self.ALERT_URL, self.SUMMARY_URL])

    def test_first_device_override_preserves_effective_global_settings(self):
        alert_service.save_alert_settings(None, {
            "webhook_url": self.ALERT_URL, "summary_webhook_url": self.SUMMARY_URL,
            "temperature": False, "daily_summary": True, "summary_hour": 9,
        })
        response = self.client.put("/api/alert-settings", json={
            "device_id": "esp32_01", "weekly_summary": True,
        })
        self.assertEqual(response.status_code, 200)
        settings = alert_service.get_alert_settings("esp32_01")
        self.assertEqual(settings["webhook_url"], self.ALERT_URL)
        self.assertFalse(settings["temperature"])
        self.assertTrue(settings["daily_summary"])
        self.assertTrue(settings["weekly_summary"])
        self.assertEqual(settings["summary_hour"], 9)
        # Summary fallback must stay inherited, not become a stale URL copy.
        changed_url = self.SUMMARY_URL + "-changed"
        alert_service.save_alert_settings(None, {"summary_webhook_url": changed_url})
        settings = alert_service.get_alert_settings("esp32_01")
        self.assertEqual(settings["summary_webhook_url"], changed_url)
        self.assertTrue(settings["summary_webhook_inherited"])

    def test_settings_read_failure_does_not_send_to_environment_destination(self):
        alert_service.save_alert_settings("esp32_01", {"summary_webhook_url": self.SUMMARY_URL})
        conn = alert_service._connect()
        try:
            alert_service._enqueue_notification(conn, "esp32_01", "[스마트팜 일간 요약]\nreport",
                                                notification_type="summary")
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(alert_service, "_read_settings", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(alert_service.deliver_pending_notifications(), 0)
        self.discord.assert_not_called()
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            row = conn.execute("SELECT status, attempts, last_error FROM notification_outbox").fetchone()
            self.assertEqual(row, ("pending", 1, "Alert settings unavailable"))
            conn.execute("UPDATE notification_outbox SET next_attempt_at = '2000-01-01 00:00:00'")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(alert_service.deliver_pending_notifications(), 1)
        self.discord.assert_called_once()
        self.assertEqual(self.discord.call_args.kwargs["webhook_url"], self.SUMMARY_URL)

    def test_settings_api_returns_unavailable_instead_of_fake_defaults(self):
        with mock.patch.object(alert_service, "_read_settings", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs(app.logger, level="ERROR"):
            response = self.client.get("/api/alert-settings")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("settings", response.get_json())

    def test_first_device_override_still_honors_explicit_clear(self):
        alert_service.save_alert_settings(None, {
            "webhook_url": self.ALERT_URL, "summary_webhook_url": self.SUMMARY_URL,
        })
        response = self.client.put("/api/alert-settings", json={
            "device_id": "esp32_01", "webhook_url": None, "summary_webhook_url": None,
        })
        self.assertEqual(response.status_code, 200)
        settings = alert_service.get_alert_settings("esp32_01")
        self.assertIsNone(settings["webhook_url"])
        self.assertEqual(settings["summary_webhook_url"], self.SUMMARY_URL)
        self.assertTrue(settings["summary_webhook_inherited"])

    def test_interrupted_migration_can_retry_summary_classification(self):
        class InterruptedMigration(sqlite3.Connection):
            fail_classification = True

            def execute(self, sql, parameters=()):
                if self.fail_classification and sql.startswith("UPDATE notification_outbox SET notification_type"):
                    raise sqlite3.OperationalError("simulated migration interruption")
                return super().execute(sql, parameters)

        conn = sqlite3.connect(":memory:", factory=InterruptedMigration)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("""CREATE TABLE notification_outbox (
                id INTEGER PRIMARY KEY, device_id TEXT, message TEXT, status TEXT,
                attempts INTEGER, next_attempt_at TEXT, claimed_at TEXT, sent_at TEXT,
                last_error TEXT, created_at TEXT)""")
            conn.execute("INSERT INTO notification_outbox VALUES "
                         "(1, 'esp32_01', ?, 'pending', 2, '2000-01-01', NULL, NULL, NULL, '2000-01-01')",
                         ("[스마트팜 일간 요약]\nreport",))
            conn.commit()
            with self.assertRaises(sqlite3.OperationalError):
                alert_service._ensure_tables(conn)
            conn.rollback()
            conn.fail_classification = False
            alert_service._ensure_tables(conn)
            row = conn.execute("SELECT notification_type, status, attempts FROM notification_outbox").fetchone()
            self.assertEqual(tuple(row), ("summary", "pending", 2))
        finally:
            conn.close()

    def test_api_stores_both_urls_without_echoing_secrets(self):
        for method in (self.client.put, self.client.post):
            response = method("/api/alert-settings", json={
                "device_id": "esp32_01", "webhook_url": self.ALERT_URL,
                "summary_webhook_url": "  " + self.SUMMARY_URL + "  ",
            })
            self.assertEqual(response.status_code, 200)
            for result in (response, self.client.get("/api/alert-settings?device_id=esp32_01")):
                body = result.get_json()
                settings = body.get("settings", body)
                self.assertNotIn("webhook_url", settings)
                self.assertNotIn("summary_webhook_url", settings)
                self.assertTrue(settings["webhook_configured"])
                self.assertTrue(settings["summary_webhook_configured"])
                self.assertNotIn(self.SUMMARY_URL, result.get_data(as_text=True))
            saved = alert_service.get_alert_settings("esp32_01")
            self.assertEqual(saved["webhook_url"], self.ALERT_URL)
            self.assertEqual(saved["summary_webhook_url"], self.SUMMARY_URL)

    def test_partial_updates_and_clearing_are_independent(self):
        alert_service.save_alert_settings("esp32_01", {
            "webhook_url": self.ALERT_URL, "summary_webhook_url": self.SUMMARY_URL,
        })
        self.client.put("/api/alert-settings", json={"device_id": "esp32_01", "daily_summary": True})
        settings = alert_service.get_alert_settings("esp32_01")
        self.assertEqual(settings["webhook_url"], self.ALERT_URL)
        self.assertEqual(settings["summary_webhook_url"], self.SUMMARY_URL)
        self.client.put("/api/alert-settings", json={"device_id": "esp32_01", "summary_webhook_url": None})
        settings = alert_service.get_alert_settings("esp32_01")
        self.assertEqual(settings["webhook_url"], self.ALERT_URL)
        self.assertIsNone(settings["summary_webhook_url"])
        self.client.put("/api/alert-settings", json={
            "device_id": "esp32_01", "webhook_url": None, "summary_webhook_url": self.SUMMARY_URL,
        })
        settings = alert_service.get_alert_settings("esp32_01")
        self.assertIsNone(settings["webhook_url"])
        self.assertEqual(settings["summary_webhook_url"], self.SUMMARY_URL)

    def test_invalid_summary_url_rejects_entire_update(self):
        alert_service.save_alert_settings("esp32_01", {"webhook_url": self.ALERT_URL})
        for invalid in (123, [], {}, "http://discord.com/api/webhooks/test", "https://example.com/api/webhooks/test"):
            with self.subTest(invalid=invalid):
                response = self.client.put("/api/alert-settings", json={
                    "device_id": "esp32_01", "webhook_url": None, "summary_webhook_url": invalid,
                })
                self.assertEqual(response.status_code, 400)
                self.assertIn("summary_webhook_url", response.get_json()["details"])
                self.assertEqual(alert_service.get_alert_settings("esp32_01")["webhook_url"], self.ALERT_URL)

    def test_existing_device_inherits_global_summary_and_can_override(self):
        alert_service.save_alert_settings("esp32_01", {"webhook_url": self.ALERT_URL})
        alert_service.save_alert_settings(None, {"summary_webhook_url": self.SUMMARY_URL})
        settings = alert_service.get_alert_settings("esp32_01")
        self.assertEqual(settings["summary_webhook_url"], self.SUMMARY_URL)
        self.assertTrue(settings["summary_webhook_inherited"])
        alert_service.save_alert_settings("esp32_01", {"summary_webhook_url": self.ALERT_URL})
        settings = alert_service.get_alert_settings("esp32_01")
        self.assertEqual(settings["summary_webhook_url"], self.ALERT_URL)
        self.assertFalse(settings["summary_webhook_inherited"])
        alert_service.save_alert_settings("esp32_01", {"summary_webhook_url": None})
        self.assertEqual(alert_service.get_alert_settings("esp32_01")["summary_webhook_url"], self.SUMMARY_URL)

    def test_threshold_recovery_and_offline_use_alert_destination(self):
        alert_service.save_alert_settings("esp32_01", {
            "webhook_url": self.ALERT_URL, "summary_webhook_url": self.SUMMARY_URL,
        })
        threshold_service.upsert_thresholds("esp32_01", {"temperature_min": 18, "temperature_max": 25})
        self._post_sensor(temperature=40)
        self._post_sensor(temperature=22)
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            conn.execute("UPDATE sensor_data SET server_received_at = '2000-01-01 00:00:00'")
            conn.commit()
        finally:
            conn.close()
        alert_service.check_device_offline()
        messages = [call.args[0] for call in self.discord.call_args_list]
        self.assertTrue(any("복구" in message for message in messages))
        self.assertTrue(any("장치 무응답" in message for message in messages))
        self.assertGreaterEqual(len(messages), 3)
        self.assertTrue(all(call.kwargs["webhook_url"] == self.ALERT_URL for call in self.discord.call_args_list))

    def test_daily_weekly_routing_with_summary_override_and_legacy_fallback(self):
        for separate in (False, True):
            with self.subTest(separate=separate):
                self._clear()
                self.discord.reset_mock()
                self._insert_row_at("esp32_01", datetime(2026, 9, 15, 12), temperature=24)
                alert_service.save_alert_settings("esp32_01", {
                    "daily_summary": True, "weekly_summary": True,
                    "webhook_url": self.ALERT_URL,
                    "summary_webhook_url": self.SUMMARY_URL if separate else None,
                })
                self.assertEqual(alert_service.queue_due_summaries(datetime(2026, 9, 16, 9)), 2)
                self.assertEqual(alert_service.deliver_pending_notifications(), 2)
                expected = self.SUMMARY_URL if separate else self.ALERT_URL
                self.assertEqual([call.kwargs["webhook_url"] for call in self.discord.call_args_list], [expected, expected])

    def test_migration_preserves_old_jobs_and_settings(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript("""
                CREATE TABLE alert_settings (
                    device_id TEXT PRIMARY KEY, temperature INTEGER DEFAULT 1,
                    humidity INTEGER DEFAULT 1, soil_moisture INTEGER DEFAULT 1,
                    light INTEGER DEFAULT 1, device_offline INTEGER DEFAULT 1,
                    cooldown_minutes REAL, webhook_url TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
                    message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0, next_attempt_at TEXT NOT NULL,
                    claimed_at TEXT, sent_at TEXT, last_error TEXT, created_at TEXT NOT NULL);
            """)
            conn.execute("INSERT INTO alert_settings(device_id, webhook_url, updated_at) VALUES (?, ?, ?)",
                         ("esp32_01", self.ALERT_URL, "2026-10-01"))
            jobs = [("[스마트팜 일간 요약]\nreport", "pending", 2),
                    ("[스마트팜 주간 요약]\nreport", "sent", 1),
                    ("[스마트팜 장치 무응답]\nalert", "failed", 8)]
            for message, status, attempts in jobs:
                conn.execute("INSERT INTO notification_outbox "
                             "(device_id, message, status, attempts, next_attempt_at, created_at) "
                             "VALUES ('esp32_01', ?, ?, ?, '2026-10-01', '2026-10-01')",
                             (message, status, attempts))
            alert_service._ensure_tables(conn)
            alert_service._ensure_tables(conn)
            settings = alert_service._resolve_settings(conn, "esp32_01")
            self.assertEqual(settings["webhook_url"], self.ALERT_URL)
            self.assertIsNone(settings["summary_webhook_url"])
            rows = conn.execute("SELECT message, status, attempts, notification_type FROM notification_outbox ORDER BY id").fetchall()
            self.assertEqual([tuple(row) for row in rows],
                             [(*job, kind) for job, kind in zip(jobs, ("summary", "summary", "alert"))])
        finally:
            conn.close()

    def test_summary_only_configuration_does_not_redirect_alerts(self):
        alert_service.save_alert_settings("esp32_01", {"summary_webhook_url": self.SUMMARY_URL})
        conn = alert_service._connect()
        try:
            alert_service._enqueue_notification(conn, "esp32_01", "ordinary alert")
            alert_service._enqueue_notification(conn, "esp32_01", "[스마트팜 일간 요약]\nreport",
                                                notification_type="summary")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(alert_service.deliver_pending_notifications(), 2)
        self.assertEqual([call.kwargs["webhook_url"] for call in self.discord.call_args_list],
                         [None, self.SUMMARY_URL])


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

    def test_events_csv_uses_dashboard_date_range(self):
        conn = alert_service._connect()
        try:
            alert_service._ensure_tables(conn)
            for created_at, value in (
                ("2025-07-08 10:00:00", 41),
                ("2025-08-19 11:00:00", 42),
                ("2025-09-01 09:00:00", 43),
            ):
                conn.execute(
                    """
                    INSERT INTO event_log (
                        device_id, event_type, metric, value,
                        threshold_min, threshold_max, message,
                        status, severity, created_at
                    ) VALUES ('esp32_sensor', 'threshold_above', 'temperature', ?,
                              18, 25, 'historical event',
                              'abnormal', 'warning', ?)
                    """,
                    (value, created_at),
                )
            conn.commit()
        finally:
            conn.close()

        text = self.client.get(
            "/api/export/events.csv?device_id=esp32_sensor"
            "&date_from=2025-07-01&date_to=2025-08-31"
        ).get_data(as_text=True)
        self.assertIn("2025-07-08", text)
        self.assertIn("2025-08-19", text)
        self.assertNotIn("2025-09-01", text)

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

class CsvAndTimeRegressionTests(_BaseCase):
    def _seed_times(self):
        stamps = [f"2025-{month:02d}-08 {clock}" for month in (7, 8)
                  for clock in ("09:59:59", "10:00:00", "10:30:00", "10:30:59", "10:31:00", "15:00:00")]
        conn = alert_service._connect()
        try:
            alert_service._ensure_tables(conn)
            conn.executemany("INSERT INTO sensor_data (device_id, server_received_at) VALUES ('esp32_sensor', ?)",
                             ((stamp,) for stamp in stamps))
            conn.executemany(
                "INSERT INTO event_log (device_id, metric, event_type, status, message, created_at) "
                "VALUES ('esp32_sensor', 'temperature', 'threshold_above', 'abnormal', 'test', ?)",
                ((stamp,) for stamp in stamps),
            )
            conn.commit()
        finally:
            conn.close()
        return stamps

    def _assert_table_and_csv(self, kind, query, expected):
        query = dict(query, device_id="esp32_sensor")
        path = "/api/dashboard/history" if kind == "sensor" else "/api/events"
        response = self.client.get(path, query_string=dict(query, per_page=200))
        body = response.get_json()
        self.assertEqual(body["total"], len(expected))
        field = "server_received_at" if kind == "sensor" else "created_at"
        self.assertEqual(sorted(item[field] for item in body["items"]), sorted(expected))
        exported = self.client.get(f"/api/export/{kind}.csv", query_string=query)
        rows = list(csv.reader(io.StringIO(exported.get_data(as_text=True).lstrip("\ufeff"))))
        self.assertEqual(sorted(row[0] for row in rows[1:]), sorted(expected))
        exported.close()

    def test_end_minute_is_included_with_date_and_without_date(self):
        stamps = self._seed_times()
        for kind in ("sensor", "events"):
            for day in (None, "2025-08-08"):
                with self.subTest(kind=kind, day=day):
                    query = {"time_from": "10:00", "time_to": "10:30"}
                    if day:
                        query["date"] = day
                    expected = [s for s in stamps if "10:00" <= s[11:16] <= "10:30" and (not day or s[:10] == day)]
                    self._assert_table_and_csv(kind, query, expected)

    def test_same_minute_and_second_precision(self):
        stamps = self._seed_times()
        for kind in ("sensor", "events"):
            for clock in ("10:30", "10:30:59"):
                with self.subTest(kind=kind, clock=clock):
                    expected = [s for s in stamps if s[11:11 + len(clock)] == clock]
                    self._assert_table_and_csv(kind, {"time_from": clock, "time_to": clock}, expected)

    def test_time_applies_to_every_day_of_event_date_range(self):
        stamps = self._seed_times()
        self._assert_table_and_csv("events", {
            "date_from": "2025-07-01", "date_to": "2025-08-31", "time_from": "10:00", "time_to": "10:30",
        }, [s for s in stamps if "10:00" <= s[11:16] <= "10:30"])

    def test_one_sided_date_does_not_drop_opposite_time_bound(self):
        stamps = self._seed_times()
        self._assert_table_and_csv("events", {"date_from": "2025-08-01", "time_to": "10:30"},
                                   [s for s in stamps if s[:10] >= "2025-08-01" and s[11:16] <= "10:30"])
        self._assert_table_and_csv("events", {"date_to": "2025-07-31", "time_from": "10:30"},
                                   [s for s in stamps if s[:10] <= "2025-07-31" and s[11:16] >= "10:30"])

    def test_invalid_or_reversed_time_never_broadens_results(self):
        self._seed_times()
        for kind in ("sensor", "events"):
            for query in ({"time_from": "invalid"}, {"time_to": "24:00"},
                          {"time_from": "15:00", "time_to": "10:00"}):
                with self.subTest(kind=kind, query=query):
                    self._assert_table_and_csv(kind, query, [])

    def test_sensor_date_filter_uses_same_legacy_timestamp_as_time_filter(self):
        self._insert_row_at("esp32_sensor", "", time="2025-08-08T10:30:59")
        query = {"device_id": "esp32_sensor", "date": "2025-08-08", "time_from": "10:30", "time_to": "10:30"}
        body = self.client.get("/api/dashboard/history", query_string=query).get_json()
        self.assertEqual(body["total"], 1)
        response = self.client.get("/api/export/sensor.csv", query_string=query)
        self.assertEqual(len(list(csv.reader(io.StringIO(response.get_data(as_text=True))))), 2)
        response.close()

    def test_sensor_export_includes_august_after_200000_july_rows(self):
        conn = sensor_service._connect()
        try:
            conn.executemany("INSERT INTO sensor_data (device_id, server_received_at) VALUES (?, ?)",
                             (("esp32_sensor", "2025-07-08 10:00:00") for _ in range(200000)))
            conn.execute("INSERT INTO sensor_data (device_id, server_received_at) VALUES ('esp32_sensor', '2025-08-08 10:30:59')")
            conn.commit()
        finally:
            conn.close()
        count = 0
        for line in export_service.stream_sensor_csv("esp32_sensor"):
            count += 1
        self.assertEqual(count, 200002)  # Header + every stored row.
        self.assertIn("2025-08-08", line)

    def test_event_export_does_not_truncate_at_200000_rows(self):
        conn = alert_service._connect()
        try:
            alert_service._ensure_tables(conn)
            conn.executemany(
                "INSERT INTO event_log (device_id, metric, event_type, status, message, created_at) "
                "VALUES ('esp32_sensor', 'temperature', 'threshold_above', 'abnormal', 'test', ?)",
                (("2025-08-08 10:00:00",) for _ in range(200001)),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(sum(1 for _ in export_service.stream_events_csv("esp32_sensor")), 200002)

    def test_csv_query_failure_propagates_and_closes_connection(self):
        with mock.patch.object(sensor_service, "_connect") as connect:
            connect.return_value.execute.side_effect = sqlite3.OperationalError("test failure")
            stream = export_service.stream_sensor_csv()
            next(stream)  # Header.
            with self.assertRaises(sqlite3.OperationalError):
                next(stream)
            connect.return_value.close.assert_called_once()


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

    def test_period_apis_return_the_period_the_server_applied(self):
        for period in ("daily", "weekly", "monthly"):
            body = self.client.get(
                f"/api/dashboard/stats?period={period}"
            ).get_json()
            self.assertEqual(body["period"], period)

        for period in ("hourly", "daily", "weekly", "monthly"):
            body = self.client.get(
                f"/api/dashboard/chart?period={period}"
            ).get_json()
            self.assertEqual(body["period"], period)

    def test_period_statistics_use_different_time_windows(self):
        now = datetime.now()
        samples = (
            (now - timedelta(hours=2), 10),
            (now - timedelta(days=2), 20),
            (now - timedelta(days=10), 40),
        )
        for received_at, temperature in samples:
            self._insert_row_at(
                "esp32_01", received_at, temperature=temperature,
                humidity=temperature, soil_moisture=temperature,
                light=temperature, light_unit="lux",
            )

        daily = self.client.get(
            "/api/dashboard/stats?device_id=esp32_01&period=daily"
        ).get_json()
        weekly = self.client.get(
            "/api/dashboard/stats?device_id=esp32_01&period=weekly"
        ).get_json()
        monthly = self.client.get(
            "/api/dashboard/stats?device_id=esp32_01&period=monthly"
        ).get_json()

        self.assertEqual((daily["count"], daily["temperature"]), (1, 10.0))
        self.assertEqual((weekly["count"], weekly["temperature"]), (2, 15.0))
        self.assertEqual((monthly["count"], monthly["temperature"]), (3, 23.3))

    def test_dashboard_discards_late_period_responses(self):
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("period !== currentChartPeriod || data.period !== period", html)
        self.assertIn("period !== currentAvgPeriod || d.period !== period", html)


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

    def test_queued_daily_and_weekly_summaries_deliver_as_distinct_cards(self):
        from app.services import discord_alert_service

        for day, temperature in ((13, 20), (15, 24)):
            self._insert_row_at(
                "esp32_01", datetime(2026, 9, day, 12),
                temperature=temperature, light=3500, light_unit="lux",
            )
        alert_service.save_alert_settings("esp32_01", {
            "daily_summary": True, "weekly_summary": True,
            "summary_hour": 8, "summary_weekday": 0,
            "webhook_url": "https://discord.com/api/webhooks/test-id/test-token",
        })
        now = datetime(2026, 9, 16, 9)
        self.assertEqual(alert_service.queue_due_summaries(now), 2)
        self.assertEqual(alert_service.queue_due_summaries(now), 0)

        self.discord.side_effect = discord_alert_service.send_discord_message
        with mock.patch.object(discord_alert_service.urllib.request, "urlopen") as send:
            send.return_value.__enter__.return_value.status = 204
            self.assertEqual(alert_service.deliver_pending_notifications(), 2)
            self.assertEqual(alert_service.deliver_pending_notifications(), 0)
        embeds = [json.loads(call.args[0].data)["embeds"][0] for call in send.call_args_list]
        self.assertEqual(len(embeds), 2)
        self.assertEqual([item["color"] for item in embeds], [0x3498DB, 0x9B59B6])
        self.assertIn("일간 요약 · 1일", embeds[0]["title"])
        self.assertIn("기간: 2026-09-15 ~ 2026-09-15 23:59", embeds[0]["description"])
        self.assertIn("평균 24", embeds[0]["description"])
        self.assertIn("주간 요약 · 7일", embeds[1]["title"])
        self.assertIn("기간: 2026-09-07 ~ 2026-09-13 23:59", embeds[1]["description"])
        self.assertIn("평균 20", embeds[1]["description"])
        conn = sqlite3.connect(_SENSOR_DB)
        try:
            rows = conn.execute(
                "SELECT message, status, attempts FROM notification_outbox ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        for row, embed in zip(rows, embeds):
            self.assertEqual(row[0].split("\n", 1)[1], embed["description"])
            self.assertEqual(row[1:], ("sent", 1))

    def test_split_summary_retries_only_failed_parts_including_legacy_jobs(self):
        from app.services.discord_alert_service import split_discord_message

        message = "[스마트팜 주간 요약]\n" + "앞" * 4096 + "중" * 4096 + "끝"
        parts = split_discord_message(message)
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self._clear()
                summary_url = "https://discord.com/api/webhooks/summary/token"
                alert_service.save_alert_settings("esp32_01", {
                    "webhook_url": "https://discord.com/api/webhooks/alert/token",
                    "summary_webhook_url": summary_url,
                })
                conn = sqlite3.connect(_SENSOR_DB)
                try:
                    if legacy:
                        # A pre-split job after the schema migration has classified it.
                        conn.execute(
                            "INSERT INTO notification_outbox "
                            "(device_id, message, notification_type, status, attempts, next_attempt_at, created_at) "
                            "VALUES (?, ?, 'summary', 'pending', 0, ?, ?)",
                            ("esp32_01", message, "2000-01-01 00:00:00", "2000-01-01 00:00:00"),
                        )
                    else:
                        alert_service._enqueue_notification(conn, "esp32_01", message,
                                                            notification_type="summary")
                    conn.commit()
                finally:
                    conn.close()
                self.discord.reset_mock()
                self.discord.side_effect = [True, False, True]
                self.assertEqual(alert_service.deliver_pending_notifications(), 2)
                self.assertEqual([call.args[0] for call in self.discord.call_args_list], parts)
                self.assertTrue(all(call.kwargs["webhook_url"] == summary_url
                                    for call in self.discord.call_args_list))
                conn = sqlite3.connect(_SENSOR_DB)
                try:
                    rows = conn.execute(
                        "SELECT message, status, attempts FROM notification_outbox ORDER BY id"
                    ).fetchall()
                    self.assertEqual(rows, [(parts[0], "sent", 1),
                                            (parts[1], "pending", 1), (parts[2], "sent", 1)])
                    conn.execute("UPDATE notification_outbox SET next_attempt_at = ? WHERE status = 'pending'",
                                 ("2000-01-01 00:00:00",))
                    conn.commit()
                finally:
                    conn.close()
                self.discord.reset_mock()
                self.discord.side_effect = [True]
                self.assertEqual(alert_service.deliver_pending_notifications(), 1)
                self.assertEqual(alert_service.deliver_pending_notifications(), 0)
                self.assertEqual(self.discord.call_args.args[0], parts[1])
                self.assertEqual(self.discord.call_args.kwargs["webhook_url"], summary_url)
                self.discord.assert_called_once()

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


class RefactoringRegressionTests(_BaseCase):
    def test_database_wrappers_resolve_paths_at_call_time(self):
        for module in _PINNED:
            with self.subTest(module=module.__name__):
                with mock.patch.object(module, "DB_FILE", "test-override.db"):
                    with mock.patch.object(module, "connect_database") as connect:
                        self.assertIs(module._connect(), connect.return_value)
                        connect.assert_called_once_with("test-override.db")

    def test_alert_settings_reject_non_finite_values_without_saving(self):
        for field in ("cooldown_minutes", "danger_deviation_percent", "abnormal_count"):
            for value in (float("nan"), float("inf"), -float("inf"), "NaN", "Infinity"):
                with self.subTest(field=field, value=value):
                    with mock.patch("app.routes.management.save_alert_settings") as save:
                        response = self.client.put("/api/alert-settings", json={field: value})
                        self.assertEqual(response.status_code, 400)
                        save.assert_not_called()

    def test_alert_settings_reject_non_string_device_without_saving(self):
        for value in ([], {}, 0, False, 42, ["esp32_01"]):
            with self.subTest(value=value):
                with mock.patch("app.routes.management.save_alert_settings") as save:
                    response = self.client.put("/api/alert-settings", json={"device_id": value})
                    self.assertEqual(response.status_code, 400)
                    save.assert_not_called()

    def test_alert_settings_keep_null_global_and_numeric_string_compatibility(self):
        response = self.client.put("/api/alert-settings", json={
            "device_id": None, "cooldown_minutes": "12.5", "abnormal_count": "3",
        })
        self.assertEqual(response.status_code, 200)
        settings = alert_service.get_alert_settings()
        self.assertEqual(settings["cooldown_minutes"], 12.5)
        self.assertEqual(settings["abnormal_count"], 3)

    def test_malformed_retention_payload_never_runs_maintenance(self):
        for payload in ([], ["raw_days"], False, 0, "", "invalid"):
            with self.subTest(payload=payload):
                with mock.patch.object(retention_service, "run_maintenance") as maintenance:
                    response = self.client.post("/api/maintenance/retention", json=payload)
                    self.assertEqual(response.status_code, 400)
                    maintenance.assert_not_called()

    def test_non_finite_retention_days_never_run_maintenance(self):
        for value in (float("inf"), float("nan"), -float("inf")):
            with self.subTest(value=value):
                with mock.patch.object(retention_service, "run_maintenance") as maintenance:
                    response = self.client.post("/api/maintenance/retention", json={"raw_days": value})
                    self.assertEqual(response.status_code, 400)
                    maintenance.assert_not_called()

    def test_work_type_containers_return_validation_error(self):
        for value in ([], {}, ["watering"]):
            response = self.client.post("/api/work-logs", json={
                "device_id": "esp32_01", "work_type": value,
            })
            self.assertEqual(response.status_code, 400)

    def test_crop_assignment_rejects_invalid_types(self):
        for payload in ({"device_id": [], "crop_id": 1},
                        {"device_id": {}, "crop_id": 1},
                        {"device_id": 42, "crop_id": 1},
                        {"device_id": "esp32_01", "crop_id": True}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.put("/api/crops/device", json=payload).status_code, 400)

    def test_huge_growth_value_returns_validation_error(self):
        response = self.client.post("/api/growth", json={
            "device_id": "esp32_01", "height_cm": 10**400,
        })
        self.assertEqual(response.status_code, 400)

    def test_auth_rejects_container_device_ids_without_server_error(self):
        env = {"SMART_FARM_API_KEY": "admin", "SMART_FARM_DEVICE_KEYS": '{"esp32_01":"node"}'}
        with mock.patch.dict(os.environ, env):
            for value in ([], {}, ["esp32_01"]):
                for key, expected_status in (("node", 401), ("admin", 400)):
                    with self.subTest(value=value, key=key):
                        response = self.client.post("/api/sensor", json={"device_id": value},
                                                    headers={"X-API-Key": key})
                        self.assertEqual(response.status_code, expected_status)

    def test_non_ascii_key_comparison_is_safe(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            for value in ("잘못된키", "\ud800", [], None):
                self.assertFalse(auth_service.key_is_valid(value))
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "테스트키"}):
            self.assertTrue(auth_service.key_is_valid("테스트키"))

    def test_malformed_and_expired_sessions_are_rejected(self):
        with mock.patch.dict(os.environ, {"SMART_FARM_API_KEY": "secret"}):
            with mock.patch.object(auth_service.time, "time", return_value=1700000000):
                token = auth_service.session_cookie_value()
                self.assertTrue(auth_service.session_cookie_is_valid(token))
                issued, signature = token.split(".")
                unicode_digits = "".join(chr(ord(c) + 0xFEE0) for c in issued)
                for invalid in (f"{issued}.서명", f"{unicode_digits}.{signature}", "bad", [], None):
                    self.assertFalse(auth_service.session_cookie_is_valid(invalid))
            with mock.patch.object(auth_service.time, "time", return_value=1700000000 + 43201):
                self.assertFalse(auth_service.session_cookie_is_valid(token))

    def test_device_list_accepts_one_shot_iterables(self):
        device_service.upsert_device("registered")
        devices = device_service.list_devices(iter(["registered", "unregistered"]))
        self.assertEqual({item["device_id"] for item in devices}, {"registered", "unregistered"})
        self.assertTrue(all(item["has_data"] for item in devices))

    def test_non_finite_environment_intervals_use_defaults(self):
        for value in ("nan", "inf", "-inf", "invalid"):
            with mock.patch.dict(os.environ, {
                "ALERT_COOLDOWN_MINUTES": value,
                "DEVICE_OFFLINE_SECONDS": value,
                "DEVICE_OFFLINE_CHECK_SECONDS": value,
            }):
                self.assertEqual(alert_service._env_cooldown_minutes(), alert_service.DEFAULT_COOLDOWN_MINUTES)
                self.assertEqual(sensor_service.offline_after_seconds(), sensor_service.DEFAULT_OFFLINE_SECONDS)
                self.assertEqual(alert_service._offline_check_seconds(), alert_service.DEFAULT_OFFLINE_CHECK_SECONDS)
                self.assertEqual(retention_service._env_int("DEVICE_OFFLINE_SECONDS", 90), 90)

    def test_very_large_cooldown_does_not_overflow_timedelta(self):
        self.assertFalse(alert_service._cooldown_elapsed("2026-01-01 00:00:00", datetime(2026, 1, 2), 1e100))


if __name__ == "__main__":
    unittest.main()
