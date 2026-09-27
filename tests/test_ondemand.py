"""Tests for jevscreen.ondemand (plan / launch / child / outcomes / text), the screen update pass (supersedes,
fetch_info), the CLI (screen --fetch-docs, fetch-docs), the report's evidence section, calib's 新抓年报 cause and the
doctor checks.

No network: children run tests/ondemand_fake_adapter.py (JEVSCREEN_TESTING=1), or launch() gets a fake popen and a
fake clock. Every Jev client is a fake (test_screen.FakeJev). Issuers and texts are invented.
"""
from __future__ import annotations

import contextlib
import csv
import dataclasses
import datetime as dt
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "src"))  # run without `pip install -e .`
sys.path.insert(0, str(TESTS))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import (calib, cli, config, doctor, guard, ondemand, ondemand_cli, report, screen,  # noqa: E402
                       store)
from test_screen import FakeKeywords, make_factory  # noqa: E402

D = dt.date(2026, 9, 26)
SEC_COLS = ("security_id", "exchange", "symbol", "name", "isin", "country", "tv_type", "tv_subtype", "is_primary",
            "company_key", "last_seen_snapshot", "active")
# (security_id, name, isin, country, market cap, profile) -- invented issuers
COMPANIES = [
    ("NASDAQ:DOCD", "Docd Robots", "US1000000008", "United States", 9e9, "Docd builds robot vision systems."),
    ("NYSE:ACME", "Acme Robotics", "US1000000001", "United States", 8e9, "Acme builds warehouse robot fleets."),
    ("SSE:600999", "Huadong Robot", "CNE100000002", "China", 7e9, "Huadong makes industrial robot arms."),
    ("NSE:ROBIN", "Robin Automation", "INE100000003", "India", 6e9, "Robin makes robot welding cells."),
    ("TWSE:2999", "Formosa Robot", "TW0001000004", "Taiwan", 5e9, "Formosa builds robot controllers."),
    ("KRX:099999", "Hanil Robot", "KR7100000005", "Korea", 4e9, "Hanil builds service robot units."),
    ("IDX:ROBI", "Robi Indo", "ID1000000006", "Indonesia", 3e9, "Robi assembles robot kits."),
    ("TSE:7999", "Kaze Robot", "JP3100000007", "Japan", 2e9, "Kaze makes robot joints."),
]
CK = {sid: store.company_key(isin, sid) for sid, _n, isin, *_ in COMPANIES}
DOCD_TEXT = ("Item 1. Business\n\nDocd Robots designs machine vision cameras and software for robot arms used in "
             "factories and warehouses across North America and Europe.\n\nOur humanoid robots program is at an "
             "early pilot stage with two customers and is not yet a material product line.\n")
ALL_READY = {"pdf_reader": True, "backends": ["pymupdf"], "sec_email": True, "opendart": True, "mops_consent": "yes"}
NONE_READY = {"pdf_reader": False, "backends": [], "sec_email": False, "opendart": False, "mops_consent": None}


def seed(cfg: config.Config, home: Path) -> None:
    (home / "docs").mkdir(parents=True, exist_ok=True)
    (home / "docs" / "docd.txt").write_text(DOCD_TEXT, encoding="utf-8")
    with store.session(cfg) as con:
        snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="t", request=None, raw_path=None,
                                     raw_sha256=None, raw_bytes=None, rows=None, duration_s=None)
        rows, mrows, drows = [], [], []
        for sid, name, isin, country, mcap, desc in COMPANIES:
            ex, sym = sid.split(":")
            rows.append((sid, ex, sym, name, isin, country, "stock", "common", True, CK[sid], snap, True))
            mrows.append((sid, D, mcap, 1e6, snap))
            drows.append((sid, "tradingview_profile", CK[sid], desc, snap))
        store.upsert_many(con, "securities", SEC_COLS, rows)
        store.upsert_many(con, "market_daily", ("security_id", "as_of", "market_cap_usd", "avg_volume_10d",
                                                "snapshot_id"), mrows)
        store.upsert_many(con, "descriptions", ("security_id", "source_id", "company_key", "text", "snapshot_id"),
                          drows)
        store.upsert_many(con, "documents", ("doc_id", "security_id", "company_key", "source_id", "cik", "form",
                                             "section", "filing_date", "url", "text_path", "extractor"), [
            ("sec_filing_text:9:a:item1", "NASDAQ:DOCD", CK["NASDAQ:DOCD"], "sec_filing_text", "9", "10-K", "item1",
             dt.date(2026, 2, 1), "https://example.invalid/docd", str(home / "docs" / "docd.txt"), "sec_edgar-v2/x")])


L1_CORE = json.dumps({"core": 0.9, "adjacent": 0.05, "unrelated": 0.03, "insufficient": 0.02})


def make_run(cfg: config.Config, run_id: str, *, profile=None, stored=("NASDAQ:DOCD",), forced=(), out_dir=None,
             p_core: dict | None = None) -> None:
    """A finished screen run with L2 rows (input_source 'profile' or an annual report) without calling screen()."""
    profile = [s for s, *_ in COMPANIES if s not in stored] if profile is None else list(profile)
    with store.session(cfg) as con:
        con.execute("INSERT INTO screen_runs (run_id, idea, params_json, started_at, status, output_dir) VALUES "
                    "(?, 'humanoid robots', ?, ?, 'ok', ?)",
                    [run_id, json.dumps({"min_mcap_usd": 2e8}), store.now_utc(), str(out_dir or cfg.home / "out")])
        for sid in list(stored) + profile:
            pc = (p_core or {}).get(sid, 0.9)
            probs = L1_CORE if sid not in forced else json.dumps({"core": 0.01, "adjacent": 0.01, "unrelated": 0.97,
                                                                 "insufficient": 0.01})
            if sid not in forced and pc != 0.9:
                probs = json.dumps({"core": pc, "adjacent": 0.9 - pc, "unrelated": 0.05, "insufficient": 0.05})
            con.execute("INSERT INTO screen_results (run_id, company_key, security_id, layer, label, probs_json, "
                        "status) VALUES (?, ?, ?, 'l1', ?, ?, 'ok')",
                        [run_id, CK[sid], sid, "unrelated" if sid in forced else "core", probs])
            con.execute("INSERT INTO screen_results (run_id, company_key, security_id, layer, label, input_source, "
                        "status) VALUES (?, ?, ?, 'l2', 'insufficient', ?, 'ok')",
                        [run_id, CK[sid], sid, "profile" if sid in profile else "sec_filing_text:10-K"])


