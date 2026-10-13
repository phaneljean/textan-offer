"""Colorado deals get Colorado transaction tooling: checklist, Day One
summary and reminder texts come from the CBS1's own §3.1 dates -- never the
Texas option period / Paragraph 5 earnest money math.

    python3 -m unittest tests.test_co_transaction
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_PATH", os.path.join(_TMP, "test.db"))
os.environ.setdefault("OFFER_OUTPUT_DIR", os.path.join(_TMP, "offers"))

import co_audit
import offers_db  # noqa: F401 -- creates the offers table in a fresh test DB
import reminders
import transaction_tasks
from deadlines import build_co_day_one_summary
from pdf_filler import OUTPUT_DIR
from tests.co_fixtures import fill

HERE = os.path.dirname(os.path.abspath(__file__))


class CoTransaction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        cls.co_name = "CBS1_123MainSt_test_co.pdf"
        cls.co_path = os.path.join(OUTPUT_DIR, cls.co_name)
        fill(out=cls.co_path)  # CLEAN fixture: inspection objection 10/12/2026, closing 10/30/2026
        cls.values = co_audit.read_contract(cls.co_path)
        transaction_tasks.seed_default_tasks(cls.co_name, date(2026, 10, 1), 10, None, pdf_path=cls.co_path)

    def test_co_checklist_comes_from_contract(self):
        tasks = [t for t in transaction_tasks.get_tasks(self.co_name) if t["label"] != "Call appraiser"]
        labels = " | ".join(t["label"] for t in tasks).lower()
        self.assertNotIn("option", labels)
        self.assertTrue({t["stage"] for t in tasks} <= set(transaction_tasks.CO_STAGES))
        by_label = {t["label"].split(":")[0]: t["due_date"] for t in tasks}
        self.assertEqual(by_label["Inspection Objection Deadline"], "2026-10-12")
        self.assertEqual(by_label["New Loan Availability Deadline"], "2026-10-26")
        closing = [t for t in tasks if t["stage"] == "Closing"]
        self.assertTrue(closing and all(t["due_date"] == "2026-10-30" for t in closing))  # printed Closing Date

    def test_texas_checklist_unchanged(self):
        name = "TREC20-19_123MainSt_test_tx.pdf"
        transaction_tasks.seed_default_tasks(name, date(2026, 10, 1), 10, date(2026, 10, 30))
        labels = [t["label"] for t in transaction_tasks.get_tasks(name)]
        self.assertTrue(any("option period" in l.lower() for l in labels))
        self.assertEqual(transaction_tasks.stages_for(name), transaction_tasks.STAGES)

    def test_co_add_task_keeps_co_stage(self):
        transaction_tasks.add_task(self.co_name, "Appraisal", "Call appraiser")
        t = [t for t in transaction_tasks.get_tasks(self.co_name) if t["label"] == "Call appraiser"][0]
        self.assertEqual(t["stage"], "Appraisal")

    def test_co_day_one_summary(self):
        lines = build_co_day_one_summary("123 Main St", date(2026, 10, 1), self.values)
        text = "\n".join(lines)
        self.assertNotIn("option", text.lower())
        self.assertIn("Inspection Objection Deadline: Oct 12, 2026", text)
        self.assertIn("Closing Date: Oct 30, 2026", text)

    def test_reminders(self):
        conn = sqlite3.connect(reminders.DB_PATH)
        cur = conn.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(offers)")}
        if not cols:
            self.skipTest("offers table not initialised in this DB")
        cur.execute("DELETE FROM offers WHERE filename IN (?, ?)", (self.co_name, "TREC_rem_test.pdf"))
        now = datetime.utcnow().isoformat()
        for fn in (self.co_name, "TREC_rem_test.pdf"):
            cur.execute("""INSERT INTO offers (phone, address, price, down_pct, close_days, filename, created_at,
                           option_days, thread_status, thread_responded_at)
                           VALUES ('+15550001111', '123 Main St', 500000, 0.2, 30, ?, ?, 10, 'accept', ?)""",
                        (fn, now, (datetime.utcnow() - timedelta(days=2)).isoformat()))
        conn.commit()
        conn.close()
        # Texas reminders never pick up the Colorado offer.
        names = {o.get("filename") for o in reminders._due_offers_option() + reminders._due_offers_earnest_money()}
        self.assertNotIn(self.co_name, names)
        # The Colorado deadline reminder fires the day before a §3.1 date.
        with mock.patch.object(reminders, "datetime", wraps=datetime) as dt:
            dt.utcnow.return_value = datetime(2026, 10, 11, 12, 0)
            due = reminders._due_offers_co_deadlines()
        kinds = {(o["filename"], o["kind"]) for o in due}
        self.assertIn((self.co_name, "co_insp_obj"), kinds)


if __name__ == "__main__":
    unittest.main()
