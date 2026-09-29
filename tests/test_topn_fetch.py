"""Tests for jevscreen.topn_fetch (the top-N official-text levers): selection, the plan (outside_topn, EDINET, order,
caps, deep re-reads), the sequential launch (stop at the first block, time), real child processes (EDINET and the
deep arguments reach the adapter), the update pass, `screen` and `eval run` wiring, and that both levers are off by
default.

No network: children run tests/ondemand_fake_adapter.py (JEVSCREEN_TESTING=1), or the launch is a fake. Every Jev
client is a fake. Issuers and texts are invented.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "src"))
sys.path.insert(0, str(TESTS))
import safe_env  # noqa: E402,F401

from jevscreen import cli, eval_cli, guard, ondemand, screen, store, topn_fetch  # noqa: E402
from test_ondemand import CK, CliCase, Home, ScreenCase, make_run  # noqa: E402
from test_screen import make_factory  # noqa: E402

ORDER_ROWS = ["NASDAQ:DOCD", "KRX:099999", "SSE:600999", "TSE:7999", "NYSE:ACME", "NSE:ROBIN", "TWSE:2999",
              "IDX:ROBI"]


def fake_result(run_id="scr-1", rows=ORDER_ROWS, annual=("NASDAQ:DOCD",), out_dir=None, sources=None):
    sources = sources or {}
    return {"run_id": run_id, "idea": "humanoid robots", "status": "ok", "output_dir": str(out_dir or "/tmp/x"),
            "rows": [{"rank": i + 1, "security_id": sid, "company_key": CK[sid], "name": sid,
                      "l2_evidence": "annual_report" if sid in annual else "profile",
                      "filing_source": sources.get(sid, "sec_filing_text" if sid in annual else None)}
                     for i, sid in enumerate(rows)]}


class TestSelection(unittest.TestCase):
    def test_profile_only_and_shallow(self):
        res = fake_result(annual=("NASDAQ:DOCD", "SSE:600999"), sources={"SSE:600999": "cninfo_annual_report"})
        self.assertEqual(topn_fetch.profile_only(res, 4), [CK["KRX:099999"], CK["TSE:7999"]])
        self.assertEqual(topn_fetch.profile_only(res, 0), [])
        self.assertEqual([r["security_id"] for r in topn_fetch.shallow_cjk(res, 4)], ["SSE:600999"])
        self.assertEqual(topn_fetch.shallow_cjk(res, 1), [])          # DOCD reads SEC text: not a CJK source

    def test_flags_default_off(self):
        p = cli.build_parser()
        a = p.parse_args(["screen", "x"])
        self.assertEqual((a.fetch_profile_only_topn, a.deepen_official_topn), (0, 0))
        self.assertFalse(topn_fetch.wanted(a))
        a = p.parse_args(["eval", "run", "--budget-each", "0.1", "--budget-total", "1"])
        self.assertEqual((a.fetch_profile_only_topn, a.deepen_official_topn, a.from_run), (0, 0, None))
        self.assertFalse(topn_fetch.wanted(a))
        a = p.parse_args(["screen", "x", "--fetch-profile-only-topn", "10", "--deepen-official-topn", "5"])
        self.assertTrue(topn_fetch.wanted(a))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            p.parse_args(["screen", "x", "--fetch-profile-only-topn", "-1"])

    def test_start_line_names_both_counts_in_the_run_language(self):
        pl = ondemand.Plan(run_id="r", by_source={"cninfo": [{"company_key": "a"}]})
        pl.topn = {"topn": 10, "deepen": 5, "selected": ["a"], "deepen_ready": ["b"]}
        pl.deepen = []
        zh = topn_fetch.start_text(pl, "zh", 300)
        en = topn_fetch.start_text(pl, "en", 300)
        self.assertIn("前 10 名", zh)
        self.assertIn("前 5 名", zh)
        self.assertIn("top 10", en)
        self.assertIn("top 5 deeper", en)
        self.assertIn("already have the deeper text stored", en)
        self.assertNotRegex(en, r"[\u4e00-\u9fff]")

    def test_explicit_fetch_docs_off_wins(self):
        args = argparse.Namespace(fetch_docs="off", fetch_profile_only_topn=5, deepen_official_topn=0)
        lines: list[str] = []
        res = {"idea": "humanoid robots", "status": "ok"}
        with mock.patch.object(topn_fetch, "run", side_effect=AssertionError("no download")):
            self.assertIs(topn_fetch.fetch_phase(args, None, res, out=lines.append), res)
        self.assertIn("--fetch-docs off", lines[0])
        with mock.patch.object(topn_fetch, "run", side_effect=AssertionError("no download")):
            topn_fetch.fetch_phase(args, None, dict(res, idea="人形机器人"), out=lines.append)
        self.assertIn("不下载任何年报", lines[1])

    def test_stopped_after_block_names_a_next_step(self):
        self.assertEqual(ondemand.REASONS["stopped_after_block"]["next_command"], "jevscreen fetch-docs latest")
        self.assertIn("现在就可以再补抓", ondemand.reason_text("stopped_after_block", "zh"))
        self.assertIn("can be fetched now", ondemand.reason_text("stopped_after_block", "en"))

    def test_parse_from_runs(self):
        self.assertEqual(topn_fetch.parse_from_runs("a=scr-1, b = scr-2"), {"a": "scr-1", "b": "scr-2"})
        self.assertEqual(topn_fetch.parse_from_runs(None), {})
        with self.assertRaises(ValueError):
            topn_fetch.parse_from_runs("a")
        with contextlib.ExitStack() as st:
            import tempfile
            d = Path(st.enter_context(tempfile.TemporaryDirectory()))
            (d / "m.json").write_text(json.dumps({"a": "scr-1", "b": {"run_id": "scr-2"}}), encoding="utf-8")
            self.assertEqual(topn_fetch.parse_from_runs(str(d / "m.json")), {"a": "scr-1", "b": "scr-2"})


class TestPlan(Home):
    def planner(self, res, **kw):
        return topn_fetch.make_planner(res, **kw)(self.cfg, res["run_id"])

    def test_outside_topn_order_and_edinet(self):
        make_run(self.cfg, "scr-1")
        res = fake_result()
        with self.ready(), mock.patch.dict(os.environ, {"JEVSCREEN_EDINET_API_KEY": "A" * 32}):
            pl = self.planner(res, topn=5)
        self.assertEqual(list(pl.by_source), ["dart", "cninfo", "edinet", "sec"])       # BSE -> DART -> CNINFO -> ...
        self.assertEqual([e["security_id"] for e in pl.by_source["edinet"]], ["TSE:7999"])
        reasons = {pl.entries[CK[s]]["security_id"]: pl.entries[CK[s]].get("reason") for s in ORDER_ROWS[1:]}
        self.assertEqual(reasons, {"KRX:099999": None, "SSE:600999": None, "TSE:7999": None, "NYSE:ACME": None,
                                   "NSE:ROBIN": "outside_topn", "TWSE:2999": "outside_topn",
                                   "IDX:ROBI": "outside_topn"})
        self.assertEqual(pl.topn["selected"], [CK[s] for s in ("KRX:099999", "SSE:600999", "TSE:7999", "NYSE:ACME")])
        self.assertEqual(pl.skip_counts(), {"outside_topn": 3})
        self.assertIn("edinet", topn_fetch.registry())
        self.assertNotIn("edinet", ondemand.SOURCES)                  # registered only while the fetch runs

    def test_edinet_without_key_and_paused_source(self):
        make_run(self.cfg, "scr-1")
        os.environ.pop("JEVSCREEN_EDINET_API_KEY", None)
        guard.mark_blocked(self.cfg, "sync-dart", url="https://example.invalid", status=403, reason="test")
        with self.ready():
            pl = self.planner(fake_result(), topn=4)
        self.assertEqual(pl.entries[CK["TSE:7999"]]["reason"], "no_key_edinet")
        self.assertEqual(pl.entries[CK["KRX:099999"]]["reason"], "source_paused")
        self.assertEqual(list(pl.by_source), ["cninfo"])
        for lang in ("zh", "en"):
            self.assertNotIn("{", ondemand.reason_text("no_key_edinet", lang))
            self.assertNotIn("{", ondemand.reason_text("stopped_after_block", lang))
            self.assertNotIn("{", ondemand.reason_text("outside_topn", lang))

    def test_cap_in_rank_order_and_deferred_cap_of_the_base_plan_is_readded(self):
        make_run(self.cfg, "scr-1")
        import dataclasses
        sec0 = dataclasses.replace(ondemand.SOURCES["sec"], cap=0)          # the base plan defers every SEC company
        with self.ready(), mock.patch.dict(ondemand.SOURCES, {"sec": sec0}):
            base = ondemand.plan(self.cfg, "scr-1")
            self.assertEqual(base.entries[CK["NYSE:ACME"]]["reason"], "deferred_cap")
        with self.ready():
            pl = self.planner(fake_result(), topn=5)
        self.assertEqual([e["security_id"] for e in pl.by_source["sec"]], ["NYSE:ACME"])
        self.assertNotIn("cap", pl.entries[CK["NYSE:ACME"]])

    def test_deepen_entries_and_a_deep_text_stored_earlier(self):
        make_run(self.cfg, "scr-1", stored=("NASDAQ:DOCD", "SSE:600999"))
        add_cn_doc(self.home, self.cfg)
        res = fake_result(annual=("NASDAQ:DOCD", "SSE:600999"), sources={"SSE:600999": "cninfo_annual_report"})
        with self.ready():
            pl = self.planner(res, topn=0, deepen=4)
        self.assertEqual([e["security_id"] for e in pl.by_source["cninfo"]], ["SSE:600999"])
        self.assertEqual(pl.deepen, [CK["SSE:600999"]])
        self.assertNotIn(CK["SSE:600999"], pl.entries)                   # not a profile input: no gap row
        # an earlier arm stored the deep text: read it (deepen_ready, the update pass runs), never fetched again
        add_cn_doc(self.home, self.cfg, deep=True)
        with self.ready():
            pl = self.planner(res, topn=0, deepen=4)
        self.assertEqual((pl.by_source, pl.deepen_ready), ({}, [CK["SSE:600999"]]))
        # a deep text that added nothing: nothing to read or fetch
        add_cn_doc(self.home, self.cfg, deep=True, same=True)
        with self.ready():
            pl = self.planner(res, topn=0, deepen=4)
        self.assertEqual((pl.by_source, pl.deepen_ready, pl.deepen_skipped),
                         ({}, [], {CK["SSE:600999"]: "unchanged"}))

    def test_profile_row_with_a_deep_text_stored_by_an_earlier_fetch_is_read_not_fetched(self):
        add_cn_doc(self.home, self.cfg, deep=True)          # stored before this run (an earlier arm)
        time.sleep(0.01)
        make_run(self.cfg, "scr-1")
        with self.ready():
            pl = self.planner(fake_result(), topn=4)
        self.assertEqual(pl.entries[CK["SSE:600999"]]["reason"], "stored_since")     # the update pass reads it
        self.assertNotIn("cninfo", pl.by_source)
        base = ondemand.plan(self.cfg, "scr-1")                                   # outside the top-N view: unseen
        self.assertIsNone(base.entries[CK["SSE:600999"]].get("reason"))

    def test_failed_deep_reread_is_not_retried_for_a_while(self):
        make_run(self.cfg, "scr-1", stored=("NASDAQ:DOCD", "SSE:600999"))
        add_cn_doc(self.home, self.cfg)
        with store.session(self.cfg) as con:
            con.execute("INSERT INTO documents (doc_id, security_id, company_key, source_id, cik, form, section, "
                        "accession, text_path, filing_date, extractor, fetched_at) VALUES ('cninfo_annual_report:o1:"
                        "A1:business_deep', 'SSE:600999', ?, 'cninfo_annual_report', 'o1', 'annual_report', "
                        "'business_deep', 'A1', NULL, DATE '2026-04-01', 'cninfo-v4/none', ?)",
                        [CK["SSE:600999"], store.now_utc()])
        res = fake_result(annual=("NASDAQ:DOCD", "SSE:600999"), sources={"SSE:600999": "cninfo_annual_report"})
        with self.ready():
            pl = self.planner(res, topn=0, deepen=4)
        self.assertEqual((pl.by_source, pl.deepen_skipped), ({}, {CK["SSE:600999"]: "known_failure"}))
        with self.ready():
            pl = topn_fetch.make_planner(res, topn=0, deepen=4)(self.cfg, "scr-1", retry_failed=True)
        self.assertEqual(pl.deepen, [CK["SSE:600999"]])

    def test_deepen_rows_beyond_the_cap_get_an_outcome(self):
        make_run(self.cfg, "scr-1", stored=("NASDAQ:DOCD", "SSE:600999"))
        add_cn_doc(self.home, self.cfg)
        import dataclasses
        cn0 = dataclasses.replace(ondemand.SOURCES["cninfo"], cap=0)
        res = fake_result(annual=("NASDAQ:DOCD", "SSE:600999"), sources={"SSE:600999": "cninfo_annual_report"})
        with self.ready(), mock.patch.dict(ondemand.SOURCES, {"cninfo": cn0}):
            pl = self.planner(res, topn=0, deepen=4)
        self.assertEqual((pl.by_source, pl.deepen_skipped), ({}, {CK["SSE:600999"]: "deferred_cap"}))

    def test_block_in_the_runs_table_pauses_the_source(self):
        make_run(self.cfg, "scr-1")
        with store.session(self.cfg) as con:
            con.execute("INSERT INTO runs (run_id, command, started_at, status) VALUES ('sync-x', 'sync-dart', ?, "
                        "'blocked')", [store.now_utc()])
        with self.ready():
            pl = self.planner(fake_result(), topn=4)
        self.assertEqual(pl.entries[CK["KRX:099999"]]["reason"], "source_paused")

    def test_edinet_known_failure_is_not_fetched_again(self):
        make_run(self.cfg, "scr-1")
        with store.session(self.cfg) as con:
            con.execute("INSERT INTO crawl_state (source_id, security_id, status, attempts, last_attempt_at, note) "
                        "VALUES ('edinet_yuho', 'TSE:7999', 'extract_failed', 1, ?, 'element_not_found')",
                        [store.now_utc()])
        with self.ready(), mock.patch.dict(os.environ, {"JEVSCREEN_EDINET_API_KEY": "A" * 32}):
            pl = self.planner(fake_result(), topn=5)
        e = pl.entries[CK["TSE:7999"]]
        self.assertEqual((e["reason"], e["source"]), ("known_failure", "edinet"))
        self.assertNotIn("edinet", pl.by_source)


CN_TEXT = "示例公司主要从事示例机器人关节的研发、生产和销售。" * 5
CN_DEEP = CN_TEXT + "\n\n【营业收入构成】\n" + "示例机器人关节产品的营业收入占公司营业收入的比例超过百分之六十。" * 3


def add_cn_doc(home, cfg, *, deep=False, same=False):
    """The shallow (or, deep=True, the deep) row of one CNINFO report of SSE:600999; same=True: the deep text equals
    the shallow one (nothing found to add)."""
    from jevscreen import store as st
    sec = "business_deep" if deep else "business"
    path = Path(home) / "docs" / f"cn-{sec}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = CN_TEXT if (not deep or same) else CN_DEEP
    path.write_text(text, encoding="utf-8")
    with st.session(cfg) as con:
        con.execute("DELETE FROM documents WHERE doc_id = ?", [f"cninfo_annual_report:o1:A1:{sec}"])
        con.execute("INSERT INTO documents (doc_id, security_id, company_key, source_id, cik, form, section, "
                    "accession, text_path, text_sha256, filing_date, extractor, fetched_at) VALUES (?, 'SSE:600999', ?, "
                    "'cninfo_annual_report', 'o1', 'annual_report', ?, 'A1', ?, ?, DATE '2026-04-01', ?, ?)",
                    [f"cninfo_annual_report:o1:A1:{sec}", CK["SSE:600999"], sec, str(path),
                     st.sha256(text.encode("utf-8")), "cninfo-v4/pymupdf" if deep else "cninfo-v3/pymupdf",
                     st.now_utc()])


def fake_launch_fn(record, *, blocked=(), fetch=("NYSE:ACME",), deepen_ok=(), on_fetch=None):
    """A stand-in for ondemand.launch over one source's sub-plan."""
    def launch(cfg, pl, *, time_s, fetch_dir, retry_failed=False, lang="en", out=print, mode="topn", **kw):
        [key] = list(pl.by_source)
        record.append((key, [e["security_id"] for e in pl.by_source[key]], round(time_s)))
        Path(fetch_dir).mkdir(parents=True, exist_ok=True)
        oc, fetched = {}, []
        with open(Path(fetch_dir) / f"{key}.events.jsonl", "w", encoding="utf-8") as fh:
            for e in pl.by_source[key]:
                sid = e["security_id"]
                ok = sid in fetch or sid in deepen_ok
                if ok and on_fetch:
                    on_fetch(sid)
                fh.write(json.dumps({"security_ids": [sid], "status": "ok" if ok else "no_annual_filing"}) + "\n")
                oc[e["company_key"]] = {"source": key, "code": "fetched" if ok or e.get("deepen") else
                                        ("blocked" if key in blocked else "no_filing"), "note": None}
                if ok and not e.get("deepen"):
                    fetched.append(e["company_key"])
        summ = {"sources": {key: {"status": "blocked" if key in blocked else "ok"}}}
        return {"summary": summ, "outcomes": oc, "fetched": fetched, "docs_by_ck": {}, "interrupted": False,
                "store_ok": True}
    return launch


