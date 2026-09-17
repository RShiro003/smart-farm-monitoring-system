"""SQLite connection policy shared by sensor and settings services.

Callers own transactions and closing. Paths are passed at call time so each
service's DB_FILE override continues to work in deployments and isolated tests.
"""
import os
import sqlite3


def connect_database(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        conn.close()
        raise
    return conn
