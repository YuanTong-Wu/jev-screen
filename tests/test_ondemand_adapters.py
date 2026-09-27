"""Adapter hooks used by the on-demand fetch (jevscreen.ondemand): on_company per-company events (every recorded
status and the skip paths) and the codes= filters with full security ids. Reuses each adapter test's own synthetic
fixtures and fake clients; no network.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen.sources import bse, dart, mops, sec_edgar as sec  # noqa: E402
from test_bse import HAVE_FITZ  # noqa: E402
from test_bse import SyncBase as BseBase  # noqa: E402
from test_cninfo import SyncBase as CninfoBase, standard_client  # noqa: E402
from test_dart import DartSyncBase, FakeClient as DartClient, standard_pages as dart_pages  # noqa: E402
from test_edinet import SyncBase as EdinetBase  # noqa: E402
from test_mops import AS_OF as MOPS_AS_OF, FakeClient as MopsClient, SECTION as MOPS_SECTION  # noqa: E402
from test_mops import company_pages  # noqa: E402
from test_sec_edgar import FakeSecClient, doc_url, standard_pages, subs_url  # noqa: E402
from test_sec_edgar import SyncBase as SecBase  # noqa: E402


class Events(list):
    def __call__(self, sids, status, note):
        self.append((tuple(sids), status))


class TestSecHooks(SecBase):
    def test_codes_filter_same_documents_and_events(self):
        self.add_standard()
        ev = Events()
        client = FakeSecClient(standard_pages())
        s, _ = self.run_sync(client, codes=["NASDAQ:QKNW"], on_company=ev)
        self.assertEqual(s["ciks_queued"], 1)
        self.assertEqual(client.calls, [sec.TICKERS_URL, subs_url("QKNW"), doc_url("QKNW")])
        self.assertEqual(ev, [(("NASDAQ:QKNW",), "ok")])
        one = self.q("SELECT doc_id, text_sha256 FROM documents")
        self.assertEqual(s["securities_mapped"], 3)            # every considered line is still mapped
        ev2 = Events()
        s2, _ = self.run_sync(FakeSecClient(standard_pages()), on_company=ev2)       # full run
        self.assertEqual(sorted(ev2), [(("NASDAQ:QCXL",), "ok"), (("NASDAQ:QFRT",), "form_not_supported"),
                                       (("NASDAQ:QKNW",), "skipped_unchanged")])
        full = dict(self.q("SELECT doc_id, text_sha256 FROM documents"))
        self.assertEqual(full[one[0][0]], one[0][1])            # the same document as the filtered run
        self.assertEqual(sec._norm_codes("NASDAQ:QKNW, qcxl"), {"NASDAQ:QKNW", "QKNW", "QCXL"})


class TestCninfoHooks(CninfoBase):
    def test_events_per_company_and_skip(self):
        self.add_standard()
        ev = Events()
        self.run_sync(standard_client(), on_company=ev)
        self.assertEqual(sorted(ev), [(("SSE:609519",), "ok"), (("SZSE:309386",), "ok")])
        ev = Events()
        self.run_sync(standard_client(), codes=["SSE:609519"], on_company=ev, per_company=False)
        self.assertEqual(ev, [(("SSE:609519",), "skipped_unchanged")])


@unittest.skipUnless(HAVE_FITZ, "PyMuPDF not installed")
class TestBseHooks(BseBase):
    def test_events_codes_and_current_skip(self):
        self.add_standard()
        ev = Events()
        s = self.run_sync(self.client(), codes=["NSE:ALPHAPUMP"], on_company=ev)
        self.assertEqual((s["status"], s["queued"]), ("ok", 1))
        self.assertEqual(ev, [(("NSE:ALPHAPUMP",), "ok")])
        ev = Events()
        s = self.run_sync(self.client(), on_company=ev)
        self.assertIn((("NSE:ALPHAPUMP",), "skipped_current"), ev)
        self.assertIn((("BSE:BETAFOOD",), "ok"), ev)


@mock.patch.object(mops, "pdf_to_section", return_value=(MOPS_SECTION, "start:x;end:y;pages:3;backend:x"))
class TestMopsHooks(EdinetBase):
    def test_events_and_full_security_id_codes(self, _pdf):
        self.add("TWSE:9901", 5e9, "TW0009901000")
        self.add("TPEX:9902", 3e9, "TW0009902000")
        ev = Events()
        s = mops.sync(self.cfg, MopsClient(company_pages("9901")), as_of=MOPS_AS_OF, mode="annual",
                      codes=["TWSE:9901"], on_company=ev)
        self.assertEqual((s["status"], s["companies_queued"]), ("ok", 1))
        self.assertEqual(ev, [(("TWSE:9901",), "ok")])
        ev = Events()
        mops.sync(self.cfg, MopsClient(company_pages("9901")), as_of=MOPS_AS_OF, mode="annual", codes="TWSE:9901",
                  on_company=ev)
        self.assertEqual(ev, [(("TWSE:9901",), "skipped_unchanged")])


class TestDartHooks(DartSyncBase):
    def test_events_and_full_security_id_codes(self):
        self.add_standard()
        ev = Events()
        s, _ = self.run_sync(DartClient(dart_pages()), codes=["KRX:999990", "KRX:999960"], on_company=ev)
        self.assertEqual(s["companies_queued"], 2)
        self.assertEqual(sorted(ev), [(("KRX:999960",), "no_annual_filing"), (("KRX:999990",), "ok")])
        ev = Events()
        self.run_sync(DartClient(dart_pages()), codes=["KRX:999990"], on_company=ev)
        self.assertEqual(ev, [(("KRX:999990",), "skipped_unchanged")])

    def test_a_failing_callback_never_stops_the_sync(self):
        self.add_standard()

        def boom(*a):
            raise RuntimeError("callback")
        s, _ = self.run_sync(DartClient(dart_pages()), on_company=boom)
        self.assertEqual((s["status"], s["ok_documents"]), ("ok", 2))


if __name__ == "__main__":
    unittest.main()