class TestSequentialLaunch(Home):
    def plan(self, **kw):
        make_run(self.cfg, "scr-1")
        with self.ready(), mock.patch.dict(os.environ, {"JEVSCREEN_EDINET_API_KEY": "A" * 32}):
            return topn_fetch.make_planner(fake_result(), topn=5, **kw)(self.cfg, "scr-1")

    def test_one_source_at_a_time_and_stop_at_the_first_block(self):
        pl = self.plan()
        rec: list = []
        fr = topn_fetch.make_launcher(launch=fake_launch_fn(rec, blocked=("cninfo",)))(
            self.cfg, pl, time_s=60, fetch_dir=self.home / "f", out=lambda s: None)
        self.assertEqual([r[0] for r in rec], ["dart", "cninfo"])        # edinet and sec never contacted
        oc = {pl.entries[ck]["security_id"]: o["code"] for ck, o in fr["outcomes"].items()}
        self.assertEqual((oc["KRX:099999"], oc["SSE:600999"], oc["TSE:7999"], oc["NYSE:ACME"]),
                         ("no_filing", "blocked", "stopped_after_block", "stopped_after_block"))
        self.assertEqual(oc["NSE:ROBIN"], "outside_topn")
        s = fr["summary"]
        self.assertEqual((s["status"], s["topn"]["stopped"]), ("partial", "stopped_after_block"))
        self.assertEqual((s["sources"]["edinet"]["status"], s["sources"]["cninfo"]["status"]), ("not_run", "blocked"))
        self.assertEqual(fr["fetched"], [])

    def test_time_runs_out_between_sources(self):
        pl = self.plan()
        rec: list = []
        clock = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 100.0])
        with mock.patch.object(topn_fetch.time, "monotonic", lambda: next(clock, 100.0)):
            fr = topn_fetch.make_launcher(launch=fake_launch_fn(rec))(self.cfg, pl, time_s=60,
                                                                      fetch_dir=self.home / "f", out=lambda s: None)
        self.assertEqual([r[0] for r in rec], ["dart"])
        self.assertEqual(fr["outcomes"][CK["NYSE:ACME"]]["code"], "deferred_time")

    def test_deepened_comes_from_the_store_not_the_event(self):
        make_run(self.cfg, "scr-1", stored=("NASDAQ:DOCD", "SSE:600999"))
        add_cn_doc(self.home, self.cfg)
        res = fake_result(annual=("NASDAQ:DOCD", "SSE:600999"), sources={"SSE:600999": "cninfo_annual_report"})
        with self.ready():
            pl = topn_fetch.make_planner(res, topn=0, deepen=3)(self.cfg, "scr-1")
        rec: list = []
        # the child reported 'ok' but no deep text was stored (its write failed): not deepened
        fr = topn_fetch.make_launcher(launch=fake_launch_fn(rec, fetch=(), deepen_ok=("SSE:600999",)))(
            self.cfg, pl, time_s=60, fetch_dir=self.home / "f", out=lambda s: None)
        self.assertEqual(fr["deepened"], [])
        self.assertEqual(fr["summary"]["topn"]["deepen_outcomes"], {CK["SSE:600999"]: "not_stored"})
        # the deep text stored, yet identical to the shallow one: unchanged
        rec.clear()
        fr = topn_fetch.make_launcher(launch=fake_launch_fn(
            rec, fetch=(), deepen_ok=("SSE:600999",),
            on_fetch=lambda sid: add_cn_doc(self.home, self.cfg, deep=True, same=True)))(
            self.cfg, pl, time_s=60, fetch_dir=self.home / "g", out=lambda s: None)
        self.assertEqual((fr["deepened"], fr["summary"]["topn"]["deepen_outcomes"]),
                         ([], {CK["SSE:600999"]: "unchanged"}))
        rec.clear()
        fr = topn_fetch.make_launcher(launch=fake_launch_fn(
            rec, fetch=(), deepen_ok=("SSE:600999",), on_fetch=lambda sid: add_cn_doc(self.home, self.cfg, deep=True)))(
            self.cfg, pl, time_s=60, fetch_dir=self.home / "h", out=lambda s: None)
        self.assertEqual(fr["deepened"], [CK["SSE:600999"]])
        self.assertEqual(fr["summary"]["topn"]["deepen_outcomes"], {CK["SSE:600999"]: "deepened"})
        self.assertNotIn(CK["SSE:600999"], fr["outcomes"])