class Home(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cfg = config.Config(home=self.home)
        seed(self.cfg, self.home)
        # JEVSCREEN_TESTING=1: launch() / the child refuse the real adapters unless a test maps them to fakes
        self._env = mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home), "JEVSCREEN_TESTING": "1"})
        self._env.start()
        for k in ("JEVSCREEN_SEC_USER_AGENT", "JEVSCREEN_OPENDART_API_KEY"):
            os.environ.pop(k, None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def ready(self, **kw):
        return mock.patch.object(ondemand, "readiness", return_value={**ALL_READY, **kw})

    def plan(self, run_id="scr-1", **kw):
        return ondemand.plan(self.cfg, run_id, **kw)


# ---------------------------------------------------------------------------------------------------------------
class TestRoute(unittest.TestCase):
    def line(self, sid, isin=None):
        ex, sym = sid.split(":")
        return {"security_id": sid, "exchange": ex, "symbol": sym, "isin": isin}

    def test_identifier_then_venue_then_no_adapter(self):
        self.assertEqual(ondemand.route([self.line("LSE:ABC")], {"LSE:ABC": {"sec_cik": "1"}}), ("sec", None))
        self.assertEqual(ondemand.route([self.line("NYSE:ABC")], {}), ("sec", None))
        self.assertEqual(ondemand.route([self.line("SZSE:000999")], {}), ("cninfo", None))
        self.assertEqual(ondemand.route([self.line("NSE:ABC", "INE000000001")], {}), ("bse", None))
        self.assertEqual(ondemand.route([self.line("BSE:830001", "CNE000000001")], {}), ("cninfo", None))
        self.assertEqual(ondemand.route([self.line("TPEX:6999")], {}), ("mops", None))
        self.assertEqual(ondemand.route([self.line("KRX:000001")], {}), ("dart", None))
        self.assertEqual(ondemand.route([self.line("TSE:7999")], {}), (None, "edinet_pack"))
        self.assertEqual(ondemand.route([self.line("LSE:ABC")], {"LSE:ABC": {"edinet_code": "E1"}}),
                         (None, "edinet_pack"))
        self.assertEqual(ondemand.route([self.line("IDX:ABC")], {}), (None, "no_adapter"))
        self.assertEqual(ondemand.route([self.line("HKEX:9999")], {}), (None, "no_adapter"))

    def test_source_codes_keep_the_sources_own_lines(self):
        lines = [self.line("SSE:600999"), self.line("HKEX:1999")]
        self.assertEqual(ondemand.source_codes("cninfo", lines, {}), ["SSE:600999"])
        self.assertEqual(ondemand.source_codes("sec", [self.line("LSE:X")], {"LSE:X": {"sec_cik": "1"}}), ["LSE:X"])


class TestText(unittest.TestCase):
    def test_every_reason_and_note_has_both_languages(self):
        for code, r in ondemand.REASONS.items():
            for k in ("zh", "en", "short_zh", "short_en", "next_command", "ask_human"):
                self.assertIn(k, r, code)
            self.assertTrue(r["zh"] and r["en"] and r["short_zh"] and r["short_en"], code)
            for lang in ("zh", "en"):
                self.assertNotIn("{", ondemand.reason_text(code, lang, market="X", note="deadline", date="d",
                                                            days=1, source="S", cap=1, when="w", src="s"))
        for prefix, (zh, en) in ondemand.NOTE_TEXT.items():
            self.assertTrue(zh and en, prefix)
        self.assertEqual(ondemand.note_text("too_large:20480KB;x", "zh"), "年报文件太大（超过 20 MB）")
        self.assertEqual(ondemand.note_text("pdf_parse_failed:bad xref", "en"), "no text in the PDF (possibly scanned)")
        self.assertEqual(ondemand.note_text("section_not_found", "en"), "other reason")
        for key in ondemand.SOURCES:
            self.assertTrue(all(ondemand.LABEL[key]), key)

    def test_every_crawl_status_maps_to_a_reason(self):
        from jevscreen.sources import bse, cninfo, dart, edinet, mops, sec_edgar
        for mod in (sec_edgar, cninfo, bse, mops, dart, edinet):
            for st in mod.CRAWL_STATUSES:
                self.assertIn(ondemand.STATUS_TO_REASON[st], ondemand.REASONS, (mod.__name__, st))
        for st in ondemand.SKIP_STATUSES:
            self.assertEqual(ondemand.STATUS_TO_REASON[st], "known_failure")

    def test_language_and_summary_sentence(self):
        self.assertEqual((ondemand.lang_of("人形机器人"), ondemand.lang_of("humanoid robots"),
                          ondemand.lang_of("ヒューマノイドロボット")), ("zh", "en", "en"))
        fr = {"annual": 62, "fetched": 27, "profile": 20, "by_reason": {"no_adapter": 16, "deferred_time": 4}}
        self.assertEqual(ondemand.summary_sentence("zh", fr),
                         "62 家读了官方年报（其中 27 家本次新抓）；20 家只读了简介：16 家市场暂无来源，4 家时间到")
        self.assertEqual(ondemand.summary_sentence("en", fr),
                         "62 read official annual reports (27 fetched now); 20 read the profile only: 16 no source "
                         "for their market, 4 out of time")

    def test_gap_lines_are_grouped(self):
        items = [{"reason": "no_adapter", "country": "Indonesia"}, {"reason": "no_adapter", "country": "Indonesia"},
                 {"reason": "no_adapter", "country": "Vietnam"},
                 {"reason": "no_key_sec", "country": "United States", "source": "sec"},
                 {"reason": "source_paused", "country": "India", "source": "bse", "when": "2026-09-26 10:00"}]
        zh = ondemand.gap_lines(items, "zh")
        self.assertEqual(zh[0], "印尼 2 · 越南 1：这些市场还没有官方年报来源")
        self.assertIn("1 家美国公司只读了简介：未设置 SEC 联系邮箱", zh)
        self.assertTrue(any(line.startswith("1 家（印度 BSE）：该网站 2026-09-26 10:00 暂时拒绝过访问") for line in zh))
        en = ondemand.gap_lines(items, "en")
        self.assertEqual(en[0], "Indonesia 2 · Vietnam 1: no official annual-report source for these markets yet")
        self.assertIn("1 United States company read the profile only: No SEC contact email set", en)
        self.assertFalse(any(any("一" <= ch <= "鿿" for ch in line) for line in en))


# ---------------------------------------------------------------------------------------------------------------
class TestPlan(Home):
    def test_routes_and_skip_reasons(self):
        make_run(self.cfg, "scr-1")
        with self.ready():
            pl = self.plan()
        self.assertEqual(pl.stored_n, 1)
        self.assertEqual({k: [e["security_id"] for e in v] for k, v in pl.by_source.items()},
                         {"sec": ["NYSE:ACME"], "cninfo": ["SSE:600999"], "bse": ["NSE:ROBIN"],
                          "mops": ["TWSE:2999"], "dart": ["KRX:099999"]})
        self.assertEqual({(ck, r) for ck, _s, r, _n in pl.skipped},
                         {(CK["IDX:ROBI"], "no_adapter"), (CK["TSE:7999"], "edinet_pack")})
        self.assertEqual(pl.by_source["cninfo"][0]["codes"], ["SSE:600999"])

    def test_skip_order_without_keys_pdf_or_consent(self):
        make_run(self.cfg, "scr-1")
        with self.ready(**NONE_READY):
            pl = self.plan()
        self.assertEqual(pl.n_planned, 0)
        reasons = {e["security_id"]: e["reason"] for e in pl.entries.values()}
        self.assertEqual(reasons, {"NYSE:ACME": "no_key_sec", "SSE:600999": "no_pdf_reader",
                                   "NSE:ROBIN": "no_pdf_reader", "TWSE:2999": "no_pdf_reader",
                                   "KRX:099999": "no_key_dart", "IDX:ROBI": "no_adapter",
                                   "TSE:7999": "edinet_pack"})
        with self.ready(mops_consent="no"):
            pl = self.plan(sources=["sec"])
        reasons = {e["security_id"]: e["reason"] for e in pl.entries.values() if e.get("reason")}
        self.assertEqual(reasons["TWSE:2999"], "mops_off")
        self.assertEqual(reasons["SSE:600999"], "disabled")
        self.assertEqual(list(pl.by_source), ["sec"])

    def test_already_stored_now(self):
        make_run(self.cfg, "scr-1", profile=["NASDAQ:DOCD", "NYSE:ACME"], stored=())
        with self.ready():
            pl = self.plan()
        self.assertEqual([(ck, r) for ck, _s, r, _n in pl.skipped], [(CK["NASDAQ:DOCD"], "already_stored")])

    def test_source_paused_by_marker_and_by_runs_journal_alone(self):
        make_run(self.cfg, "scr-1")
        guard.mark_blocked(self.cfg, "sync-bse", url="https://example.invalid", status=403, reason="http_403")
        with store.session(self.cfg) as con:
            con.execute("INSERT INTO runs VALUES ('r1', 'sync sec_edgar', ?, ?, 'blocked', 3, 'x')",
                        [store.now_utc() - dt.timedelta(hours=2), store.now_utc()])
        with self.ready():
            pl = self.plan()
        reasons = {e["security_id"]: e.get("reason") for e in pl.entries.values()}
        self.assertEqual((reasons["NSE:ROBIN"], reasons["NYSE:ACME"]), ("source_paused", "source_paused"))
        self.assertNotIn("bse", pl.by_source)
        self.assertIn("cninfo", pl.by_source)

    def test_a_marker_with_a_utc_offset_counts(self):
        make_run(self.cfg, "scr-1")
        path = self.home / "cooldown" / "sync-mops.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        path.write_text(json.dumps({"command": "sync-mops", "blocked_at": at, "http_status": 403}), encoding="utf-8")
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, "sync-mops"))
        with self.ready():
            pl = self.plan()
        self.assertEqual(pl.entries[CK["TWSE:2999"]]["reason"], "source_paused")

    def test_store_busy_returns_quickly(self):
        make_run(self.cfg, "scr-1")
        holder = subprocess.Popen([sys.executable, "-c", "import duckdb, sys, time; c = duckdb.connect(sys.argv[1]); "
                                   "print('held', flush=True); time.sleep(20)", str(self.cfg.db_path)],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            t = time.monotonic()
            with self.ready():
                pl = self.plan(wait_s=0.5)
            self.assertTrue(pl.store_busy)
            self.assertLess(time.monotonic() - t, 5)
            self.assertEqual(pl.n_planned, 0)
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()

    def crawl(self, sid, source_id, status, days_ago, note=None, extractor=None):
        with store.session(self.cfg) as con:
            con.execute("INSERT OR REPLACE INTO crawl_state VALUES (?, ?, ?, 200, 1, ?, ?)",
                        [source_id, sid, status, store.now_utc() - dt.timedelta(days=days_ago), note])
            if extractor:
                con.execute("INSERT OR REPLACE INTO documents (doc_id, security_id, company_key, source_id, extractor, "
                            "extract_note, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [f"{source_id}:{sid}:x", sid, CK[sid], source_id, extractor, note, store.now_utc()])

    def test_negative_cache_windows(self):
        make_run(self.cfg, "scr-1")
        self.crawl("NYSE:ACME", "sec_filing_text", "no_annual_filing", 3)                 # cached (14 d)
        self.crawl("NSE:ROBIN", "bse_annual_report", "error", 2)                          # expired (1 d)
        self.crawl("SSE:600999", "cninfo_annual_report", "extract_failed", 5, "pdf_parse_failed:no PDF backend",
                   "cninfo-v3/none")                                                        # no reader then: retry
        self.crawl("KRX:099999", "dart_business_report", "extract_failed", 5, "section_too_short", "dart-v0/web")
        self.crawl("TWSE:2999", "mops_annual_report", "extract_failed", 5, "section_too_short", "mops-ar-v2")
        with self.ready():
            pl = self.plan()
        reasons = {e["security_id"]: e.get("reason") for e in pl.entries.values()}
        self.assertEqual(reasons["NYSE:ACME"], "known_failure")
        self.assertEqual(pl.entries[CK["NYSE:ACME"]]["days"], 14)
        self.assertIsNone(reasons["NSE:ROBIN"])
        self.assertIsNone(reasons["SSE:600999"])
        self.assertIsNone(reasons["KRX:099999"])          # extractor dart-v0 != the adapter's dart-v1: retried
        self.assertEqual(reasons["TWSE:2999"], "known_failure")
        with self.ready():
            pl = self.plan(retry_failed=True)
        self.assertIn("sec", pl.by_source)
        self.assertIn("mops", pl.by_source)

    def test_caps_and_forced_first_order(self):
        make_run(self.cfg, "scr-1", profile=["NYSE:ACME", "NASDAQ:DOCD"], stored=(), forced=("NASDAQ:DOCD",),
                 p_core={"NYSE:ACME": 0.6})
        (self.home / "docs" / "docd.txt").unlink()                  # DOCD's text is gone: planned again
        sec1 = dataclasses.replace(ondemand.SOURCES["sec"], cap=1)
        with self.ready(), mock.patch.dict(ondemand.SOURCES, {"sec": sec1}):
            pl = self.plan()
        self.assertEqual(pl.order[:2], [CK["NASDAQ:DOCD"], CK["NYSE:ACME"]])
        self.assertEqual([e["security_id"] for e in pl.by_source["sec"]], ["NASDAQ:DOCD"])
        self.assertEqual(pl.entries[CK["NYSE:ACME"]]["reason"], "deferred_cap")

    def test_estimates_and_speeds_override(self):
        make_run(self.cfg, "scr-1")
        with self.ready():
            pl = self.plan()
        self.assertEqual(pl.est_seconds, max(s.list_s + s.s_per_company for s in ondemand.SOURCES.values()))
        self.assertEqual(pl.est_mb, sum(s.mb_per_company for s in ondemand.SOURCES.values()))
        for _ in range(3):
            ondemand.record_speed(self.cfg, "mops", 0.0 + 3 * 2.0, 3)
        self.assertLess(ondemand.s_per_company(self.cfg, "mops"), 18.0)
        with self.ready():
            pl = self.plan(time_s=5)
        self.assertEqual(pl.est_seconds, 5)
        with self.assertRaises(ValueError):
            self.plan("scr-nope")


# ---------------------------------------------------------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeProc:
    """A child that writes its events file on the fake clock; obeys SIGTERM (exit 130) unless stuck."""

    def __init__(self, key, fetch_dir, clock, events, exit_at=None, stuck=False, status="ok", on_poll=None):
        self.key, self.dir, self.clock, self.events = key, Path(fetch_dir), clock, list(events)
        self.exit_at, self.stuck, self.status, self.on_poll = exit_at, stuck, status, on_poll
        self.returncode = None
        self.term_at = self.killed_at = None
        self.t0 = clock()

    def _flush(self):
        due = [e for e in self.events if self.t0 + e[0] <= self.clock()]
        self.events = [e for e in self.events if e not in due]
        with open(self.dir / f"{self.key}.events.jsonl", "a", encoding="utf-8") as fh:
            for _t, sids, st in due:
                fh.write(json.dumps({"t": 0, "security_ids": sids, "status": st, "note": None}) + "\n")
        if due and self.on_poll:
            self.on_poll(due)

    def summary(self, status, stopped=None):
        (self.dir / f"{self.key}.summary.json").write_text(json.dumps(
            {"status": status, "stopped_reason": stopped, "requests": 4, "seconds": self.clock() - self.t0}))

    def poll(self):
        if self.returncode is None:
            self._flush()
            if self.term_at is not None and not self.stuck:
                self.summary("interrupted", "deadline")
                self.returncode = 130
            elif self.exit_at is not None and self.clock() >= self.t0 + self.exit_at:
                self.summary(self.status)
                self.returncode = 0
        return self.returncode

    def terminate(self):
        self.term_at = self.clock()

    def kill(self):
        self.killed_at = self.clock()
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class LaunchCase(Home):
    def setUp(self):
        super().setUp()
        make_run(self.cfg, "scr-1")
        with self.ready():
            self.pl = self.plan(sources=["sec", "bse"])
        self.clock = FakeClock()
        self.fdir = self.home / "out" / "fetch"
        self.lines: list[str] = []

    def launch(self, procs, **kw):
        def popen(cmd, **kwargs):
            key = cmd[cmd.index("--source") + 1]
            return procs[key](key)
        kw.setdefault("time_s", 60.0)
        return ondemand.launch(self.cfg, self.pl, fetch_dir=self.fdir, lang="zh", out=self.lines.append,
                               popen=popen, clock=self.clock, sleep=self.clock.sleep, grace_s=5.0, **kw)


class TestLaunchWithFakeChildren(LaunchCase):
    def test_heartbeat_deadline_term_then_kill_and_outcomes(self):
        procs = {"sec": lambda k: FakeProc(k, self.fdir, self.clock, [(2, ["NYSE:ACME"], "no_annual_filing")],
                                           exit_at=3),
                 "bse": lambda k: FakeProc(k, self.fdir, self.clock, [], stuck=True)}
        made = {}

        def wrap(k):
            made[k] = procs[k](k)
            return made[k]
        res = self.launch({k: wrap for k in procs}, time_s=60.0)
        self.assertEqual(made["bse"].term_at, 1060.0)
        self.assertEqual(made["bse"].killed_at, 1065.0)
        self.assertIsNone(made["sec"].term_at)
        hb = [line for line in self.lines if line.startswith("补抓中")]
        self.assertEqual(hb[0], "补抓中（15 秒/最多 60 秒）：已处理 1/2 · 美国 SEC 1/1 · 印度 BSE 0/1")
        self.assertEqual(len(hb), 4)
        self.assertTrue(any("印度 BSE 网络很慢" in line for line in self.lines))
        oc = res["outcomes"]
        self.assertEqual(oc[CK["NYSE:ACME"]]["code"], "no_filing")
        self.assertEqual(oc[CK["NSE:ROBIN"]]["code"], "deferred_time")
        self.assertEqual(res["summary"]["abandoned"], ["bse"])
        self.assertEqual(res["summary"]["status"], "partial")
        self.assertEqual(res["fetched"], [])
        self.assertNotIn("unresolved", {o["code"] for o in oc.values()})

    def test_fetched_outcome_and_no_session_while_children_run(self):
        alive = []

        real = store.session

        def write_doc(_due):          # what the sec child writes (its own process: not the parent's session)
            path = self.home / "docs" / "acme.txt"
            path.write_text(DOCD_TEXT.replace("Docd", "Acme"), encoding="utf-8")
            with real(self.cfg) as con:
                con.execute("INSERT INTO documents (doc_id, security_id, company_key, source_id, form, text_path, "
                            "filing_date) VALUES ('sec_filing_text:1:b:item1', 'NYSE:ACME', ?, 'sec_filing_text', "
                            "'10-K', ?, ?)", [CK["NYSE:ACME"], str(path), dt.date(2026, 3, 1)])

        def mk(k):
            ev = [(1, ["NYSE:ACME"], "ok")] if k == "sec" else [(1, ["NSE:ROBIN"], "extract_failed")]
            p = FakeProc(k, self.fdir, self.clock, ev, exit_at=2, on_poll=write_doc if k == "sec" else None)
            alive.append(p)
            return p
        @contextlib.contextmanager
        def spy(*a, **kw):
            self.assertTrue(all(p.returncode is not None for p in alive), "a session while a child is alive")
            with real(*a, **kw) as con:
                yield con
        with mock.patch.object(store, "session", spy):
            res = self.launch({"sec": mk, "bse": mk})
        self.assertEqual(res["fetched"], [CK["NYSE:ACME"]])
        self.assertEqual(res["outcomes"][CK["NSE:ROBIN"]]["code"], "extract_failed")
        self.assertEqual(res["summary"]["fetched"], {"sec": 1})
        self.assertEqual(res["summary"]["status"], "ok")
        self.assertIn("1 家本次新抓", res["summary"]["summary_zh"])

    def test_first_interrupt_stops_children_and_continues(self):
        made = []

        def mk(k):
            p = FakeProc(k, self.fdir, self.clock, [(1, [("NYSE:ACME" if k == "sec" else "NSE:ROBIN")], "ok")])
            made.append(p)
            return p
        calls = {"n": 0}

        def sleep(s):
            calls["n"] += 1
            self.clock.sleep(s)
            if calls["n"] == 3:
                raise KeyboardInterrupt
        def popen(cmd, **kwargs):
            return mk(cmd[cmd.index("--source") + 1])
        res = ondemand.launch(self.cfg, self.pl, time_s=60, fetch_dir=self.fdir, lang="en", out=self.lines.append,
                              popen=popen, clock=self.clock, sleep=sleep, grace_s=5.0)
        self.assertTrue(res["interrupted"])
        self.assertTrue(all(p.term_at is not None for p in made))
        self.assertIn("Fetch stopped; updating the results with the 2 reports already fetched", self.lines[-1])
        self.assertEqual(res["summary"]["status"], "interrupted")

    def test_child_status_mapping(self):
        pl, info = self.pl, {"sec": {}, "bse": {}}
        acme, robin = CK["NYSE:ACME"], CK["NSE:ROBIN"]
        for st, want in (("source_busy", "source_busy"), ("blocked", "blocked"), ("store_locked", "store_busy"),
                         ("ok", "not_mapped"), ("error", "unresolved")):
            oc = ondemand.outcomes(pl, {}, {"sec": {"status": st}}, info, {}, {})
            self.assertEqual(oc[acme]["code"], want, st)
        oc = ondemand.outcomes(pl, {}, {"sec": {"status": "ok"}}, info, {}, {"NYSE:ACME": {"sec_cik"}})
        self.assertEqual(oc[acme]["code"], "unresolved")
        oc = ondemand.outcomes(pl, {"bse": [{"security_ids": ["NSE:ROBIN"], "status": "skipped_current"}]},
                               {"bse": {"status": "ok"}}, info, {}, {})
        self.assertEqual(oc[robin]["code"], "known_failure")
        oc = ondemand.outcomes(pl, {"sec": [{"security_ids": ["NYSE:ACME"], "status": "ok"}]},
                               {"sec": {"status": "ok"}}, info, None, None)
        self.assertEqual(oc[acme]["code"], "store_busy")


# ---------------------------------------------------------------------------------------------------------------
class ChildCase(Home):
    """Real `python -m jevscreen.ondemand child` processes running tests/ondemand_fake_adapter.py."""

    def setUp(self):
        super().setUp()
        mods = {k: "ondemand_fake_adapter" for k in ondemand.SOURCES}
        os.environ.update({"JEVSCREEN_TESTING": "1", "JEVSCREEN_ONDEMAND_MODULES": json.dumps(mods),
                           "PYTHONPATH": str(TESTS)})
        self.fdir = self.home / "out" / "fetch"
        self.fdir.mkdir(parents=True)

    def fake(self, **by_key):
        (self.home / "fake_adapter.json").write_text(json.dumps(by_key), encoding="utf-8")

    def write_plan(self, key, codes, deadline_s=60.0):
        p = self.fdir / "plan.json"
        p.write_text(json.dumps({"deadline_epoch": time.time() + deadline_s, "grace_s": 5,
                                 "sources": {key: {"codes": codes, "refresh": False}}}), encoding="utf-8")
        return p

    def child(self, key, plan_path):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(TESTS.parent / "src") + os.pathsep + str(TESTS)
        return subprocess.Popen(ondemand.child_command(self.cfg, key, plan_path), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TestChildProcess(ChildCase):
    def test_sigterm_mid_queue_flushes_summary_and_events(self):
        self.fake(sec={"ok": ["NYSE:ACME", "NASDAQ:DOCD"], "delay_s": 0.4, "hang": True})
        proc = self.child("sec", self.write_plan("sec", ["NYSE:ACME", "NASDAQ:DOCD"]))
        ev = self.fdir / "sec.events.jsonl"
        deadline = time.monotonic() + 30
        while ondemand._count_lines(ev)[0] < 2 and time.monotonic() < deadline:
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(30), 130)
        s = ondemand.read_summary(self.fdir / "sec.summary.json")
        self.assertEqual((s["status"], s["stopped_reason"]), ("interrupted", "interrupted"))
        self.assertEqual([e["status"] for e in ondemand.read_events(ev)], ["ok", "ok"])

    def test_budget_lock_held_elsewhere_means_source_busy_and_no_request(self):
        self.fake(sec={"ok": ["NYSE:ACME"]})
        with guard.budget_lock(self.cfg, "sec.gov"):
            proc = self.child("sec", self.write_plan("sec", ["NYSE:ACME"]))
            self.assertEqual(proc.wait(30), 4)
        s = ondemand.read_summary(self.fdir / "sec.summary.json")
        self.assertEqual((s["status"], s["requests"]), ("source_busy", 0))
        self.assertFalse((self.fdir / "sec.events.jsonl").exists())

    def test_child_stops_itself_at_the_deadline(self):
        self.fake(bse={"hang": True})
        proc = self.child("bse", self.write_plan("bse", ["NSE:ROBIN"], deadline_s=1.5))
        self.assertEqual(proc.wait(30), 130)
        self.assertEqual(ondemand.read_summary(self.fdir / "bse.summary.json")["stopped_reason"], "deadline")


class TestLaunchRealChildren(ChildCase):
    def test_end_to_end_fetch_and_a_stuck_child(self):
        make_run(self.cfg, "scr-1")
        self.fake(sec={"ok": ["NYSE:ACME"]}, cninfo={"status": {"SSE:600999": "no_annual_report"}},
                  bse={"unmapped": ["NSE:ROBIN"], "hang": True, "ignore_term": True},
                  dart={"unmapped": ["KRX:099999"]})
        with self.ready():
            pl = self.plan(sources=["sec", "cninfo", "bse", "dart"])
        lines: list[str] = []
        t = time.monotonic()
        res = ondemand.launch(self.cfg, pl, time_s=4.0, fetch_dir=self.fdir, lang="en", out=lines.append,
                              grace_s=1.5, heartbeat_s=2.0, poll_s=0.2)
        wall = time.monotonic() - t
        self.assertLessEqual(wall, 4.0 + 1.5 + 2 + 3)   # + interpreter start-up of four children on a slow machine
        oc = {e["security_id"]: res["outcomes"][ck]["code"] for ck, e in pl.entries.items()}
        self.assertEqual(oc, {"NYSE:ACME": "fetched", "SSE:600999": "no_filing", "NSE:ROBIN": "deferred_time",
                              "KRX:099999": "not_mapped", "TWSE:2999": "disabled", "IDX:ROBI": "no_adapter",
                              "TSE:7999": "edinet_pack"})
        self.assertEqual(res["summary"]["abandoned"], ["bse"])
        self.assertTrue(any(line.startswith("Fetching (") for line in lines))
        with store.session(self.cfg, read_only=True) as con:
            runs = con.execute("SELECT count(*) FROM documents WHERE security_id = 'NYSE:ACME'").fetchone()[0]
        self.assertEqual(runs, 1)


# ---------------------------------------------------------------------------------------------------------------
class ScreenCase(Home):
    def setUp(self):
        super().setUp()
        self.log: list = []
        self.kw = FakeKeywords()

    def run_screen(self, **kw):
        kw.setdefault("jev_factory", make_factory(self.log))
        kw.setdefault("out_dir", self.home / "out")
        kw.setdefault("keywords_fn", self.kw)
        kw.setdefault("reads", 1)
        return screen.screen(self.cfg, "humanoid robots", **kw)

    def add_acme_doc(self):
        path = self.home / "docs" / "acme.txt"
        path.write_text(DOCD_TEXT.replace("Docd", "Acme").replace("is at an early pilot stage with two customers and "
                                                                  "is not yet a material product line",
                                                                  "is our largest product line"), encoding="utf-8")
        with store.session(self.cfg) as con:
            con.execute("INSERT INTO documents (doc_id, security_id, company_key, source_id, form, text_path, "
                        "filing_date, url, fetched_at) VALUES ('sec_filing_text:1:b:item1', 'NYSE:ACME', ?, "
                        "'sec_filing_text', '10-K', ?, ?, 'https://example.invalid/acme', ?)",
                        [CK["NYSE:ACME"], str(path), dt.date(2026, 3, 1), store.now_utc()])

    def info(self, fetched=(CK["NYSE:ACME"],)):
        oc = {ck: {"source": "sec", "code": "fetched", "note": None} for ck in fetched}
        oc[CK["IDX:ROBI"]] = {"source": None, "code": "no_adapter", "note": None}
        return {"summary": {"mode": "auto", "status": "ok", "planned": {"sec": 1}, "summary_zh": "z",
                            "summary_en": "e"}, "fetched": list(fetched), "outcomes": oc}


class TestScreenUpdatePass(ScreenCase):
    def test_library_never_fetches(self):
        with mock.patch.object(ondemand, "plan", side_effect=AssertionError("plan")), \
                mock.patch.object(ondemand, "launch", side_effect=AssertionError("launch")):
            res = self.run_screen()
        self.assertEqual(res["status"], "ok")
        self.assertNotIn("fetch", res["layers"])
        self.assertNotIn("l2_doc_unavailable", res["gaps"])
        self.assertNotIn("l2_doc_fetched", res["funnel"])
        with open(self.home / "out" / "results.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["doc_fetch"], "")
        self.assertNotIn("doc_fetch", (self.home / "out" / "l2_inputs.jsonl").read_text(encoding="utf-8"))

    def test_supersedes_replaces_outputs_and_keeps_v1(self):
        base = self.run_screen()
        self.add_acme_doc()
        with self.assertRaises(ValueError):
            self.run_screen(from_run=base["run_id"], supersedes="scr-other")
        upd = self.run_screen(from_run=base["run_id"], supersedes=base["run_id"], budget_usd=0.05,
                              fetch_info=self.info())
        out = self.home / "out"
        self.assertTrue(upd["outputs_written"])
        self.assertEqual(json.loads((out / "results.v1.json").read_text(encoding="utf-8"))["run_id"], base["run_id"])
        self.assertEqual(json.loads((out / "results.json").read_text(encoding="utf-8"))["run_id"], upd["run_id"])
        self.assertEqual(upd["supersedes"], base["run_id"])
        self.assertEqual(self.log[-1].layer, "l2")                  # L1 came from the base run ($0)
        lines = {json.loads(x)["security_id"]: json.loads(x)
                 for x in (out / "l2_inputs.jsonl").read_text(encoding="utf-8").splitlines()}
        self.assertEqual((lines["NYSE:ACME"]["evidence"], lines["NYSE:ACME"]["doc_fetch"]),
                         ("annual_report", "fetched_now"))
        self.assertEqual(lines["NASDAQ:DOCD"]["doc_fetch"], "stored")
        self.assertEqual(lines["IDX:ROBI"]["doc_fetch"], "no_adapter")
        self.assertEqual(lines["TSE:7999"]["doc_fetch"], "unresolved")
        gap = {g["security_id"]: g["reason"] for g in upd["gaps"]["l2_doc_unavailable"]}
        self.assertEqual(gap["IDX:ROBI"], "no_adapter")
        self.assertNotIn("NYSE:ACME", gap)
        self.assertEqual(upd["funnel"]["l2_doc_fetched"], 1)
        self.assertEqual(upd["layers"]["fetch"]["planned"], {"sec": 1})
        acme = next(r for r in upd["rows"] if r["security_id"] == "NYSE:ACME")
        self.assertEqual((acme["l2_evidence"], acme["doc_fetch"]), ("annual_report", "fetched_now"))
        with open(out / "results.csv", encoding="utf-8") as fh:
            self.assertIn("fetched_now", {r["doc_fetch"] for r in csv.DictReader(fh)})
        md = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Annual-report evidence and gaps", md)
        self.assertIn("annual_report · new report", md)
        with store.session(self.cfg, read_only=True) as con:
            od = con.execute("SELECT output_dir FROM screen_runs WHERE run_id = ?", [upd["run_id"]]).fetchone()[0]
        self.assertEqual(od, str(out))
        # a second update keeps the first version too
        upd2 = self.run_screen(from_run=upd["run_id"], supersedes=upd["run_id"], budget_usd=0.05,
                               fetch_info=self.info())
        self.assertTrue(upd2["outputs_written"])
        self.assertTrue((out / "results.v2.json").exists())

    def test_why_of_a_replaced_run_explains_the_update_and_says_so(self):
        """The update replaced the phase-1 run's files (results.json, the ledger): `why --run <phase-1 id>` must not
        label the update's explanation with the phase-1 id; it explains the update and names both runs."""
        from jevscreen import why
        base = self.run_screen()
        self.add_acme_doc()
        upd = self.run_screen(from_run=base["run_id"], supersedes=base["run_id"], budget_usd=0.05,
                              fetch_info=self.info())
        self.assertTrue(upd["outputs_written"])
        out = why.run(self.cfg, ["Acme Robotics"], run_ref=base["run_id"])
        self.assertEqual(out["run"]["run_id"], upd["run_id"])
        self.assertEqual(out["run"]["requested_run_id"], base["run_id"])
        self.assertEqual(out["results"][0]["run"]["run_id"], upd["run_id"])
        self.assertEqual(out["run"]["started_at"], upd["started_at"])
        for lang in ("zh", "en"):
            self.assertIn(base["run_id"], out[f"note_{lang}"])
            self.assertIn(upd["run_id"], out[f"note_{lang}"])
        same = why.run(self.cfg, ["Acme Robotics"], run_ref=upd["run_id"])
        self.assertEqual(same["run"]["run_id"], upd["run_id"])
        self.assertNotIn("requested_run_id", same["run"])
        self.assertNotIn("note_en", same)

    def test_worse_update_leaves_phase1_files(self):
        base = self.run_screen()
        before = {n: (self.home / "out" / n).read_bytes() for n in ("results.json", "report.md", "results.csv")}
        self.add_acme_doc()

        def dead(cfg, **kw):
            from test_screen import JevUnavailable
            raise JevUnavailable("401")
        upd = self.run_screen(from_run=base["run_id"], supersedes=base["run_id"], fetch_info=self.info(),
                              jev_factory=dead)
        self.assertEqual((upd["status"], upd["outputs_written"]), ("jev_unavailable", False))
        self.assertEqual({n: (self.home / "out" / n).read_bytes() for n in before}, before)
        self.assertFalse((self.home / "out" / "results.v1.json").exists())
        with store.session(self.cfg, read_only=True) as con:
            od = con.execute("SELECT output_dir FROM screen_runs WHERE run_id = ?", [upd["run_id"]]).fetchone()[0]
        self.assertIsNone(od)
        self.assertTrue(screen.supersede_allowed("partial", "partial"))
        self.assertFalse(screen.supersede_allowed("partial", "ok"))
        self.assertTrue(screen.supersede_allowed("ok", "partial"))


# ---------------------------------------------------------------------------------------------------------------
class CliCase(ScreenCase):
    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(screen, "_default_factory", make_factory(self.log)), \
                mock.patch.object(screen, "_default_keywords", self.kw), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def fake_launch(self, *, fetch=True, blocked=False, check=None, interrupt=False):
        def launch(cfg, pl, *, time_s, fetch_dir, retry_failed=False, lang="en", out=print, mode="auto", **kw):
            if check:
                check()
            if interrupt:
                raise KeyboardInterrupt
            if fetch:
                self.add_acme_doc()
            oc = {ck: {"source": e.get("source"), "code": e.get("reason"), "note": None}
                  for ck, e in pl.entries.items() if e.get("reason")}
            for k, ents in pl.by_source.items():
                for e in ents:
                    ok = fetch and e["security_id"] == "NYSE:ACME"
                    oc[e["company_key"]] = {"source": k, "code": "fetched" if ok else
                                            ("blocked" if blocked else "no_filing"), "note": None}
            srcs = {k: {"status": "blocked" if blocked else "ok"} for k in pl.by_source}
            summ = ondemand.fetch_summary(pl, oc, {}, {}, {}, seconds=1.0, time_s=time_s, mode=mode)
            summ["sources"] = srcs
            return {"summary": summ, "outcomes": oc, "fetched": [ck for ck, o in oc.items()
                                                                 if o["code"] == "fetched"],
                    "docs_by_ck": {}, "interrupted": False, "store_ok": True}
        return mock.patch.object(ondemand, "launch", launch)


class TestScreenCli(CliCase):
    def test_auto_fetch_updates_the_report_and_prints_cards_once(self):
        def check():
            self.assertTrue(list((self.home / "screens").glob("*/report.md")), "phase-1 report before the fetch")
        with self.ready(sec_email=True), self.fake_launch(check=check):
            code, out, err = self.main(["screen", "humanoid robots", "--reads", "1", "--sieve", "none"])
        self.assertEqual(code, 0, err)
        self.assertIn("Fetching annual reports: 7 companies that passed the first round have no annual-report text "
                      "stored; downloading 5 now", out)
        self.assertIn("Report updated: 1 more company now read from annual reports", out)
        self.assertIn("Update (newly fetched annual reports; L1 reused at $0)", out)
        self.assertEqual(out.count("这次没有需要你判断的卡"), 1)      # the cards are printed once, after the update
        self.assertLess(out.index("Report updated"), out.index("这次没有需要你判断的卡"))
        d = next((self.home / "screens").glob("*"))
        res = json.loads((d / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(res["layers"]["fetch"]["update"]["status"], "ok")
        self.assertTrue((d / "results.v1.json").exists())
        self.assertIn("## Annual-report evidence and gaps", (d / "report.md").read_text(encoding="utf-8"))

    def test_zh_run_prints_the_diff_with_its_cause(self):
        kw = FakeKeywords({"idea_en": "Humanoid robots", "keywords": {"en": ["humanoid robot"]}, "model": "fake",
                           "cached": False})
        self.kw = kw
        with self.ready(), self.fake_launch():
            code, out, err = self.main(["screen", "人形机器人", "--reads", "1", "--sieve", "none"])
        self.assertEqual(code, 0, err)
        self.assertIn("补抓年报：通过第一轮的", out)
        self.assertIn("更新（新抓年报，L1 沿用 $0）", out)
        self.assertNotIn("Fetching annual reports", out)

    def test_off_from_run_and_skip_lines(self):
        with mock.patch.object(ondemand, "plan", side_effect=AssertionError("no fetch")):
            code, out, _ = self.main(["screen", "humanoid robots", "--fetch-docs", "off", "--reads", "1"])
            self.assertEqual(code, 0)
            with store.session(self.cfg, read_only=True) as con:
                rid = con.execute("SELECT run_id FROM screen_runs ORDER BY started_at DESC LIMIT 1").fetchone()[0]
            code, out, _ = self.main(["screen", "humanoid robots", "--from-run", rid, "--cards", "0"])
            self.assertEqual(code, 0)
            self.assertNotIn("Fetching annual reports", out)
            code, out, _ = self.main(["screen", "humanoid robots", "--budget", "0.001", "--reads", "1"])
            self.assertEqual(code, 5)
        self.assertIn("Fetching annual reports: skipped (the screen did not finish", out)

    def test_dry_run_suffix_and_readiness_lines(self):
        with self.ready(**NONE_READY):
            code, out, _ = self.main(["screen", "humanoid robots", "--dry-run"])
        self.assertEqual(code, 0)
        line = next(x for x in out.splitlines() if x.startswith("total estimate:"))
        self.assertTrue(line.endswith("(+ up to 2 min, free, fetching annual reports for first-round companies with "
                                      "none stored)"))
        self.assertIn("No SEC contact email: US companies will read the profile only", out)
        self.assertIn("No PDF reader installed", out)
        self.assertNotRegex(line, r"\d+ compan")

    def test_second_interrupt_keeps_phase1_report_and_exit_130(self):
        with self.ready(), self.fake_launch(interrupt=True):
            code, out, err = self.main(["screen", "humanoid robots", "--reads", "1"])
        self.assertEqual(code, 130)
        d = next((self.home / "screens").glob("*"))
        self.assertTrue((d / "report.md").exists())
        self.assertFalse((d / "results.v1.json").exists())

    def test_blocked_source_screen_exit_0_fetch_docs_exit_2(self):
        with self.ready(), self.fake_launch(fetch=False, blocked=True):
            code, out, _ = self.main(["screen", "humanoid robots", "--reads", "1", "--cards", "0"])
            self.assertEqual(code, 0)
            code, out, _ = self.main(["fetch-docs", "latest", "--json"])
        self.assertEqual(code, 2)
        data = json.loads(out)
        self.assertEqual(data["fetch"]["sources"]["sec"]["status"], "blocked")
        self.assertIsNone(data["update"])


class TestFetchDocsCli(CliCase):
    def base(self):
        with mock.patch.object(ondemand, "plan", side_effect=AssertionError("no fetch")):
            code, _, err = self.main(["screen", "humanoid robots", "--reads", "1", "--fetch-docs", "off",
                                      "--cards", "0"])
        self.assertEqual(code, 0, err)
        with store.session(self.cfg, read_only=True) as con:
            return con.execute("SELECT run_id, output_dir FROM screen_runs ORDER BY started_at DESC LIMIT 1"
                               ).fetchone()

    def test_latest_json_schema_and_update(self):
        rid, od = self.base()
        with self.ready(), self.fake_launch():
            code, out, err = self.main(["fetch-docs", "latest", "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        for k in ("run_id", "status", "fetch", "update", "summary_zh", "summary_en", "questions", "next_command",
                  "ask_human"):
            self.assertIn(k, data)
        self.assertEqual(data["run_id"], rid)
        self.assertEqual(data["update"]["status"], "ok")
        self.assertEqual(data["fetch"]["mode"], "fetch-docs")
        res = json.loads((Path(od) / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(res["idea"], "humanoid robots")
        self.assertEqual(res["supersedes"], rid)

    def test_cards_follow_the_updated_run(self):
        with mock.patch.object(ondemand, "plan", side_effect=AssertionError("no fetch")):
            code, _, err = self.main(["screen", "humanoid robots", "--reads", "1", "--fetch-docs", "off"])
        self.assertEqual(code, 0, err)
        with self.ready(), self.fake_launch():
            code, out, err = self.main(["fetch-docs", "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        deck = json.loads(Path(data["cards_path"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(deck["run_id"], data["update"]["run_id"])
        code, out, err = self.main(["answer", "--no-apply", "--json"])
        self.assertNotIn("属于运行", err)

    def test_page_and_quickstart_job_follow_the_updated_run(self):
        """fetch-docs replaces the run: the run page and the stable page carry the new run and deck_id (the answer
        line the page builds is accepted), and a finished quickstart job of the idea points at the new run."""
        from jevscreen import page, quickstart as qs
        with mock.patch.object(ondemand, "plan", side_effect=AssertionError("no fetch")):
            code, _, err = self.main(["screen", "humanoid robots", "--reads", "1", "--fetch-docs", "off"])
        self.assertEqual(code, 0, err)
        with store.session(self.cfg, read_only=True) as con:
            rid, od = con.execute("SELECT run_id, output_dir FROM screen_runs").fetchone()
        od = Path(od)
        stable = page.stable_path(self.cfg, "humanoid robots")
        old_deck = json.loads((od / "cards.json").read_text(encoding="utf-8"))["deck_id"]
        self.assertIn(old_deck, (od / "page.html").read_text(encoding="utf-8"))
        job = qs.new_job("humanoid robots", lang="en", min_mcap=qs.DEFAULT_MIN_MCAP, countries=None)
        job.update(state="done", idea_en="humanoid robots", result={
            "run_id": rid, "status": "ok", "output_dir": str(od), "page": str(stable), "run_page": str(od / "page.html"),
            "page_opened": True, "deck_id": old_deck, "cost_usd": 0.01, "seconds": 10.0, "summary": {}, "top": [],
            "next_steps": [], "gaps": [], "sieve_version": None, "idea_en": "humanoid robots",
            "min_mcap_usd": qs.DEFAULT_MIN_MCAP, "countries": None,
            "fetch": {"status": "skipped", "next_command": "jevscreen fetch-docs latest"}})
        qs.save_job(self.cfg, job)
        with self.ready(), self.fake_launch():
            code, out, err = self.main(["fetch-docs", "latest", "--json"])
        self.assertEqual(code, 0, err)
        new_rid = json.loads(out)["update"]["run_id"]
        new_deck = json.loads((od / "cards.json").read_text(encoding="utf-8"))["deck_id"]
        self.assertNotEqual(new_deck, old_deck)
        for p in (od / "page.html", stable):
            html = p.read_text(encoding="utf-8")
            self.assertTrue(new_deck in html and new_rid in html and old_deck not in html, p)
        code, _, err = self.main(["answer", "1?", "--deck", new_deck, "--no-apply"])
        self.assertNotIn("已换成", err)
        r = qs.load_job(self.cfg, job["idea_key"])["result"]
        self.assertEqual((r["run_id"], r["deck_id"], r["output_dir"]), (new_rid, new_deck, str(od)))
        self.assertEqual(r["cost_usd"], round(0.01 + float(json.loads(out)["update"]["cost_usd"] or 0), 6))
        self.assertNotEqual((r["fetch"] or {}).get("next_command"), "jevscreen fetch-docs latest")
        self.assertTrue(r["page_opened"])
        view = qs.status(self.cfg, job["idea_key"])
        self.assertEqual((view["run_id"], view["deck_id"]), (new_rid, new_deck))

    def test_dry_run_and_unknown_run(self):
        self.base()
        with self.ready(sec_email=False), mock.patch.object(ondemand, "launch", side_effect=AssertionError("launch")):
            code, out, _ = self.main(["fetch-docs", "--dry-run", "--json"])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data["skipped"]["no_key_sec"], 1)
            code, _, err = self.main(["fetch-docs", "scr-nope"])
        self.assertEqual(code, 1)

    def test_sieve_changed_skips_the_update(self):
        sv_path = self.home / "my_sieve.json"
        calib.save_sieve(sv_path, calib.new_sieve("humanoid robots"))
        with mock.patch.object(ondemand, "plan", side_effect=AssertionError("no fetch")):
            code, _, err = self.main(["screen", "humanoid robots", "--reads", "1", "--fetch-docs", "off",
                                      "--cards", "0", "--sieve", str(sv_path)])
        self.assertEqual(code, 0, err)
        sv_path.write_text(sv_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")   # edited since
        with self.ready(), self.fake_launch():
            code, out, _ = self.main(["fetch-docs"])
        self.assertEqual(code, 0)
        self.assertIn("The sieve this run used has changed since", out)

    def test_exit_codes_for_update_failures(self):
        self.base()

        def busy(*a, **kw):
            from test_screen import JevUnavailable

            class JevBusy(JevUnavailable):
                pass
            raise JevBusy("busy")
        def dead(*a, **kw):
            from test_screen import JevUnavailable
            raise JevUnavailable("401")
        for exc_factory, argv, want in ((busy, [], 4), (dead, [], 6), (make_factory(self.log), ["--budget", "0.001"], 5),
                                        (make_factory(self.log), ["--no-update"], 0)):
            with store.session(self.cfg) as con:       # each case fetches ACME afresh
                con.execute("DELETE FROM documents WHERE doc_id = 'sec_filing_text:1:b:item1'")
            with self.ready(), self.fake_launch(), mock.patch.object(screen, "_default_factory", exc_factory):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                        mock.patch.object(screen, "_default_keywords", self.kw):
                    code = cli.main(["fetch-docs", "--json", *argv])
            self.assertEqual(code, want, (argv, out.getvalue()[-400:]))
            data = json.loads(out.getvalue())
            if want in (4, 6):     # the update run happened, found Jev busy / unavailable, wrote nothing
                self.assertEqual((data["update"]["status"], data["update"]["written"], data["update_skipped"]),
                                 ({4: "jev_busy", 6: "jev_unavailable"}[want], False, "not_written"))
            if argv == ["--no-update"]:
                self.assertEqual(data["update_skipped"], "no_update")


# ---------------------------------------------------------------------------------------------------------------
class TestReportSection(unittest.TestCase):
    def result(self, idea="humanoid robots", fetch=True):
        rows = [{"rank": 1, "security_id": "NYSE:ACME", "company_key": "a", "name": "Acme", "region": "US",
                 "market_cap_usd": 8e9, "revenue_ttm_usd": None, "revenue_cagr_3y_local": None, "cagr_currency": None,
                 "cagr_years": None, "l1_label": "core", "l1_p_core": 0.9, "l2_label": "explicit",
                 "l2_status": "explicit", "l2_evidence": "annual_report", "score": 5.0, "evidence_excerpt": "x",
                 "evidence_url": None, "filing_source": "sec_filing_text", "filing_form": "10-K", "filing_date": None,
                 "doc_lang": "en", "l1_input_tier": "gray-private", "l2_input_tier": "official-private",
                 "doc_fetch": "fetched_now"}]
        unv = [dict(rows[0], rank=None, security_id=f"IDX:R{i}", company_key=f"r{i}", name=f"Robi {i}",
                    l2_label="insufficient", l2_status="insufficient", l2_evidence="profile", l1_p_core=0.5 + i / 100,
                    doc_fetch="no_adapter") for i in range(22)]
        res = {"run_id": "scr-x", "idea": idea, "status": "ok", "dry_run": False, "params": {}, "funnel":
               {"universe": 1, "l2_doc_fetched": 1}, "layers": {"l1": {}, "l2": {"sec_inputs": 3,
                                                                                  "profile_inputs": 22}},
               "cost_usd": 0.0, "budget_usd": 1.0, "timing": {}, "started_at": "", "finished_at": "", "rows": rows,
               "unverified": unv, "gaps": {}, "output_dir": "/tmp/x"}
        if fetch:
            res["layers"]["fetch"] = {"summary_zh": "3 家读了官方年报（其中 1 家本次新抓）", "summary_en": "3 read official "
                                      "annual reports (1 fetched now)"}
            res["gaps"]["l2_doc_unavailable"] = [{"security_id": r["security_id"], "reason": "no_adapter",
                                                 "country": "Indonesia"} for r in unv]
        return res

    def test_english_section_marks_and_maybe_missing(self):
        md = report.render_markdown(self.result())
        self.assertIn("## Annual-report evidence and gaps", md)
        self.assertIn("- Fact: 3 companies were read from official annual reports (1 fetched now); 22 read the company "
                      "profile only", md)
        self.assertIn("- Indonesia 22: no official annual-report source for these markets yet", md)
        self.assertIn("### May be missing from your list (profile only, not a 'no'): 22", md)
        self.assertIn("... and 2 more", md)
        self.assertIn("annual_report · new report", md)
        self.assertIn("| profile |", md)
        section = md.split("## Annual-report evidence and gaps")[1].split("## Gaps")[0]
        self.assertLess(section.index("IDX:R21"), section.index("IDX:R20"))       # highest L1 p_core first
        self.assertNotIn("IDX:R1 ", section)                                       # beyond the top 20
        self.assertNotIn("年报原文与缺口", md)

    def test_chinese_section_and_phase1_without_fetch(self):
        md = report.render_markdown(self.result(idea="人形机器人"))
        self.assertIn("## 年报原文与缺口", md)
        self.assertIn("- 印尼 22：这些市场还没有官方年报来源", md)
        self.assertIn("可能被漏掉的公司（只读了简介，不是'不符合'）", md)
        self.assertIn("| 简介 |", md)
        self.assertIn("annual_report · 新年报", md)
        md = report.render_markdown(self.result(fetch=False))
        self.assertIn("no annual-report fetch ran for this run", md)


class TestCalibAndDoctor(Home):
    def test_recheck_why_after_a_profile_answer(self):
        self.assertIn("recheck_profile", calib.WHY_ZH)
        self.assertEqual(calib.WHY_ZH["recheck_profile"], "你上次答时只有公司简介，现在有了年报原文")
        self.assertTrue(calib.filing_key(*calib._filing_of({"evidence": "profile"})).startswith(calib.PROFILE_FILING))

    def test_diff_cause(self):
        b = {"rows": [], "unverified": [{"company_key": "a", "security_id": "X:A", "name": "A", "l2_evidence":
                                         "profile", "l2_label": "insufficient"}]}
        a = {"rows": [{"company_key": "a", "security_id": "X:A", "name": "A", "rank": 1, "l2_evidence":
                       "annual_report", "l2_label": "explicit"}], "unverified": []}
        self.assertIn("新抓年报", calib.render_diff_zh(b, a, title="t"))

    def test_doctor_checks(self):
        with mock.patch.object(ondemand, "pdf_backends", return_value=[]):
            c = {x["id"]: x for x in ondemand.doctor_checks(self.cfg)}
        self.assertEqual((c["on_demand_docs"]["status"], c["on_demand_docs"]["fix_command"]),
                         ("warn", "python3 -m pip install pypdf"))
        self.assertEqual(c["mops_annual"]["status"], "skip")
        self.assertIn("human_question", c["mops_annual"])
        with mock.patch.object(ondemand, "pdf_backends", return_value=["pymupdf"]):
            c = {x["id"]: x for x in ondemand.doctor_checks(self.cfg)}
            self.assertEqual((c["on_demand_docs"]["status"], c["on_demand_docs"]["ask_human"],
                              c["on_demand_docs"]["fix_command"]), ("warn", True, None))
            self.assertEqual(c["on_demand_docs"]["record_answer_commands"],
                             ["jevscreen keys set sec-email", "jevscreen consent set sec-email-ask no"])
            with mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": "Test Person test@example.org"}):
                c = {x["id"]: x for x in ondemand.doctor_checks(self.cfg)}
            self.assertEqual(c["on_demand_docs"]["status"], "ok")
            full = doctor.run(self.cfg)
        ids = [x["id"] for x in full["checks"]]
        self.assertIn("on_demand_docs", ids)
        self.assertIn("mops_annual", ids)
        from jevscreen import consent
        consent.record(self.cfg, "mops-annual", "yes")
        self.assertEqual(ondemand.readiness(self.cfg)["mops_consent"], "yes")



# ---------------------------------------------------------------------------------------------------------------
# Review fixes (feat/ondemand-l2)

class TestPendingUpdate(CliCase):
    """A fetch whose update pass was skipped is finished by the next fetch-docs (stored_since), with a line and a
    next_command saying so; the counts add up."""

    def base(self):
        with mock.patch.object(ondemand, "plan", side_effect=AssertionError("no fetch")):
            code, _, err = self.main(["screen", "humanoid robots", "--reads", "1", "--fetch-docs", "off",
                                      "--cards", "0"])
        self.assertEqual(code, 0, err)
        with store.session(self.cfg, read_only=True) as con:
            return con.execute("SELECT run_id, output_dir FROM screen_runs ORDER BY started_at DESC LIMIT 1"
                               ).fetchone()

    def acme(self, od):
        res = json.loads((Path(od) / "results.json").read_text(encoding="utf-8"))
        r = next(r for r in res["rows"] + res["unverified"] if r["security_id"] == "NYSE:ACME")
        return r["l2_evidence"], r.get("doc_fetch")

    def test_no_update_then_fetch_docs_finishes_the_update(self):
        rid, od = self.base()
        with self.ready(), self.fake_launch():
            code, out, err = self.main(["fetch-docs", "--json", "--no-update"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["update_skipped"], "no_update")
        self.assertEqual(data["next_command"], "jevscreen fetch-docs latest")
        self.assertTrue(any("jevscreen fetch-docs latest" in line and "not updated" in line
                            for line in data["console"]), data["console"])
        # 1 read + 1 stored, not re-read yet + 6 profile only = the run's 8 L2 inputs
        self.assertTrue(data["summary_en"].startswith(
            "1 read official annual reports; 1 more have an annual report stored but the results are not updated yet "
            "(next: jevscreen fetch-docs latest); 6 read the profile only"), data["summary_en"])
        md = (Path(od) / "report.md").read_text(encoding="utf-8")
        self.assertIn("Annual report stored, but the results are not updated with it yet", md)
        self.assertEqual(self.acme(od), ("profile", None))
        with self.ready(), self.fake_launch(fetch=False):
            code, out, err = self.main(["fetch-docs", "latest", "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["fetch"]["by_reason"].get("stored_since"), 1, data["fetch"]["by_reason"])
        self.assertEqual((data["update"] or {}).get("status"), "ok", data["update_skipped"])
        self.assertEqual(self.acme(od), ("annual_report", "fetched_now"))
        self.assertIsNone(data["next_command"])
        self.assertIn("(1 stored earlier)", data["summary_en"])
        # nothing new since: no update loop
        with self.ready(), self.fake_launch(fetch=False):
            code, out, err = self.main(["fetch-docs", "latest", "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["update"], data["update_skipped"]), (None, "nothing_fetched"))

    def test_jev_down_then_retry_and_budget_zero(self):
        self.base()

        def dead(*a, **kw):
            from test_screen import JevUnavailable
            raise JevUnavailable("401")
        with self.ready(), self.fake_launch(), mock.patch.object(screen, "_default_factory", dead):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    mock.patch.object(screen, "_default_keywords", self.kw):
                code = cli.main(["fetch-docs"])
        self.assertEqual(code, 6)
        self.assertIn("run jevscreen fetch-docs latest later", out.getvalue())
        with self.ready(), self.fake_launch(fetch=False):
            code, out, err = self.main(["fetch-docs", "--json", "--budget", "0"])
        data = json.loads(out)
        self.assertEqual((code, data["update_skipped"], data["next_command"]),
                         (0, "no_budget", "jevscreen fetch-docs latest"))
        with self.ready(), self.fake_launch(fetch=False):
            code, out, err = self.main(["fetch-docs", "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["update"]["status"], "ok")

    def test_a_document_older_than_the_run_is_a_gap_not_a_pending_update(self):
        make_run(self.cfg, "scr-1", profile=[s for s, *_ in COMPANIES], stored=())
        with self.ready():
            pl = self.plan()
        self.assertEqual(pl.entries[CK["NASDAQ:DOCD"]]["reason"], "already_stored")   # stored before the run
        oc = ondemand.outcomes(pl, {}, {}, {}, {}, {})
        summ = ondemand.fetch_summary(pl, oc, {}, {}, {}, seconds=0.0, time_s=1.0)
        self.assertTrue(summ["summary_en"].startswith("0 read official annual reports; 8 read the profile only"),
                        summ["summary_en"])
        self.assertIn("NASDAQ:DOCD", {r["security_id"] for r in ondemand.outcome_rows(pl, oc)})


class TestUpdateRunBudget(ScreenCase):
    def test_from_run_of_an_update_run_keeps_the_original_budget(self):
        base = self.run_screen()
        self.assertEqual(base["budget_usd"], 3.0)
        self.add_acme_doc()
        upd = self.run_screen(from_run=base["run_id"], supersedes=base["run_id"], budget_usd=0.05,
                              fetch_info=self.info())
        self.assertEqual((upd["budget_usd"], upd["params"]["budget_usd"], upd["params"]["update_budget_usd"]),
                         (0.05, 3.0, 0.05))
        later = self.run_screen(from_run=upd["run_id"])
        self.assertEqual(later["budget_usd"], 3.0)


class TestSkipEvents(LaunchCase):
    def test_skip_event_text_has_a_date_and_days_and_is_remembered(self):
        robin = CK["NSE:ROBIN"]
        oc = ondemand.outcomes(self.pl, {"bse": [{"security_ids": ["NSE:ROBIN"], "status": "skipped_current",
                                                  "note": None}]},
                               {"bse": {"status": "ok"}}, {"bse": {}}, {}, {})
        self.assertEqual(oc[robin]["code"], "known_failure")
        self.assertEqual(oc[robin]["days"], ondemand.SKIP_CACHE_DAYS)
        rows = ondemand.outcome_rows(self.pl, oc)
        for lang in ("zh", "en"):
            text = " ".join(ondemand.gap_lines(rows, lang))
            self.assertNotIn("None", text)
            self.assertIn(str(ondemand.SKIP_CACHE_DAYS), text)
        self.assertIn("could not be read", " ".join(ondemand.gap_lines(rows, "en")))
        for lang in ("zh", "en"):
            self.assertNotIn("None", ondemand.reason_text("known_failure", lang, note=None, date=None, days=None))
            self.assertNotIn("None", ondemand.reason_text("source_paused", lang, when=None))
        # launch remembers the skip: the next plan does not queue the company again for SKIP_CACHE_DAYS
        procs = {"sec": lambda k: FakeProc(k, self.fdir, self.clock, [(1, ["NYSE:ACME"], "skipped_unchanged")],
                                           exit_at=2),
                 "bse": lambda k: FakeProc(k, self.fdir, self.clock, [(1, ["NSE:ROBIN"], "no_annual_report")],
                                           exit_at=2)}
        res = self.launch(procs)
        self.assertEqual(res["outcomes"][CK["NYSE:ACME"]]["code"], "known_failure")
        with self.ready():
            pl = self.plan(sources=["sec", "bse"])
        self.assertEqual(pl.entries[CK["NYSE:ACME"]]["reason"], "known_failure")
        self.assertNotIn("sec", pl.by_source)
        with self.ready():
            pl = self.plan(sources=["sec", "bse"], retry_failed=True)
        self.assertIn("sec", pl.by_source)

    def test_ok_event_without_a_document(self):
        acme = CK["NYSE:ACME"]
        ev = {"sec": [{"security_ids": ["NYSE:ACME"], "status": "ok"}]}
        oc = ondemand.outcomes(self.pl, ev, {"sec": {"status": "store_locked"}}, {"sec": {}}, {}, {})
        self.assertEqual(oc[acme]["code"], "store_busy")
        oc = ondemand.outcomes(self.pl, ev, {"sec": None}, {"sec": {"killed": True}}, {}, {})
        self.assertEqual(oc[acme]["code"], "deferred_time")
        oc = ondemand.outcomes(self.pl, ev, {"sec": {"status": "interrupted", "stopped_reason": "deadline"}},
                               {"sec": {"terminated": "deadline"}}, {}, {})
        self.assertEqual(oc[acme]["code"], "deferred_time")
        oc = ondemand.outcomes(self.pl, ev, {"sec": {"status": "ok"}}, {"sec": {}}, {}, {})
        self.assertEqual(oc[acme]["code"], "extract_failed")

    def test_interrupt_while_starting_children_stops_the_started_ones(self):
        made = []
        for exc in (KeyboardInterrupt, cli.Terminated):
            made.clear()

            def popen(cmd, **kwargs):
                if made:
                    raise exc("SIGTERM") if exc is cli.Terminated else exc()
                p = FakeProc(cmd[cmd.index("--source") + 1], self.fdir, self.clock, [], stuck=True)
                made.append(p)
                return p
            res = ondemand.launch(self.cfg, self.pl, time_s=60, fetch_dir=self.fdir, lang="en",
                                  out=self.lines.append, popen=popen, clock=self.clock, sleep=self.clock.sleep,
                                  grace_s=5.0)
            self.assertTrue(res["interrupted"])
            self.assertEqual(len(made), 1)
            self.assertIsNotNone(made[0].term_at)
            self.assertIsNotNone(made[0].killed_at)


class TestQuickstartOptions(CliCase):
    """What quickstart's fetch step uses (the one on-demand path): progress counts and a stopping interrupt."""

    def test_launch_reports_progress_as_companies_settle(self):
        seen = []
        clock = FakeClock()
        fdir = self.home / "out" / "fetch-p"
        make_run(self.cfg, "scr-p")
        with self.ready():
            pl = self.plan("scr-p", sources=["sec", "bse"])

        def popen(cmd, **kwargs):
            k = cmd[cmd.index("--source") + 1]
            sid = "NYSE:ACME" if k == "sec" else "NSE:ROBIN"
            return FakeProc(k, fdir, clock, [(1, [sid], "no_annual_filing")], exit_at=2)
        ondemand.launch(self.cfg, pl, time_s=60, fetch_dir=fdir, lang="en", out=lambda s: None, popen=popen,
                        clock=clock, sleep=clock.sleep, grace_s=5.0,
                        on_progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen[0], (0, 2))
        self.assertEqual(seen[-1], (2, 2))
        self.assertEqual(seen, sorted(set(seen)))          # only changes, never backwards

    def test_interrupt_stops_skips_the_update_and_raises(self):
        base = self.run_screen()
        base = dict(base, output_dir=str(self.home / "out"))
        got = {}

        def launch(cfg, pl, *, time_s, fetch_dir, on_progress=None, mode="auto", **kw):
            got["on_progress"] = on_progress
            self.add_acme_doc()
            oc = {e["company_key"]: {"source": k, "code": "fetched" if e["security_id"] == "NYSE:ACME" else
                                     "deferred_time", "note": None} for k, ents in pl.by_source.items() for e in ents}
            summ = ondemand.fetch_summary(pl, oc, {}, {}, {}, seconds=1.0, time_s=time_s, interrupted=True,
                                          mode=mode)
            return {"summary": summ, "outcomes": oc, "fetched": [ck for ck, o in oc.items()
                                                                 if o["code"] == "fetched"],
                    "docs_by_ck": {}, "interrupted": True, "store_ok": True}
        lines: list[str] = []
        prog = []
        with self.ready(sec_email=True), mock.patch.object(ondemand, "launch", launch), \
                self.assertRaises(KeyboardInterrupt):
            ondemand_cli.run_fetch(self.cfg, base, time_s=30, update_budget=0.05, out=lines.append,
                                   on_progress=lambda d, t: prog.append((d, t)), interrupt_stops=True)
        self.assertIsNotNone(got["on_progress"])
        res = json.loads((self.home / "out" / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(res["run_id"], base["run_id"])                     # no update pass
        self.assertEqual(res["layers"]["fetch"]["update"], {"skipped": "interrupted"})
        self.assertFalse((self.home / "out" / "results.v1.json").exists())
        self.assertEqual(res["layers"]["fetch"]["next_command"], "jevscreen fetch-docs latest")


class TestScreenProgressAndIdeaEn(ScreenCase):
    def test_progress_phases(self):
        phases = []
        self.run_screen(progress=lambda ph, d, t: phases.append(ph))
        self.assertTrue({"l1", "l2"} <= set(phases), phases)

    def test_agent_idea_en_skips_the_local_model(self):
        res = screen.screen(self.cfg, "人形机器人", idea_en="Humanoid robots", jev_factory=make_factory(self.log),
                            out_dir=self.home / "out", keywords_fn=self.kw, reads=1)
        self.assertEqual((res["idea_en"], res["keywords"]["status"]), ("Humanoid robots", "agent"))
        self.assertEqual(self.kw.calls, [])
        self.assertFalse(any("unavailable" in w for w in res["warnings"]))
        self.assertEqual(res["params"]["idea_en_source"], "agent")


class TestNetworkKillSwitch(CliCase):
    def test_real_adapters_are_refused_under_testing(self):
        self.assertEqual(os.environ.get("JEVSCREEN_TESTING"), "1")
        make_run(self.cfg, "scr-1")
        with self.ready():
            pl = self.plan()
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a real child was started")):
            with self.assertRaises(ondemand.RealAdaptersRefused):
                ondemand.launch(self.cfg, pl, time_s=5, fetch_dir=self.home / "out" / "fetch")
        with self.assertRaises(ondemand.RealAdaptersRefused):
            ondemand.adapter_module("sec")
        with mock.patch.dict(os.environ, {"JEVSCREEN_ONDEMAND_MODULES": json.dumps({"sec": "json"})}):
            self.assertEqual(ondemand.adapter_module("sec"), "json")
        with mock.patch.dict(os.environ, {"JEVSCREEN_TESTING": "0"}):
            self.assertEqual(ondemand.adapter_module("sec"), "jevscreen.sources.sec_edgar")


class TestSecEmailQuestion(Home):
    def test_asked_until_a_recorded_no(self):
        from jevscreen import consent
        make_run(self.cfg, "scr-1")
        with self.ready(sec_email=False, sec_email_ask=None, mops_consent=None):
            pl = self.plan()
        qs = ondemand.questions_for(pl, {"no_key_sec": 1})
        self.assertEqual([q["id"] for q in qs], ["sec_email"])
        self.assertIn("jevscreen consent set sec-email-ask no", qs[0]["record_answer_commands"])
        # after a yes (the key or the consent is recorded) the fetch and the update actually happen
        self.assertEqual(qs[0]["then_command"], "jevscreen fetch-docs scr-1")
        tw = ondemand.questions_for(pl, {"mops_off": 2})
        self.assertEqual([(q["id"], q["then_command"]) for q in tw], [("mops_annual", "jevscreen fetch-docs scr-1")])
        with self.ready(sec_email=False, sec_email_ask="no"):
            pl = self.plan()
        self.assertEqual(ondemand.questions_for(pl, {"no_key_sec": 1}), [])
        self.assertIn("sec-email-ask", consent.TOPICS)
        consent.record(self.cfg, "sec-email-ask", "no")
        self.assertEqual(ondemand.readiness(self.cfg)["sec_email_ask"], "no")
        with mock.patch.object(ondemand, "pdf_backends", return_value=["pymupdf"]):
            c = {x["id"]: x for x in ondemand.doctor_checks(self.cfg)}
        self.assertFalse(c["on_demand_docs"]["ask_human"])


class TestAtomicOutputs(ScreenCase):
    def test_an_interrupted_update_keeps_results_json(self):
        base = self.run_screen()
        self.add_acme_doc()
        out = self.home / "out"
        real, calls = os.replace, {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return real(a, b)
        with mock.patch.object(screen.os, "replace", flaky), self.assertRaises(KeyboardInterrupt):
            self.run_screen(from_run=base["run_id"], supersedes=base["run_id"], budget_usd=0.05,
                            fetch_info=self.info())
        self.assertEqual(json.loads((out / "results.json").read_text(encoding="utf-8"))["run_id"], base["run_id"])
        self.assertEqual(list(out.glob(".*.tmp")), [])
        with store.session(self.cfg, read_only=True) as con:
            self.assertEqual(cli._resolve_run(con, "latest")[0], base["run_id"])
        upd = self.run_screen(from_run=base["run_id"], supersedes=base["run_id"], budget_usd=0.05,
                              fetch_info=self.info())
        self.assertEqual(json.loads((out / "results.json").read_text(encoding="utf-8"))["run_id"], upd["run_id"])
        self.assertEqual(json.loads((out / "results.v1.json").read_text(encoding="utf-8"))["run_id"], base["run_id"])


class TestNoviceText(Home):
    def test_text_details(self):
        self.assertEqual(ondemand.dry_run_suffix("zh"), "（另加最多 2 分钟，免费补抓年报：只抓通过第一轮、本地没有年报原文的公司）")
        self.assertIn("1 more company now read", ondemand.end_line(1, 0.0, 0, "en"))
        self.assertIn("2 more companies now read", ondemand.end_line(2, 0.0, 0, "en"))
        self.assertEqual([ondemand.lang_of(x) for x in ("半導体製造装置", "人形机器人", "半導體製造設備", "半导体制造设备",
                                                          "人形機器人")], ["en", "zh", "zh", "zh", "zh"])
        make_run(self.cfg, "scr-1")
        guard.mark_blocked(self.cfg, "sync-bse", url="https://example.invalid", status=403, reason="http_403")
        with self.ready():
            pl = self.plan()
        self.assertTrue(pl.entries[CK["NSE:ROBIN"]]["when"].endswith(" UTC"))


if __name__ == "__main__":
    unittest.main()
