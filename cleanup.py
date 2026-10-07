"""
cleanup.py -- Enforces the retention policy stated in the Privacy Policy:
generated PDFs deleted after 30 days, SMS/event logs deleted after 90 days.
Exception: offers/amendments drafted by an agent linked to a brokerage are
kept BROKERAGE_RETENTION_DAYS (5 years), the Brokerage plan's records
archive. TREC 22 TAC 535.2(h) asks brokers to keep transaction records 4
years from closing or termination; we only know the draft date, so 5 years
from creation covers a normal contract-to-close window.
Nothing else in the codebase was doing this until now -- files and event
rows just accumulated indefinitely.
"""
import os
import sqlite3
import time
import threading
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")
PDF_RETENTION_DAYS = int(os.environ.get("PDF_RETENTION_DAYS", 30))
SMS_LOG_RETENTION_DAYS = int(os.environ.get("SMS_LOG_RETENTION_DAYS", 90))
BROKERAGE_RETENTION_DAYS = int(os.environ.get("BROKERAGE_RETENTION_DAYS", 5 * 365))

_STATE_FILE = os.environ.get("CLEANUP_STATE_FILE", ".last_cleanup")
_CHECK_INTERVAL_SECONDS = 6 * 3600  # actually run cleanup at most this often


def _cleanup_due() -> bool:
    try:
        with open(_STATE_FILE) as f:
            last_run = float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return True
    return (time.time() - last_run) > _CHECK_INTERVAL_SECONDS


def _mark_cleanup_ran():
    try:
        with open(_STATE_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def cleanup_old_pdfs(output_dir: str, max_age_days: int = PDF_RETENTION_DAYS,
                     retained: set = None, retained_max_age_days: int = BROKERAGE_RETENTION_DAYS) -> int:
    """Delete generated PDFs older than max_age_days. Returns count deleted.
    Filenames in `retained` (brokerage archive) use retained_max_age_days.

    Only touches *.pdf files -- output_dir may be shared with non-PDF files
    (e.g. subscriptions.db, if DATABASE_PATH points into the same volume),
    which must never be swept up by this.
    """
    if not os.path.isdir(output_dir):
        return 0
    cutoff = time.time() - max_age_days * 86400
    retained_cutoff = time.time() - retained_max_age_days * 86400
    retained = retained or set()
    deleted = 0
    for name in os.listdir(output_dir):
        if not name.lower().endswith(".pdf"):
            continue
        path = os.path.join(output_dir, name)
        try:
            limit = retained_cutoff if name in retained else cutoff
            if os.path.isfile(path) and os.path.getmtime(path) < limit:
                os.remove(path)
                deleted += 1
        except OSError:
            continue
    return deleted


def cleanup_old_sms_logs(max_age_days: int = SMS_LOG_RETENTION_DAYS) -> int:
    """Delete `events` rows (SMS bodies + analytics events) older than max_age_days."""
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def run_cleanup_if_due(output_dir: str):
    """Called on request traffic; actually runs at most once per _CHECK_INTERVAL_SECONDS,
    in a background thread so it never delays the caller's response."""
    if not _cleanup_due():
        return
    _mark_cleanup_ran()

    def _do():
        try:
            from brokerages import get_retained_brokerage_filenames
            # If this lookup fails, skip the PDF sweep entirely rather than
            # fall back to 30 days and wipe a broker's archive.
            pdfs_deleted = cleanup_old_pdfs(output_dir, retained=get_retained_brokerage_filenames())
            logs_deleted = cleanup_old_sms_logs()
            try:
                from archive import purge_expired
                archive_deleted = purge_expired()
                if archive_deleted:
                    print(f"[cleanup] Purged {archive_deleted} archived file(s) past the 5-year retention")
            except Exception as e:
                print(f"[cleanup] Archive purge error: {e}")
            if pdfs_deleted or logs_deleted:
                print(f"[cleanup] Deleted {pdfs_deleted} old PDF(s), {logs_deleted} old SMS/event log row(s)")
        except Exception as e:
            print(f"[cleanup] Error: {e}")

    threading.Thread(target=_do, daemon=True).start()