class TestRealChildren(Home):
    """Real `python -m jevscreen.topn_fetch child` processes: EDINET is known there and the deep arguments reach the
    adapter (a probe module records them), with no network."""

    def setUp(self):
        super().setUp()
        probe = self.home / "probe_mods"
        probe.mkdir()
        (probe / "topn_probe_adapter.py").write_text(
            "import json, sys\nfrom pathlib import Path\nimport ondemand_fake_adapter as f\n"
            "def sync(cfg, client=None, **kw):\n"
            "    key = f._key()\n"
            "    rec = {k: v for k, v in kw.items() if k in ('deep', 'kind', 'mode')}\n"
            "    (Path(cfg.home) / f'probe_{key}.json').write_text(json.dumps(rec))\n"
            "    return f.sync(cfg, client, **kw)\n", encoding="utf-8")
        mods = {k: "topn_probe_adapter" for k in topn_fetch.registry()}
        os.environ.update({"JEVSCREEN_ONDEMAND_MODULES": json.dumps(mods),
                           "PYTHONPATH": os.pathsep.join([str(TESTS), str(probe)])})

    def test_edinet_and_deep_kwargs_in_the_child(self):
        make_run(self.cfg, "scr-1")
        (self.home / "fake_adapter.json").write_text(json.dumps({"edinet": {"ok": ["TSE:7999"]},
                                                                 "cninfo": {"ok": ["SSE:600999"]}}), encoding="utf-8")
        res = fake_result(rows=["SSE:600999", "TSE:7999"])
        with self.ready(), mock.patch.dict(os.environ, {"JEVSCREEN_EDINET_API_KEY": "A" * 32}):
            pl = topn_fetch.make_planner(res, topn=2)(self.cfg, "scr-1")
            fr = topn_fetch.make_launcher()(self.cfg, pl, time_s=30, fetch_dir=self.home / "f",
                                            out=lambda s: None)
        self.assertEqual(sorted(fr["fetched"]), sorted([CK["SSE:600999"], CK["TSE:7999"]]))
        self.assertEqual(json.loads((self.home / "probe_cninfo.json").read_text()), {"deep": True, "kind": "full"})
        self.assertEqual(json.loads((self.home / "probe_edinet.json").read_text()), {"deep": True})
        self.assertTrue((self.home / "f" / "1-cninfo" / "cninfo.events.jsonl").exists())

    def test_kill_switch_without_a_module_map(self):
        os.environ.pop("JEVSCREEN_ONDEMAND_MODULES", None)
        make_run(self.cfg, "scr-1")
        with self.ready():
            pl = topn_fetch.make_planner(fake_result(rows=["SSE:600999"]), topn=1)(self.cfg, "scr-1")
            with self.assertRaises(ondemand.RealAdaptersRefused):
                topn_fetch.make_launcher()(self.cfg, pl, time_s=10, fetch_dir=self.home / "f", out=lambda s: None)


