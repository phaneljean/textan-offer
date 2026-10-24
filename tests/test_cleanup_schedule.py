"""Retention cleanup runs on ordinary page traffic, not only /sms
(2026-10-24, txo-trec-qa C1): the privacy policy's 30-day PDFs and the
~90-day event logs on /trust are only true if cleanup actually runs.

    python3 -m unittest tests.test_cleanup_schedule
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
os.environ["ANALYTICS_PASSWORD"] = "test-internal"
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402
import cleanup  # noqa: E402


class CleanupSchedule(unittest.TestCase):
    def test_page_visit_triggers_cleanup_check(self):
        with mock.patch.object(app_module, "run_cleanup_if_due") as run:
            app_module.app.test_client().get("/trust")
        run.assert_called_once_with(app_module.OUTPUT_DIR)

    def test_old_event_rows_are_deleted(self):
        conn = sqlite3.connect(cleanup.DB_PATH)
        old = (datetime.utcnow() - timedelta(days=91)).isoformat()
        new = datetime.utcnow().isoformat()
        conn.execute("INSERT INTO events (event_type, phone, metadata, created_at) VALUES ('t_old', NULL, '{}', ?)", (old,))
        conn.execute("INSERT INTO events (event_type, phone, metadata, created_at) VALUES ('t_new', NULL, '{}', ?)", (new,))
        conn.commit()
        cleanup.cleanup_old_sms_logs()
        kinds = {r[0] for r in conn.execute("SELECT event_type FROM events WHERE event_type IN ('t_old','t_new')")}
        conn.close()
        self.assertEqual(kinds, {"t_new"})


if __name__ == "__main__":
    unittest.main()
