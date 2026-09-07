"""
tc_bulk.py -- Bulk TC File Check: a brokerage uploads a single .zip of up
to MAX_BULK_FILES closed TREC 20-19 files and gets back one aggregate
report (how many had at least one issue, which issues were most common
across the batch) instead of running the single-file checker by hand
200 times. Reuses tc_audit.check_tc_file() unchanged -- this module only
adds zip handling, batch persistence, and aggregation on top of it.

Each zip entry is checked alone (no 40-11/39-11 pairing across files --
see tc_audit.check_tc_file's docstring for why that needs each of a
contract's addendum/amendment uploaded together; a batch of unrelated
closed files has no such pairing to make). A future version could accept
a per-transaction folder structure inside the zip if that turns out to
matter for real users.

Processing runs in a background thread (see app.py's /tc-check/bulk POST
route) rather than inside the request: 200 files well within Gunicorn's
120s worker timeout individually, but not a bet worth making combined
with zip-extraction and upload transfer time on a slow connection. The
batch_id in the results URL is the access secret -- same no-login,
unguessable-token pattern as /thread/<filename> and a brokerage join_code,
not a new auth system.
"""
import json
import os
import shutil
import sqlite3
import zipfile
from html import escape

from tc_audit import check_tc_file
from tc_check_email import _email_shell, _P_STYLE, _UPSELL_HTML
from integrations import send_html_email
from analytics import track_event

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")

MAX_BULK_FILES = 200
MAX_ZIP_ENTRIES = 600  # scanned before filtering to .pdf -- rejects an absurdly padded zip outright
MAX_SINGLE_FILE_BYTES = 20 * 1024 * 1024  # one AcroForm 20-19 is a few hundred KB; 20MB is already generous
MAX_TOTAL_BYTES = 300 * 1024 * 1024  # sum of uncompressed PDF sizes actually extracted


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tc_batch_checks (
            batch_id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            file_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'processing',
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        )
    """)
    return conn


def create_batch(batch_id: str, email: str, file_count: int) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO tc_batch_checks (batch_id, email, file_count, status, created_at) "
            "VALUES (?, ?, ?, 'processing', datetime('now'))",
            (batch_id, email, file_count),
        )
        conn.commit()
    finally:
        conn.close()


def save_batch_result(batch_id: str, result: dict) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE tc_batch_checks SET status = 'done', result_json = ?, finished_at = datetime('now') WHERE batch_id = ?",
            (json.dumps(result), batch_id),
        )
        conn.commit()
    finally:
        conn.close()


def save_batch_error(batch_id: str, message: str) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE tc_batch_checks SET status = 'error', error = ?, finished_at = datetime('now') WHERE batch_id = ?",
            (message, batch_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_batch(batch_id: str):
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT email, file_count, status, result_json, error, created_at FROM tc_batch_checks WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not row:
            return None
        email, file_count, status, result_json, error, created_at = row
        return {
            "email": email,
            "file_count": file_count,
            "status": status,
            "result": json.loads(result_json) if result_json else None,
            "error": error,
            "created_at": created_at,
        }
    finally:
        conn.close()


class BulkUploadError(ValueError):
    """Raised for any zip that fails validation -- the message is safe to
    show directly to the user (no internal detail leaks)."""


def extract_pdfs_from_zip(zip_path: str, tmp_dir: str) -> list:
    """Reads the zip at zip_path and writes each recognized PDF entry out
    to its own file inside tmp_dir. Returns a list of
    {"path": str, "filename": str} dicts -- caller owns cleanup of tmp_dir.
    Raises BulkUploadError on anything that fails the size/count guards
    below; those are abuse/sanity limits, not product limits, so the
    message stays generic rather than explaining the exact threshold."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise BulkUploadError("That doesn't look like a valid .zip file.")

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise BulkUploadError(f"That zip has too many entries (over {MAX_ZIP_ENTRIES}). Split it into smaller batches.")

        pdf_infos = [
            info for info in infos
            if not info.is_dir()
            and info.filename.lower().endswith(".pdf")
            and "__MACOSX" not in info.filename
            and not os.path.basename(info.filename).startswith(".")
        ]
        if not pdf_infos:
            raise BulkUploadError("No PDF files found in that zip.")
        if len(pdf_infos) > MAX_BULK_FILES:
            raise BulkUploadError(f"That zip has {len(pdf_infos)} PDFs -- up to {MAX_BULK_FILES} per batch. Split it up and send the rest separately.")

        total_bytes = sum(info.file_size for info in pdf_infos)
        if total_bytes > MAX_TOTAL_BYTES:
            raise BulkUploadError("That batch is too large overall. Split it into smaller zips.")
        if any(info.file_size > MAX_SINGLE_FILE_BYTES for info in pdf_infos):
            raise BulkUploadError("One of the files in that zip is larger than expected for a filled contract PDF.")

        extracted = []
        for i, info in enumerate(pdf_infos):
            out_path = os.path.join(tmp_dir, f"{i}.pdf")
            with zf.open(info) as src, open(out_path, "wb") as dst:
                dst.write(src.read())
            extracted.append({"path": out_path, "filename": os.path.basename(info.filename)})
        return extracted