class TestUpdatePass(ScreenCase):
    def test_top_rows_fetched_and_reread_others_outside(self):
        base = self.run_screen()
        rows = base["rows"]
        self.assertTrue(any(r["security_id"] == "NYSE:ACME" and r["l2_evidence"] == "profile" for r in rows))
        rec: list = []
        lines: list[str] = []
        with self.ready():
            ret = topn_fetch.run(self.cfg, base, topn=len(rows), update_budget=0.05, out=lines.append,
                                 launch=fake_launch_fn(rec, on_fetch=lambda sid: self.add_acme_doc()),
                                 jev_factory=make_factory(self.log))
        final = ret["result"]
        self.assertNotEqual(final["run_id"], base["run_id"])
        self.assertEqual(final["supersedes"], base["run_id"])
        self.assertEqual(final["layers"]["l1"]["from_run"], base["run_id"])        # L1 reused, $0
        acme = next(r for r in final["rows"] + final.get("unverified", []) if r["security_id"] == "NYSE:ACME")
        self.assertEqual((acme.get("l2_evidence"), acme.get("doc_fetch")), ("annual_report", "fetched_now"))
        self.assertEqual(final["layers"]["fetch"]["mode"], "topn")
        self.assertIn("Fetching annual reports for the top", lines[0])
        self.assertIn("the first site that refuses access stops the whole fetch", lines[0])

    def test_update_pass_keeps_the_read_offset(self):
        """A --read-offset noise run stays one: the update pass re-ranks with the same fresh band reads."""
        base = self.run_screen(reads=3, read_offset=3)
        seen: list = []
        real = screen.screen

        def spy(cfg, idea, **kw):
            seen.append(kw)
            return real(cfg, idea, **kw)
        with self.ready(), mock.patch.object(screen, "screen", side_effect=spy):
            ret = topn_fetch.run(self.cfg, base, topn=len(base["rows"]), update_budget=0.05, out=lambda s: None,
                                 launch=fake_launch_fn([], on_fetch=lambda sid: self.add_acme_doc()),
                                 jev_factory=make_factory(self.log))
        self.assertEqual(seen[0]["read_offset"], 3)
        self.assertEqual(ret["result"]["params"]["read_offset"], 3)
        self.assertNotEqual(ret["result"]["run_id"], base["run_id"])

    def test_deep_text_is_read_by_the_update_pass_only(self):
        """The fetched deep text (its own row) is read by the top-N update pass; a later screen without the levers
        reads exactly what it read before (the profile)."""
        base = self.run_screen()
        ck = CK["SSE:600999"]
        with self.ready():
            ret = topn_fetch.run(self.cfg, base, topn=3, update_budget=0.05, out=lambda s: None,
                                 launch=fake_launch_fn([], fetch=("SSE:600999",),
                                                       on_fetch=lambda sid: add_cn_doc(self.home, self.cfg, deep=True)),
                                 jev_factory=make_factory(self.log))
        final = ret["result"]
        row = next(r for r in final["rows"] + final.get("unverified", []) if r["company_key"] == ck)
        self.assertEqual(row["l2_evidence"], "annual_report")
        self.assertIn(ck, final["params"]["deep_view"])
        again = self.run_screen()
        row = next(r for r in again["rows"] + again.get("unverified", []) if r["company_key"] == ck)
        self.assertEqual(row["l2_evidence"], "profile")
        self.assertNotIn("deep_view", again["params"])
        # a --from-run of the update run (e.g. after answering its cards) keeps reading the deep text
        rerun = self.run_screen(from_run=final["run_id"], out_dir=self.home / "out3")
        row = next(r for r in rerun["rows"] + rerun.get("unverified", []) if r["company_key"] == ck)
        self.assertEqual(row["l2_evidence"], "annual_report")
        self.assertIn(ck, rerun["params"]["deep_view"])

    def test_nothing_to_do(self):
        base = self.run_screen()
        lines: list[str] = []
        ret = topn_fetch.run(self.cfg, dict(base, rows=[]), topn=5, update_budget=0.05, out=lines.append)
        self.assertEqual(ret["skipped"], "none")
        self.assertIn("not needed", lines[0])


