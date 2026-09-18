"""Pure helper regression tests: no application import or production DB access."""
import sqlite3
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.routes.query_filters import event_filters
from app.services.database import connect_database
from app.services.validation import finite_number, positive_int, required_device_id
from app.services.datetime_filters import datetime_conditions


class SharedHelperTests(unittest.TestCase):
    def test_device_id_normalization_preserves_existing_rules(self):
        for value in (None, False, 0, 123, [], {}, "", " \t\n"):
            with self.subTest(value=value):
                self.assertIsNone(required_device_id(value))
        for value, expected in ((" esp32_01 ", "esp32_01"), ("a b", "a b"),
                                ("장치", "장치"), ("x" * 100, "x" * 100)):
            self.assertEqual(required_device_id(value), expected)

    def test_datetime_filters_cover_leap_day_and_maximum_date(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE samples (stamp TEXT)")
            conn.executemany("INSERT INTO samples VALUES (?)", [
                ("2024-02-29 23:59:59.500",), ("2024-03-01 00:00:00",),
                ("9999-12-31 23:59:59.999999",),
            ])
            for query, expected in (({"date": "2024-02-29", "time_to": "23:59"}, 1),
                                    ({"date": "2026-02-29"}, 0),
                                    ({"date": "9999-12-31"}, 1),
                                    ({"date_from": "2026-08-25", "date_to": "2026-07-25"}, 0)):
                with self.subTest(query=query):
                    clauses, params = datetime_conditions("stamp", **query)
                    result = conn.execute("SELECT COUNT(*) FROM samples WHERE " + " AND ".join(clauses), params).fetchone()[0]
                    self.assertEqual(result, expected)
        finally:
            conn.close()

    def test_mixed_time_precision_keeps_end_minute_inclusive(self):
        conn = sqlite3.connect(":memory:")
        try:
            for query, expected in (({"time_from": "10:30:59", "time_to": "10:30"}, 1),
                                    ({"time_from": "10:31", "time_to": "10:30:59"}, 0)):
                clauses, params = datetime_conditions("'2026-08-25 10:30:59.500'", **query)
                self.assertEqual(conn.execute("SELECT COUNT(*) WHERE " + " AND ".join(clauses), params).fetchone()[0], expected)
        finally:
            conn.close()

    def test_both_application_import_modes_use_isolated_databases(self):
        root = Path(__file__).resolve().parents[1]
        for working_dir, module in ((root, "app.main"), (root / "app", "main")):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ, SMART_FARM_SENSOR_DB_FILE=str(Path(directory) / "sensor.db"),
                           SMART_FARM_DB_FILE=str(Path(directory) / "settings.db"),
                           SMART_FARM_API_KEY="", SMART_FARM_DEVICE_KEYS="")
                code = (
                    f"from {module} import app; "
                    "app.config['TESTING'] = True; client = app.test_client(); "
                    "assert client.get('/api/status').status_code == 200; "
                    "assert client.get('/dashboard').status_code == 200"
                )
                result = subprocess.run([sys.executable, "-B", "-c", code], cwd=working_dir,
                                        env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_connection_policy_and_parent_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect_database(Path(directory) / "nested" / "test.db")
            try:
                self.assertIs(conn.row_factory, sqlite3.Row)
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
                conn.execute("CREATE TABLE example (value INTEGER)")
                conn.execute("INSERT INTO example VALUES (1)")
                conn.rollback()
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM example").fetchone()[0], 0)
            finally:
                conn.close()

    def test_connection_closes_when_setup_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("app.services.database.sqlite3.connect") as connect:
                connect.return_value.execute.side_effect = sqlite3.OperationalError("locked")
                with self.assertRaises(sqlite3.OperationalError):
                    connect_database(Path(directory) / "test.db")
                connect.return_value.close.assert_called_once()

    def test_finite_number_compatibility(self):
        for value, expected in (("12.5", 12.5), (0, 0), (-5, -5), (" 42 ", 42)):
            self.assertEqual(finite_number(value), expected)
        for value in (True, False, "NaN", "inf", 10**400, None, [], {}):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    finite_number(value)

    def test_page_normalization_keeps_defaults_and_bounds(self):
        for value, expected in (("3", 3), (0, 1), (-2, 1), ("bad", 10), (None, 10),
                                (float("inf"), 10), (float("nan"), 10), (999, 100)):
            self.assertEqual(positive_int(value, 10, 100), expected)

    def test_event_filter_parser_preserves_legacy_and_range_filters(self):
        filters = event_filters({"status": " abnormal ", "metric": " temperature ",
                                 "date": "2026-07-01", "date_from": "2026-07-01",
                                 "date_to": "2026-08-31", "time_from": " 09:00 "})
        self.assertEqual(filters, {"status": "abnormal", "metric": "temperature",
                                  "date": "2026-07-01", "date_from": "2026-07-01",
                                  "date_to": "2026-08-31", "time_from": "09:00", "time_to": ""})
        self.assertIsNone(event_filters({})["status"])
