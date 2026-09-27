"""Tests for the FinanceDatabase bulk path (financedatabase_local.download_equities / load_fd_csv_bz2 /
import_descriptions(fd_csv=...)) and the fetch-fd cooldown through ops.run_networked.

No network: the HTTP client is a fake. The equities.bz2 is built in the test from invented companies (no fixture file).
"""
from __future__ import annotations

import bz2
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import cli, config, consent, guard, ops, store  # noqa: E402
from jevscreen.http import Blocked, Halted  # noqa: E402
from jevscreen.sources import financedatabase_local as fd  # noqa: E402
from test_quickstart import FD_ROWS, fd_bz2  # noqa: E402
from test_screen import seed  # noqa: E402

EXTRA = [
    {"symbol": "OLDX", "name": "Oldex Corp", "exchange": "NMS", "delisted": "True",
     "summary": "Oldex made fax machines until it was taken private and delisted last year."},
    {"symbol": "SHRT", "name": "Short Co", "exchange": "NMS", "delisted": "False", "summary": "Short."},
    {"symbol": "ROBO-PA", "name": "RoboCorp Pref A", "exchange": "NYQ", "delisted": "False",
     "summary": "Preferred shares of RoboCorp paying a fixed quarterly dividend to holders."},
    {"symbol": "ROBO-WT", "name": "RoboCorp Warrants", "exchange": "NYQ", "delisted": "False",
     "summary": "Warrants to buy RoboCorp common shares at a fixed price until expiry."},
    {"symbol": "QNVD-R.BK", "name": "Quenvida PCL", "exchange": "SET", "delisted": "False",
     "summary": "Quenvida operates cold-chain warehouses and trucking across Thailand."},
]


class Resp:
    def __init__(self, status, body=b"", headers=None, truncated=False):
        self.status, self.body, self.headers, self.truncated = status, body, headers or {}, truncated


class FakeClient:
    def __init__(self, plan):
        self.plan, self.calls, self.requests_made = plan, [], 0

    def get(self, url, headers=None, max_bytes=None, deadline_s=None, **kw):
        self.calls.append((url, dict(headers or {})))
        self.requests_made += 1
        step = self.plan.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class FdCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cfg = config.Config(home=self.home).ensure()
        self.path = fd_bz2(self.home / "equities.bz2", FD_ROWS + EXTRA)
        env = mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def yes(self):
        consent.record(self.cfg, "gray-sources", "yes")


class TestLoad(FdCase):
    def test_rows_skips_and_shape(self):
        rows, skipped, _ = fd.load_fd_csv_bz2(self.path)
        self.assertEqual(sorted(r["listing_id"] for r in rows), ["6000.T", "BANK", "GEAR.L", "QNVD-R.BK", "ROB2",
                                                                  "ROBO"])
        self.assertEqual(skipped, {"delisted": 1, "non_equity": 2, "short_summary": 1, "fd_rows_total": 10})
        robo = next(r for r in rows if r["listing_id"] == "ROBO")
        self.assertEqual((robo["exchange"], robo["id_isin"], robo["security_type"]),
                         ("NYQ", "US0000000001", "common_stock"))
        self.assertTrue(fd.non_common_ticker("ABR-PA", "NYQ"))
        self.assertTrue(fd.non_common_ticker("XYZ-U", "NMS"))
        self.assertFalse(fd.non_common_ticker("QNVD-R.BK", "SET"))       # a Thai NVDR is the ordinary line

    def test_import_matches_and_stale_deletion_spares_the_duckdb_import(self):
        seed(self.cfg, self.home)
        self.yes()
        with store.session(self.cfg) as con:
            out = fd.import_descriptions(self.cfg, con, fd_csv=self.path)
            self.assertEqual(out["kind"], fd.BZ2_KIND)
            got = dict(con.execute("SELECT d.security_id, n.kind FROM descriptions d JOIN snapshots n USING "
                                   "(snapshot_id) WHERE d.source_id = ?", [fd.SOURCE_ID]).fetchall())
        self.assertEqual(got["NYSE:ROBO"], fd.BZ2_KIND)
        self.assertEqual(got["NASDAQ:ROB2"], fd.BZ2_KIND)
        # a second bulk file without GearCo: its bulk row goes, the earlier DuckDB-import row (TSE:6000's seed row
        # is replaced by the bulk file here, so plant one for a line the bulk file never had) stays
        with store.session(self.cfg) as con:
            snap = store.record_snapshot(con, source_id=fd.SOURCE_ID, kind=fd.KIND, request=None, raw_path=None,
                                         raw_sha256=None, raw_bytes=None, rows=1, duration_s=None)
            con.execute("INSERT OR REPLACE INTO descriptions (security_id, source_id, company_key, text, snapshot_id)"
                        " VALUES ('NYSE:NODS', ?, 'isin:US0000000005', 'NoDesc makes nothing at all today.', ?)",
                        [fd.SOURCE_ID, snap])
            fd_bz2(self.home / "v2.bz2", [r for r in FD_ROWS if r["symbol"] != "GEAR.L"])
            out = fd.import_descriptions(self.cfg, con, fd_csv=self.home / "v2.bz2")
            left = {r[0] for r in con.execute("SELECT security_id FROM descriptions WHERE source_id = ?",
                                              [fd.SOURCE_ID]).fetchall()}
        self.assertEqual(out["stale_removed"], 1)
        self.assertNotIn("LSE:GEAR", left)
        self.assertIn("NYSE:NODS", left)

    def test_consent_is_required_first(self):
        seed(self.cfg, self.home)
        client = FakeClient([])
        with self.assertRaises(consent.ConsentRequired):
            fd.download_equities(self.cfg, client)
        with store.session(self.cfg) as con, self.assertRaises(consent.ConsentRequired):
            fd.import_descriptions(self.cfg, con, fd_csv=self.path)
        self.assertEqual(client.calls, [])