class TestScreenCli(CliCase):
    def fake_launch(self, **kw):
        return mock.patch.object(ondemand, "launch", fake_launch_fn([], on_fetch=lambda sid: self.add_acme_doc(), **kw))

    def test_flag_replaces_auto_fetch_and_updates(self):
        with self.ready(sec_email=True), self.fake_launch(), \
                mock.patch.object(ondemand_cli_mod(), "fetch_phase", side_effect=AssertionError("auto fetch")):
            code, out, err = self.main(["screen", "humanoid robots", "--reads", "1", "--sieve", "none",
                                        "--fetch-profile-only-topn", "10", "--cards", "0"])
        self.assertEqual(code, 0, err)
        self.assertIn("Fetching annual reports for the top 10", out)
        self.assertIn("Report updated", out)

    def test_zh_start_line(self):
        from test_screen import FakeKeywords
        self.kw = FakeKeywords({"idea_en": "Humanoid robots", "keywords": {"en": ["humanoid robot"]}, "model": "fake",
                                "cached": False})
        with self.ready(), self.fake_launch():
            code, out, err = self.main(["screen", "人形机器人", "--reads", "1", "--sieve", "none",
                                        "--fetch-profile-only-topn", "10", "--cards", "0"])
        self.assertEqual(code, 0, err)
        self.assertIn("补抓前 10 名的年报原文", out)
        self.assertNotIn("Fetching annual reports", out)

    def test_works_after_from_run(self):
        with self.ready(), mock.patch.object(ondemand, "plan", side_effect=AssertionError("no fetch")):
            code, _o, _e = self.main(["screen", "humanoid robots", "--fetch-docs", "off", "--reads", "1",
                                      "--cards", "0"])
        self.assertEqual(code, 0)
        with store.session(self.cfg, read_only=True) as con:
            rid = con.execute("SELECT run_id FROM screen_runs ORDER BY started_at DESC LIMIT 1").fetchone()[0]
        n_l1 = len([x for x in self.log if x.layer == "l1"])
        with self.ready(sec_email=True), self.fake_launch():
            code, out, err = self.main(["screen", "humanoid robots", "--from-run", rid, "--cards", "0",
                                        "--fetch-profile-only-topn", "10"])
        self.assertEqual(code, 0, err)
        self.assertIn("Report updated", out)
        self.assertEqual(len([x for x in self.log if x.layer == "l1"]), n_l1)       # no L1 call


