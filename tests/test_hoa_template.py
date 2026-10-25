"""HOA_ADDENDUM_TEMPLATE_PATH guard (2026-10-25).

36-10.pdf was deleted when 36-11 became the template. If Railway still has
HOA_ADDENDUM_TEMPLATE_PATH pointing at it (or at any old copy), every HOA
offer would fail, so hoa_addendum only honours the env path when the file
exists and is TREC 36-11 or newer, and otherwise falls back to the bundled
36-11.pdf with a warning.

    python3 -m unittest tests.test_hoa_template
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pypdf import PdfReader

import hoa_addendum
from hoa_addendum import BUNDLED_TEMPLATE_PATH, _template_path, fill_hoa_addendum

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PARSED = {"address": "123 Main St", "city": "Austin", "state": "TX", "zip": "78701"}


def _fields(pdf_bytes):
    return PdfReader(io.BytesIO(pdf_bytes)).get_fields() or {}


class HoaTemplatePathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        # The real, retired 36-10 from git history: exists, but outdated.
        cls.old_3610 = os.path.join(cls.tmp, "36-10.pdf")
        try:
            data = subprocess.run(["git", "show", "fe41593:36-10.pdf"], cwd=_REPO,
                                  capture_output=True, check=True).stdout
            with open(cls.old_3610, "wb") as f:
                f.write(data)
        except Exception:
            cls.old_3610 = None
        cls.copy_3611 = os.path.join(cls.tmp, "custom-36-11.pdf")
        shutil.copy(os.path.join(_REPO, BUNDLED_TEMPLATE_PATH), cls.copy_3611)
        cls.not_a_pdf = os.path.join(cls.tmp, "junk.pdf")
        with open(cls.not_a_pdf, "wb") as f:
            f.write(b"not a pdf")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _resolve(self, env_value):
        env = {} if env_value is None else {"HOA_ADDENDUM_TEMPLATE_PATH": env_value}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(out):
            if env_value is None:
                os.environ.pop("HOA_ADDENDUM_TEMPLATE_PATH", None)
            return _template_path(), out.getvalue()

    def test_unset_uses_bundled(self):
        path, log = self._resolve(None)
        self.assertEqual(path, BUNDLED_TEMPLATE_PATH)
        self.assertEqual(log, "")

    def test_missing_file_falls_back_with_warning(self):
        # The exact Railway risk: env still names the deleted 36-10.pdf.
        path, log = self._resolve("/app/36-10.pdf")
        self.assertEqual(path, BUNDLED_TEMPLATE_PATH)
        self.assertIn("WARNING", log)
        self.assertIn("does not exist", log)

    def test_old_3610_falls_back_with_warning(self):
        if not self.old_3610:
            self.skipTest("36-10.pdf not in git history")
        path, log = self._resolve(self.old_3610)
        self.assertEqual(path, BUNDLED_TEMPLATE_PATH)
        self.assertIn("is 36-10", log)

    def test_unreadable_file_falls_back(self):
        path, log = self._resolve(self.not_a_pdf)
        self.assertEqual(path, BUNDLED_TEMPLATE_PATH)
        self.assertIn("no TREC 36-N form ID", log)

    def test_valid_3611_override_is_used(self):
        path, log = self._resolve(self.copy_3611)
        self.assertEqual(path, self.copy_3611)
        self.assertEqual(log, "")

    def test_bundled_3611_reads_as_revision_11(self):
        # 36-11's text also contains "TREC No. 36-10" ("This form replaces...");
        # the highest revision has to win.
        self.assertEqual(hoa_addendum._form_revision(BUNDLED_TEMPLATE_PATH), 11)

    def test_fill_still_works_with_stale_env(self):
        with mock.patch.dict(os.environ, {"HOA_ADDENDUM_TEMPLATE_PATH": "/app/36-10.pdf"}), \
                redirect_stdout(io.StringIO()):
            pdf = fill_hoa_addendum(_PARSED)
        fields = _fields(pdf)
        self.assertIn("HOA_Street Address and City", fields)
        self.assertIn("123 Main St", str(fields["HOA_Street Address and City"].get("/V")))


if __name__ == "__main__":
    unittest.main()