class TestDownload(FdCase):
    def test_mirror_fallthrough_then_304_reuse(self):
        self.yes()
        body = self.path.read_bytes()
        client = FakeClient([OSError("timeout"), Resp(200, body, {"ETag": '"v1"', "Last-Modified": "Sat, 01 Aug"})])
        info = fd.download_equities(self.cfg, client)
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["url"], fd.FD_MIRRORS[1])
        self.assertEqual(Path(info["path"]).read_bytes(), body)
        self.assertTrue(Path(info["path"]).name.startswith("equities-"))
        self.assertIn("raw.githubusercontent.com: OSError", info["errors"])
        client = FakeClient([Resp(500), Resp(304)])
        again = fd.download_equities(self.cfg, client)
        self.assertEqual((again["status"], again["path"]), ("not_modified", info["path"]))
        self.assertEqual(client.calls[1][1].get("If-None-Match"), '"v1"')
        with store.session(self.cfg, read_only=True) as con:
            kinds = [r[0] for r in con.execute("SELECT kind FROM snapshots").fetchall()]
        self.assertEqual(kinds, [fd.DOWNLOAD_KIND])

    def test_size_cap_and_not_bz2(self):
        self.yes()
        info = fd.download_equities(self.cfg, FakeClient([Resp(200, b"BZh" + b"x" * 50)]), max_bytes=10)
        self.assertEqual(info["status"], "too_large")
        info = fd.download_equities(self.cfg, FakeClient([Resp(200, b"<html>"), Resp(404)]))
        self.assertEqual(info["status"], "error")

    def test_a_block_writes_the_fetch_fd_marker_and_the_cooldown_holds(self):
        self.yes()
        client = FakeClient([Blocked(fd.FD_MIRRORS[0], 403, "http_403"), Blocked(fd.FD_MIRRORS[1], 429, "http_429")])
        code, summ = ops.run_networked("fetch-fd", self.cfg, lambda: fd.download_equities(self.cfg, client),
                                       client=client, consent_source=fd.SOURCE_ID)
        self.assertEqual((code, summ["status"]), (2, "blocked"))
        self.assertTrue(summ["retry_after"])
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "fetch-fd"))
        client2 = FakeClient([Resp(200, self.path.read_bytes())])
        code, summ = ops.run_networked("fetch-fd", self.cfg, lambda: fd.download_equities(self.cfg, client2),
                                       client=client2, consent_source=fd.SOURCE_ID)
        self.assertEqual((code, summ["status"], client2.calls), (2, "cooldown", []))
        with store.session(self.cfg, read_only=True) as con:
            rows = con.execute("SELECT command, status FROM runs").fetchall()
        self.assertIn(("fetch-fd", "blocked"), rows)
        self.assertIn("fetch-fd", cli.RUN_COMMANDS)

    def test_a_block_on_one_mirror_tries_the_other_host(self):
        """A 403 from raw.githubusercontent.com (common on shared / CN networks) says nothing about jsDelivr."""
        self.yes()

        class HaltingClient(FakeClient):
            def __init__(self, plan):
                super().__init__(plan)
                self.halt = threading.Event()

            def get(self, url, **kw):
                if self.halt.is_set():
                    raise Halted(url)
                try:
                    return super().get(url, **kw)
                except Blocked:
                    self.halt.set()                    # what http.Client does at a block
                    raise
        client = HaltingClient([Blocked(fd.FD_MIRRORS[0], 403, "http_403"), Resp(200, self.path.read_bytes())])
        code, summ = ops.run_networked("fetch-fd", self.cfg, lambda: fd.download_equities(self.cfg, client),
                                       client=client, consent_source=fd.SOURCE_ID)
        self.assertEqual((code, summ["result"]["status"], summ["result"]["url"]), (0, "ok", fd.FD_MIRRORS[1]))
        self.assertIn("raw.githubusercontent.com: blocked (http_403)", summ["result"]["errors"])
        self.assertIsNone(guard.recent_block_marker(self.cfg, "fetch-fd"))
        self.assertIsNone(ops.cooldown(self.cfg, "fetch-fd"))

    def test_a_refusing_mirror_cools_down_even_when_the_other_delivers(self):
        """The host that refused is not asked again for 24 h (its own marker), although the run succeeded through
        the other mirror; the refusal stays in the result's errors. When every mirror is refusing or cooling down
        the run is blocked (fetch-fd marker)."""
        self.yes()
        gh = fd.FD_MIRRORS[0].split("/")[2]
        client = FakeClient([Blocked(fd.FD_MIRRORS[0], 403, "http_403"), Resp(200, self.path.read_bytes())])
        code, summ = ops.run_networked("fetch-fd", self.cfg, lambda: fd.download_equities(self.cfg, client),
                                       client=client, consent_source=fd.SOURCE_ID)
        self.assertEqual((code, summ["result"]["status"]), (0, "ok"))
        self.assertIsNotNone(fd.mirror_cooling(self.cfg, gh))
        self.assertEqual(fd.mirror_blocks(self.cfg)[gh]["http_status"], 403)
        client2 = FakeClient([Resp(200, self.path.read_bytes())])
        code, summ = ops.run_networked("fetch-fd", self.cfg, lambda: fd.download_equities(self.cfg, client2),
                                       client=client2, consent_source=fd.SOURCE_ID)
        self.assertEqual(code, 0)
        self.assertEqual([u for u, _ in client2.calls], [fd.FD_MIRRORS[1]])      # not the refusing host again
        self.assertTrue(any(e.startswith(f"{gh}: cooling down") for e in summ["result"]["errors"]))
        client3 = FakeClient([Blocked(fd.FD_MIRRORS[1], 429, "http_429")])
        code, summ = ops.run_networked("fetch-fd", self.cfg, lambda: fd.download_equities(self.cfg, client3),
                                       client=client3, consent_source=fd.SOURCE_ID)
        self.assertEqual((code, summ["status"]), (2, "blocked"))
        self.assertEqual([u for u, _ in client3.calls], [fd.FD_MIRRORS[1]])
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "fetch-fd"))

    def test_consent_missing_through_run_networked(self):
        client = FakeClient([])
        code, summ = ops.run_networked("fetch-fd", self.cfg, lambda: fd.download_equities(self.cfg, client),
                                       consent_source=fd.SOURCE_ID)
        self.assertEqual((code, summ["status"], client.calls), (ops.EXIT_CONSENT, "consent_required", []))


class TestScanSecrets(FdCase):
    def test_names_not_values(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-" + "fake" * 10,
                                          "JEVSCREEN_SEC_USER_AGENT": "Test Person tester@example.test"}):
            hits = ops.scan_secrets(self.cfg, "x sk-or-v1-" + "fake" * 10 + " and tester@example.test")
            self.assertEqual(hits, ["openrouter", "sec-email"])
            self.assertEqual(ops.scan_secrets(self.cfg, json.dumps({"a": "clean"})), [])


if __name__ == "__main__":
    unittest.main()