def ondemand_cli_mod():
    from jevscreen import ondemand_cli
    return ondemand_cli


class TestEvalWiring(unittest.TestCase):
    def setUp(self):
        import tempfile
        from test_eval import idea
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.set = self.home / "ideas"
        self.set.mkdir()
        (self.set / "demo-idea.json").write_text(json.dumps(idea()), encoding="utf-8")
        from jevscreen import config
        self.cfg = config.Config(home=self.home / "home")
        with store.session(self.cfg):
            pass

    def base_ok(self, idea_text="demo idea"):
        """Every base run holds L1 answers for that idea (validate_from_runs reads it before any screen)."""
        return mock.patch.object(screen, "load_base_run", side_effect=lambda con, rid: {
            "run_id": rid, "idea": idea_text, "params": {"idea_en": "Demo idea"}, "l1": {"x": {}}})

    def run_eval(self, screen_fn, **kw):
        kw.setdefault("read_offset", 0)
        ns = argparse.Namespace(set=self.set, json=True, eval_command="run", budget_each=0.5, budget_total=1.0,
                                ideas=None, reads=None, **kw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = eval_cli._run(ns, self.cfg, screen_fn=screen_fn)
        return code, json.loads(buf.getvalue())

    def test_default_off_no_fetch(self):
        from test_eval import result
        with mock.patch.object(topn_fetch, "run", side_effect=AssertionError("no fetch by default")):
            code, out = self.run_eval(lambda cfg, text, **kw: result(["NYSE:A"]))
        self.assertEqual(code, 0)
        self.assertNotIn("topn_fetch", out["scores"][0])

    def fake_screen(self, calls, read_offset=0):
        from test_eval import result

        def fake(cfg, text, **kw):
            calls.append(kw)
            r = result(["NYSE:A", "NYSE:C"], run_id="scr-arm")
            r["layers"] = {"l1": {"from_run": kw.get("from_run")}}
            r["params"] = {"read_offset": kw.get("read_offset", 0)}
            r["cost_usd"] = 0.01
            return r
        return fake

    def test_from_run_uses_stored_l1_and_the_flag_runs_the_fetch(self):
        calls: list = []

        def fake_run(cfg, res, **kw):
            upd = dict(res, run_id="scr-upd", rows=res["rows"][:1])
            return {"result": upd, "update": {"run_id": "scr-upd", "cost_usd": 0.002}}
        with self.base_ok(), mock.patch.object(topn_fetch, "run", side_effect=fake_run) as fr:
            code, out = self.run_eval(self.fake_screen(calls), from_runs="demo-idea=scr-base",
                                      fetch_profile_only_topn=10)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["from_run"], "scr-base")
        self.assertEqual(fr.call_args.kwargs["topn"], 10)
        [s] = out["scores"]
        self.assertAlmostEqual(s["cost_usd"], 0.012, places=6)
        self.assertEqual(s["topn_fetch"]["base_run"], "scr-arm")

    def test_read_offset_reaches_the_update_pass(self):
        calls: list = []
        with self.base_ok(), mock.patch.object(topn_fetch, "run", return_value={"result": None}) as fr:
            code, _out = self.run_eval(self.fake_screen(calls), from_runs="demo-idea=scr-base",
                                       fetch_profile_only_topn=10, read_offset=3)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["read_offset"], 3)
        self.assertEqual(fr.call_args.kwargs["read_offset"], 3)

    def test_a_fetch_failure_keeps_the_paid_screen_and_the_eval_goes_on(self):
        calls: list = []
        with self.base_ok(), mock.patch.object(topn_fetch, "run", side_effect=OSError("cannot start the child")):
            code, out = self.run_eval(self.fake_screen(calls), from_runs="demo-idea=scr-base",
                                      deepen_official_topn=5)
        self.assertEqual(code, 0)
        [s] = out["scores"]
        self.assertEqual((s["status"], s["run_id"]), ("ok", "scr-arm"))
        self.assertIn("OSError", s["topn_fetch"]["error"])

    def test_every_selected_idea_needs_a_base_run_and_every_key_an_idea(self):
        from test_eval import idea
        from jevscreen import evalset
        (self.set / "other-idea.json").write_text(json.dumps(idea(id="other-idea", idea="other idea")),
                                                  encoding="utf-8")
        called: list = []
        for value, needle in (("demo-idea=scr-1", "no base run for other-idea"),
                              ("demo-idea=scr-1,other-idea=scr-2,demo-ide=scr-3", "not a selected idea: demo-ide")):
            with self.base_ok(), self.assertRaises(evalset.EvalError) as cm:
                self.run_eval(self.fake_screen(called), from_runs=value)
            self.assertIn(needle, str(cm.exception))
        self.assertEqual(called, [])                                        # nothing screened, nothing paid

    def test_base_run_without_l1_stops_the_eval_before_any_screen(self):
        from jevscreen import evalset
        with store.session(self.cfg) as con:
            con.execute("INSERT INTO screen_runs (run_id, idea, params_json, started_at, status, output_dir) VALUES "
                        "('scr-dry', 'demo idea', '{}', ?, 'ok', '/tmp/x')", [store.now_utc()])
        called: list = []
        with self.assertRaises(evalset.EvalError) as cm:
            self.run_eval(self.fake_screen(called), from_runs="demo-idea=scr-dry")
        self.assertIn("no stored L1 answers", str(cm.exception))
        self.assertEqual(called, [])                                        # stopped before any screen (no spend)
        self.assertTrue(list((self.home / "home" / "evals").glob("*/report.md")))

    def test_base_run_of_another_idea_is_refused(self):
        from jevscreen import evalset
        called: list = []
        with self.base_ok("a different idea"), self.assertRaises(evalset.EvalError) as cm:
            self.run_eval(self.fake_screen(called), from_runs="demo-idea=scr-base")
        self.assertIn("screened another idea", str(cm.exception))
        self.assertEqual(called, [])

    def test_l1_not_reused_is_an_error(self):
        from test_eval import result

        def fake_screen(cfg, text, **kw):
            r = result(["NYSE:A"])
            r["layers"] = {"l1": {"from_run": None}}
            return r
        from jevscreen import evalset
        with self.base_ok(), self.assertRaises(evalset.EvalError) as cm:   # the whole eval stops (L1 re-read)
            self.run_eval(fake_screen, from_runs="demo-idea=scr-base")
        self.assertIn("read L1 again", str(cm.exception))
        [scores] = [json.loads(p.read_text(encoding="utf-8"))["scores"]
                    for p in (self.home / "home" / "evals").glob("*/scores.json")]
        self.assertEqual(scores[0]["status"], "stopped")

    def test_store_drift_is_reported(self):
        calls: list = []
        with self.base_ok(), mock.patch.object(topn_fetch, "store_drift", return_value=["ck1", "ck2"]):
            code, out = self.run_eval(self.fake_screen(calls), from_runs="demo-idea=scr-base")
        self.assertEqual(code, 0)
        self.assertEqual(out["scores"][0]["store_drift"], {"base_run": "scr-base", "companies": ["ck1", "ck2"],
                                                           "n": 2})


class TestStoreDrift(Home):
    def test_changed_inputs_against_the_base_run(self):
        make_run(self.cfg, "scr-a")
        make_run(self.cfg, "scr-b", stored=("NASDAQ:DOCD", "NYSE:ACME"))
        self.assertEqual(topn_fetch.store_drift(self.cfg, "scr-a", "scr-b"), [CK["NYSE:ACME"]])
        self.assertEqual(topn_fetch.store_drift(self.cfg, "scr-a", "scr-a"), [])


if __name__ == "__main__":
    unittest.main()
