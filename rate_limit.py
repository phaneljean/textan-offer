"""
rate_limit.py — Simple per-key request throttle backed by SQLite.

Gunicorn runs this app with 3 worker processes (see Procfile), so an
in-memory counter would only limit each worker independently -- roughly
3x the intended rate, since requests get distributed across workers. This
uses the same DB_PATH pattern as every other module in this app
(offers_db.py, subscriptions.py, etc.): SQLite on Railway's persistent
volume, which is genuinely shared across all worker processes.

Fixed-window, not sliding -- good enough for an abuse guard (protecting
shared infrastructure from a flood), not billing-grade accuracy.
"""
import os
import sqlite3
import time
import random

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")
WINDOW_SECONDS = 3600  # 1 hour


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            bucket_key TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bucket_key, window_start)
        )
    """)
    return conn


def check_and_increment(key: str, limit: int, window_seconds: int = WINDOW_SECONDS) -> bool:
    """Records one request for `key` and returns True if it's within
    `limit` for the current fixed window, False if `key` already hit the
    limit (the request should be rejected, e.g. with a 429)."""
    window_start = int(time.time()) // window_seconds * window_seconds
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO rate_limits (bucket_key, window_start, count) VALUES (?, ?, 1) "
            "ON CONFLICT(bucket_key, window_start) DO UPDATE SET count = count + 1",
            (key, window_start),
        )
        conn.commit()
        row = conn.execute(
            "SELECT count FROM rate_limits WHERE bucket_key = ? AND window_start = ?",
            (key, window_start),
        ).fetchone()

        # Opportunistic cleanup (~1% of calls) so this table doesn't grow
        # unbounded -- every distinct key+window is a permanent row otherwise.
        if random.random() < 0.01:
            conn.execute(
                "DELETE FROM rate_limits WHERE window_start < ?",
                (window_start - window_seconds,),
            )
            conn.commit()

        return row[0] <= limit
    finally:
        conn.close()