def run_batch(batch_id: str, files: list) -> dict:
    """files: list of {"path", "filename"} as returned by
    extract_pdfs_from_zip. Runs check_tc_file on each independently and
    returns the aggregate result dict that gets persisted and emailed.
    Does not raise -- a single unreadable file is recorded as its own
    per-file entry, not a batch-wide failure."""
    per_file = []
    issue_key_counts = {}
    issue_key_messages = {}  # first-seen human-readable message per key, for the report

    for f in files:
        try:
            result = check_tc_file([f["path"]])
        except Exception:
            per_file.append({
                "filename": f["filename"], "recognized": False, "complete": False,
                "issue_count": 0, "unreadable": True,
            })
            continue

        # Only recognized files contribute to the frequency table -- an
        # unrecognized file's single "doesn't look like a TREC 20-19"
        # pseudo-issue would otherwise outrank genuine field-completeness
        # issues and get double-surfaced (it's already visible per-file
        # via the "Unreadable" badge below).
        if result["recognized"]:
            seen_this_file = {}
            for issue in result["issues"]:
                key = issue.get("key")
                if key:
                    seen_this_file[key] = issue.get("message", key)
            for key, message in seen_this_file.items():
                issue_key_counts[key] = issue_key_counts.get(key, 0) + 1
                issue_key_messages.setdefault(key, message)

        per_file.append({
            "filename": f["filename"],
            "recognized": result["recognized"],
            "complete": result["complete"],
            "issue_count": len(result["issues"]),
            "unreadable": False,
        })

    total = len(per_file)
    recognized = [f for f in per_file if f["recognized"]]
    with_issues = [f for f in recognized if not f["complete"]]
    top_issues = sorted(issue_key_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return {
        "total_files": total,
        "recognized_count": len(recognized),
        "clean_count": len(recognized) - len(with_issues),
        "with_issues_count": len(with_issues),
        "top_issues": [{"key": k, "count": c, "message": issue_key_messages[k]} for k, c in top_issues],
        "per_file": per_file,
    }


def process_batch(batch_id: str, email: str, files: list, tmp_dir: str) -> None:
    """Full background-thread job: run the checks, persist the result,
    email it, clean up the extracted PDFs, and record one analytics event
    for the whole batch. Deliberately a single "tc_check_bulk" event, not
    one "tc_check" event per file -- get_tc_check_summary()'s existing
    issue-frequency/gate-conversion numbers describe organic one-off
    traffic, and a single batch of 200 files from one sender would swamp
    that signal if it were counted the same way. Never lets an exception
    escape (this runs with no request context to report one to)."""
    try:
        result = run_batch(batch_id, files)
        save_batch_result(batch_id, result)
        track_event("tc_check_bulk", metadata={
            "batch_id": batch_id,
            "email": email.strip().lower(),
            "total_files": result["total_files"],
            "recognized_count": result["recognized_count"],
            "with_issues_count": result["with_issues_count"],
        })
        try:
            send_html_email(
                email, "Your TC File Check batch results",
                format_batch_email_body(batch_id, result),
                format_batch_email_html(batch_id, result),
            )
        except Exception as e:
            print(f"[tc_bulk] batch report email failed for {email}: {e}")
    except Exception as e:
        save_batch_error(batch_id, "Something went wrong processing this batch.")
        print(f"[tc_bulk] batch {batch_id} failed: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _results_url(batch_id: str) -> str:
    return f"https://txtanoffer.com/tc-check/bulk/{batch_id}"


def format_batch_email_body(batch_id: str, result: dict) -> str:
    """Plain-text batch report. Deliberately states counts against the
    actual sample checked (e.g. "9 of 40 files"), never a claim about
    files generally -- a batch of specifically the files a brokerage
    already suspects are messy is not a representative sample of all
    their transactions."""
    total, recognized, clean, with_issues = (
        result["total_files"], result["recognized_count"], result["clean_count"], result["with_issues_count"],
    )
    body = (
        f"TC File Check -- batch results\n\n"
        f"Checked {total} file(s) from your upload. "
        f"{recognized} were recognized as a TREC 20-19 we could read.\n"
        f"Of those, {with_issues} of {recognized} had at least one issue; {clean} were clean.\n\n"
    )
    if result["top_issues"]:
        body += "Most common issues in this batch:\n"
        for item in result["top_issues"]:
            body += f"- ({item['count']}x) {item['message']}\n"
        body += "\n"
    body += (
        f"Full per-file breakdown: {_results_url(batch_id)}\n\n"
        "---\n"
        "Checked with TC Check by TxtAnOffer\n"
        "Want this running automatically on every file your agents submit? "
        "See the Brokerage Dashboard: https://txtanoffer.com/pricing#brokerage"
    )
    return body


def format_batch_email_html(batch_id: str, result: dict) -> str:
    total, recognized, clean, with_issues = (
        result["total_files"], result["recognized_count"], result["clean_count"], result["with_issues_count"],
    )
    body = (
        f'<p style="{_P_STYLE}">Checked <strong>{total}</strong> file(s) from your upload. '
        f"{recognized} were recognized as a TREC 20-19 we could read.</p>"
        f'<div style="background:rgba(245,158,11,0.10);border-radius:8px;padding:12px 16px;margin-bottom:20px;">'
        f'<span style="font-size:15px;font-weight:700;color:#b45309;">{with_issues} of {recognized} files had at least one issue</span>'
        f"</div>"
    )
    if result["top_issues"]:
        rows = "".join(
            f'<tr><td style="padding:7px 0;border-bottom:1px solid #f0f0ee;font-size:14px;color:#171717;">'
            f'<strong>{item["count"]}x</strong> &mdash; {escape(item["message"])}</td></tr>'
            for item in result["top_issues"]
        )
        body += (
            '<p style="margin:20px 0 8px;font-size:11px;font-weight:700;letter-spacing:0.05em;'
            'text-transform:uppercase;color:#737373;">Most common issues in this batch</p>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>'
        )
    body += (
        f'<p style="{_P_STYLE}margin-top:20px;">'
        f'<a href="{_results_url(batch_id)}" style="color:#171717;font-weight:600;">See the full per-file breakdown &rarr;</a></p>'
    )
    body += _UPSELL_HTML
    return _email_shell("Your batch results are ready", f"{total} files checked", body)
