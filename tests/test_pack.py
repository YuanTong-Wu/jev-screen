"""Tests for jevscreen.pack (open data pack build / pull / fetch-open).

No network: GitHub, SEC and EDINET are fake clients. All rows are SYNTHETIC (fictional companies, codes E999xx /
9999 / 285A, CIKs 9000001..); the only fixture file used is the synthetic tests/fixtures/edinet_Edinetcode.zip.
"""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import cli, guard, pack, provenance, store  # noqa: E402
from jevscreen.config import Config  # noqa: E402
from jevscreen.http import Blocked, Response  # noqa: E402
from jevscreen.sources import edinet  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CODELIST_ZIP = (FIXTURES / "edinet_Edinetcode.zip").read_bytes()
NOW = dt.datetime(2026, 9, 27, 3, 0, 0)
FAKE_UA = "Test Person test.person@example.invalid"

TICKERS = {"fields": ["cik", "name", "ticker", "exchange"],
           "data": [[9000001, "Fictional Robotics Inc", "FROB", "Nasdaq"],
                    [9000002, "Imaginary Motors Corp", "IMOT", "NYSE"]]}
BUSINESS = ("当社グループは、精密減速機及びロボット用部品の製造販売を主な事業としております。"
            "主要な製品は産業用ロボット向けの波動歯車装置であり、国内外の顧客に供給しております。\n"
            "当社グループの事業は単一セグメントであります。")
POLICY = "\n\n" + edinet.POLICY_HEADING + "\nPOLICY-BLOCK-MUST-NOT-SHIP 当社は中期経営計画において成長を目指します。"


def codelist_zip_with_individuals() -> bytes:
    """The synthetic code list plus two SYNTHETIC individual (個人) filers; one even carries a 証券コード."""
    import zipfile
    with zipfile.ZipFile(io.BytesIO(CODELIST_ZIP)) as zf:
        name = zf.namelist()[0]
        text = zf.read(name).decode("cp932")
    text += ('"E99908","個人(組合発行者を除く)","","","","","架空　個人太郎","","カクウコジンタロウ","","","",""\n'
             '"E99909","個人(非居住者)(組合発行者を除く)","","","","","PRIVATE-PERSON-MUST-NOT-SHIP","Fictional Person",'
             '"","","","99950",""\n')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text.encode("cp932"))
    return buf.getvalue()


def make_cfg(tmp: str) -> Config:
    return Config(home=Path(tmp), fd_duckdb=Path(tmp) / "none.duckdb", user_agent="jevscreen-test/0",
                  min_interval_s=0.0).ensure()


def write_text(cfg: Config, rel: str, text: str) -> tuple[str, str, int]:
    p = cfg.home / "docs" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    p.write_bytes(data)
    return str(p), store.sha256(data), len(text)


def seed_source_store(cfg: Config) -> None:
    """A store holding every kind of data: gray (TradingView, FinanceDatabase), official-private (SEC Item 1,
    CNINFO, DART) and the two pack sources (SEC ticker list, EDINET code list + 事業の内容)."""
    with store.session(cfg) as con:
        tv = store.record_snapshot(con, source_id="tradingview_scanner", kind="scan", request=None, raw_path=None,
                                   raw_sha256=None, raw_bytes=None, rows=2, duration_s=None)
        store.upsert_many(con, "securities", ["security_id", "exchange", "symbol", "name", "company_key", "active",
                                              "is_primary", "tv_type", "tv_subtype"],
                          [["NASDAQ:FROB", "NASDAQ", "FROB", "GRAY-NAME-FROB", "sec:NASDAQ:FROB", True, True,
                            "stock", "common"]])
        prof = store.record_snapshot(con, source_id="tradingview_profile", kind="page", request=None, raw_path=None,
                                     raw_sha256=None, raw_bytes=None, rows=1, duration_s=None)
        store.upsert_many(con, "descriptions", ["security_id", "source_id", "text", "snapshot_id"],
                          [["NASDAQ:FROB", "tradingview_profile", "GRAY-TV-DESCRIPTION", prof]])
        # SEC ticker list (raw file) and Item 1 text (must never ship)
        raw_path, digest = store.save_raw(cfg, "sec_tickers", "company_tickers_exchange", json.dumps(TICKERS).encode())
        store.record_snapshot(con, source_id="sec_tickers", kind="company_tickers_exchange", request=None,
                              raw_path=raw_path, raw_sha256=digest, raw_bytes=1, rows=2, duration_s=None)
        sec_snap = store.record_snapshot(con, source_id="sec_filing_text", kind="filing_batch", request=None,
                                         raw_path=None, raw_sha256=None, raw_bytes=None, rows=1, duration_s=None)
        tp, sha, n = write_text(cfg, "sec/9000001/item1.txt", "SEC-ITEM1-VERBATIM-MUST-NOT-SHIP " * 100)
        cn_tp, cn_sha, cn_n = write_text(cfg, "cninfo/x.txt", "CNINFO-TEXT-MUST-NOT-SHIP " * 50)
        # EDINET code list (raw zip) and two filings of E99901 (newest ships) + one of E99902
        cl_path, cl_digest = store.save_raw(cfg, "edinet_yuho", "edinet_codelist", CODELIST_ZIP, suffix="zip")
        store.record_snapshot(con, source_id="edinet_yuho", kind="edinet_codelist", request=None, raw_path=cl_path,
                              raw_sha256=cl_digest, raw_bytes=len(CODELIST_ZIP), rows=7, duration_s=None)
        ed_snap = store.record_snapshot(con, source_id="edinet_yuho", kind="filing_batch", request=None,
                                        raw_path=None, raw_sha256=None, raw_bytes=None, rows=3, duration_s=None)
        e1_old = write_text(cfg, "edinet/E99901/S100OLD1-business.txt", "古い" + BUSINESS)
        e1_new = write_text(cfg, "edinet/E99901/S100NEW1-business.txt", BUSINESS + POLICY)
        e2 = write_text(cfg, "edinet/E99902/S100AAA2-business.txt", "短い")   # too short: skipped
        cols = ["doc_id", "security_id", "company_key", "source_id", "cik", "form", "section", "accession",
                "filing_date", "report_date", "url", "text_path", "text_sha256", "text_chars", "extractor",
                "snapshot_id"]
        store.upsert_many(con, "documents", cols, [
            ["sec_filing_text:9000001:0009000001-26-000001:item1", "NASDAQ:FROB", "sec:NASDAQ:FROB",
             "sec_filing_text", "9000001", "10-K", "item1", "0009000001-26-000001", dt.date(2026, 3, 1),
             dt.date(2025, 12, 31), "https://www.sec.gov/Archives/x.htm", tp, sha, n, "sec_edgar-v2", sec_snap],
            ["cninfo_annual_report:1:2:summary", None, None, "cninfo_annual_report", None, "年报摘要", "summary",
             "2", dt.date(2026, 4, 1), dt.date(2025, 12, 31), "http://static.cninfo.com.cn/x.pdf", cn_tp, cn_sha,
             cn_n, "cninfo-v1", sec_snap],
            ["edinet_yuho:E99901:S100OLD1:business", "TSE:9999", "isin:JP9999999991", "edinet_yuho", None,
             "有価証券報告書", "business", "S100OLD1", dt.date(2025, 6, 27), dt.date(2025, 3, 31),
             "https://api.edinet-fsa.go.jp/api/v2/documents/S100OLD1?type=5", *e1_old, "edinet-v2/csv", ed_snap],
            ["edinet_yuho:E99901:S100NEW1:business", "TSE:9999", "isin:JP9999999991", "edinet_yuho", None,
             "有価証券報告書", "business", "S100NEW1", dt.date(2026, 6, 26), dt.date(2026, 3, 31),
             "https://api.edinet-fsa.go.jp/api/v2/documents/S100NEW1?type=5", *e1_new, "edinet-v2/csv", ed_snap],
            ["edinet_yuho:E99902:S100AAA2:business", "TSE:285A", "isin:JP9999999992", "edinet_yuho", None,
             "有価証券報告書", "business", "S100AAA2", dt.date(2026, 6, 26), dt.date(2026, 3, 31),
             "https://api.edinet-fsa.go.jp/api/v2/documents/S100AAA2?type=5", *e2, "edinet-v2/csv", ed_snap],
        ])
        store.upsert_many(con, "descriptions", ["security_id", "source_id", "text", "snapshot_id"],
                          [["TSE:9999", "edinet_yuho", "LOCAL-EDINET-SHORT", ed_snap]])


def all_pack_text(folder: Path) -> str:
    out = []
    for p in sorted(folder.iterdir()):
        data = p.read_bytes()
        out.append((gzip.decompress(data) if p.name.endswith(".gz") else data).decode("utf-8"))
    return "\n".join(out)


class LicenceRulesTest(unittest.TestCase):
    def test_only_licence_clean_sources_are_allowlisted(self):
        self.assertEqual(set(pack.PACK_SOURCES), {"sec_tickers", "edinet_yuho"})
        for sid in pack.PACK_SOURCES:
            self.assertIs(pack.assert_redistributable(sid), pack.PACK_SOURCES[sid])
        for sid in ("tradingview_scanner", "tradingview_profile", "financedatabase_local", "cninfo_annual_report",
                    "dart_business_report", "sec_filing_text", "bse_whatever"):
            with self.assertRaises(pack.PackError):
                pack.assert_redistributable(sid)

    def test_gray_source_can_never_be_allowlisted(self):
        fake = pack.PackSource("tradingview_profile", ("x",), "l", "u", "a", "explicit allowlist: nope")
        with mock.patch.dict(pack.PACK_SOURCES, {"tradingview_profile": fake}):
            with self.assertRaisesRegex(pack.PackError, "gray-private"):
                pack.assert_redistributable("tradingview_profile")

    def test_private_tier_needs_explicit_allowlist(self):
        fake = pack.PackSource("dart_business_report", ("x",), "l", "u", "a", "seems fine")
        with mock.patch.dict(pack.PACK_SOURCES, {"dart_business_report": fake}):
            with self.assertRaisesRegex(pack.PackError, "explicit allowlist"):
                pack.assert_redistributable("dart_business_report")

    def test_every_registered_non_pack_source_has_an_exclusion_reason(self):
        for sid in provenance.SOURCES:
            if sid not in pack.PACK_SOURCES:
                self.assertIn(sid, pack.EXCLUDED_REASONS, sid)

    def test_split_business_block(self):
        self.assertEqual(pack.split_business_block(BUSINESS + POLICY), BUSINESS.strip())
        self.assertEqual(pack.split_business_block("  abc  "), "abc")


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        seed_source_store(self.cfg)
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_contents_and_manifest(self):
        res = pack.build(self.cfg, self.out, tag="pack-2026-09-27", now=NOW, secrets=())
        names = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(names, ["ATTRIBUTION.txt", "edinet_business.jsonl.gz", "edinet_codes.jsonl.gz",
                                 "manifest.json", "sec_tickers.jsonl.gz"])
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(manifest["format"], pack.FORMAT)
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["tag"], "pack-2026-09-27")
        self.assertEqual(set(manifest["sources"]), {"sec_tickers", "edinet_yuho"})
        self.assertIn("Public Data License 1.0", manifest["sources"]["edinet_yuho"]["licence"])
        self.assertIn("出典：EDINET", manifest["sources"]["edinet_yuho"]["attribution"])
        for sid in ("tradingview_scanner", "tradingview_profile", "financedatabase_local", "cninfo_annual_report",
                    "dart_business_report", "sec_filing_text"):
            self.assertIn(sid, manifest["excluded"])
        for f in manifest["files"]:
            data = (self.out / f["name"]).read_bytes()
            self.assertEqual(store.sha256(data), f["sha256"])
            self.assertEqual(len(data), f["bytes"])
        pack.validate_manifest(manifest)
        rows = {f["table"]: f["rows"] for f in manifest["files"] if f["table"]}
        self.assertEqual(rows, {"sec_tickers": 2, "edinet_codes": 6, "edinet_business": 1})   # G99907: no 証券コード
        self.assertEqual(res["summary"]["skipped_too_short"], 1)

    def test_nothing_private_or_gray_ships(self):
        pack.build(self.cfg, self.out, now=NOW, secrets=())
        text = all_pack_text(self.out)
        for needle in ("GRAY-", "SEC-ITEM1-VERBATIM", "CNINFO-TEXT", "POLICY-BLOCK", "LOCAL-EDINET-SHORT",
                       edinet.POLICY_HEADING, "NASDAQ:FROB", "TSE:9999", "isin:JP"):
            self.assertNotIn(needle, text)

    def test_individual_filers_never_ship(self):
        data = codelist_zip_with_individuals()
        with store.session(self.cfg) as con:
            path, digest = store.save_raw(self.cfg, "edinet_yuho", "edinet_codelist", data, suffix="zip")
            store.record_snapshot(con, source_id="edinet_yuho", kind="edinet_codelist", request=None, raw_path=path,
                                  raw_sha256=digest, raw_bytes=len(data), rows=9, duration_s=None,
                                  fetched_at=store.now_utc() + dt.timedelta(minutes=5))
        res = pack.build(self.cfg, self.out, now=NOW, secrets=())
        rows = list(pack.decode_jsonl_gz((self.out / "edinet_codes.jsonl.gz").read_bytes()))
        codes = {r["edinet_code"] for r in rows}
        self.assertNotIn("E99908", codes)
        self.assertNotIn("E99909", codes)
        self.assertFalse(any("個人" in str(r.get("filer_type")) for r in rows))
        self.assertTrue(all(r["sec_code"] for r in rows))
        self.assertEqual(res["summary"]["codelist_rows_dropped"], 3)    # two individuals + G99907 (no 証券コード)
        text = all_pack_text(self.out)
        self.assertNotIn("PRIVATE-PERSON", text)
        self.assertNotIn("個人太郎", text)

    def test_individual_filers_in_a_pack_are_not_imported(self):
        pack.build(self.cfg, self.out, now=NOW, secrets=())
        extra = [{"edinet_code": "E99909", "sec_code": "99950", "listed": None, "name": "PRIVATE-PERSON",
                  "name_en": None, "filer_type": "個人(非居住者)(組合発行者を除く)", "jcn": None}]
        rows = list(pack.decode_jsonl_gz((self.out / "edinet_codes.jsonl.gz").read_bytes())) + extra
        data, n = pack.encode_jsonl_gz(rows)
        (self.out / "edinet_codes.jsonl.gz").write_bytes(data)
        m = json.loads((self.out / "manifest.json").read_text())
        for f in m["files"]:
            if f["name"] == "edinet_codes.jsonl.gz":
                f.update(rows=n, bytes=len(data), sha256=store.sha256(data))
        (self.out / "manifest.json").write_text(json.dumps(m))
        with tempfile.TemporaryDirectory() as t:
            dst = make_cfg(t)
            with store.session(dst):
                pass
            counts = pack.import_pack(dst, self.out)
        self.assertEqual(counts["codelist_rows_dropped"], 1)
        self.assertEqual(counts["edinet_codes"], 6)

    def test_newest_business_text_only(self):
        pack.build(self.cfg, self.out, now=NOW, secrets=())
        rows = list(pack.decode_jsonl_gz((self.out / "edinet_business.jsonl.gz").read_bytes()))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["edinet_code"], r["doc_id"], r["sec_code"]), ("E99901", "S100NEW1", "99990"))
        self.assertEqual(r["text"], BUSINESS.strip())
        self.assertEqual(r["text_sha256"], store.sha256(BUSINESS.strip().encode()))
        self.assertEqual(r["filing_date"], "2026-06-26")
        self.assertEqual(r["filer_name"], "テスト精機株式会社")

    def test_source_url_is_the_public_viewer_not_the_key_gated_api(self):
        pack.build(self.cfg, self.out, now=NOW, secrets=())
        rows = list(pack.decode_jsonl_gz((self.out / "edinet_business.jsonl.gz").read_bytes()))
        self.assertEqual(rows[0]["source_url"], pack.EDINET_VIEWER_URL.format(doc_id="S100NEW1"))
        self.assertNotIn("api.edinet-fsa.go.jp", all_pack_text(self.out))
        # an older pack with API URLs is rewritten on import
        old = pack._clean_business_row({"edinet_code": "E99901", "doc_id": "S100NEW1", "text": BUSINESS,
                                        "source_url": "https://api.edinet-fsa.go.jp/api/v2/documents/S100NEW1?type=5"})
        self.assertEqual(old["url"], pack.EDINET_VIEWER_URL.format(doc_id="S100NEW1"))

    def test_attribution_carries_the_third_party_rights_caveat(self):
        pack.build(self.cfg, self.out, now=NOW, secrets=())
        att = (self.out / "ATTRIBUTION.txt").read_text(encoding="utf-8")
        self.assertIn("第三者", att)
        self.assertIn("third party", att)
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertIn("third party", manifest["sources"]["edinet_yuho"]["caveat"])

    def test_deterministic(self):
        pack.build(self.cfg, self.out, now=NOW, secrets=())
        first = {p.name: p.read_bytes() for p in self.out.iterdir()}
        out2 = Path(self.tmp.name) / "out2"
        pack.build(self.cfg, out2, now=NOW, secrets=())
        self.assertEqual(first, {p.name: p.read_bytes() for p in out2.iterdir()})

    def business_rows(self):
        return list(pack.decode_jsonl_gz((self.out / "edinet_business.jsonl.gz").read_bytes()))

    def test_tampered_text_file_falls_back_to_the_older_filing(self):
        p = self.cfg.home / "docs" / "edinet" / "E99901" / "S100NEW1-business.txt"
        p.write_text("tampered " * 30, encoding="utf-8")
        res = pack.build(self.cfg, self.out, now=NOW, secrets=())
        self.assertEqual(res["summary"]["skipped_sha_mismatch"], 1)
        self.assertEqual([r["doc_id"] for r in self.business_rows()], ["S100OLD1"])

    def test_too_short_or_missing_newest_falls_back_to_the_older_filing(self):
        p = self.cfg.home / "docs" / "edinet" / "E99901" / "S100NEW1-business.txt"
        p.write_text("短すぎる本文です。", encoding="utf-8")
        with store.session(self.cfg) as con:
            con.execute("UPDATE documents SET text_sha256 = ? WHERE doc_id = 'edinet_yuho:E99901:S100NEW1:business'",
                        [store.sha256(p.read_bytes())])
        res = pack.build(self.cfg, self.out, now=NOW, secrets=())
        self.assertEqual(res["summary"]["skipped_too_short"], 2)      # E99901's newest + E99902's only filing
        self.assertEqual([r["doc_id"] for r in self.business_rows()], ["S100OLD1"])
        p.unlink()
        res = pack.build(self.cfg, self.out, now=NOW, secrets=())
        self.assertEqual(res["summary"]["skipped_missing_file"], 1)
        self.assertEqual([r["doc_id"] for r in self.business_rows()], ["S100OLD1"])

    def test_delisted_filer_does_not_ship(self):
        tp, sha, n = write_text(self.cfg, "edinet/E99905/S100DDD5-business.txt", "旧上場" + BUSINESS)
        with store.session(self.cfg) as con:
            store.upsert_many(con, "documents", ["doc_id", "source_id", "form", "filing_date", "text_path",
                                                 "text_sha256", "extractor"],
                              [["edinet_yuho:E99905:S100DDD5:business", "edinet_yuho", "有価証券報告書",
                                dt.date(2026, 6, 26), tp, sha, "edinet-v2/csv"]])
        res = pack.build(self.cfg, self.out, now=NOW, secrets=())
        self.assertEqual(res["summary"]["skipped_delisted"], 1)
        self.assertEqual([r["edinet_code"] for r in self.business_rows()], ["E99901"])

    def test_rebuild_removes_stale_tables(self):
        pack.build(self.cfg, self.out, now=NOW, secrets=())
        with store.session(self.cfg) as con:
            con.execute("DELETE FROM documents WHERE source_id = 'edinet_yuho'")
        pack.build(self.cfg, self.out, now=NOW, secrets=())
        self.assertFalse((self.out / "edinet_business.jsonl.gz").exists())

    def test_default_secrets_include_the_email_inside_the_user_agent(self):
        with mock.patch.object(Config, "sec_user_agent", return_value=FAKE_UA), \
                mock.patch.object(Config, "edinet_api_key", return_value=None), \
                mock.patch.object(Config, "opendart_api_key", return_value=None), \
                mock.patch.object(Config, "openrouter_key", return_value=None):
            self.assertIn("test.person@example.invalid", pack.default_secrets(self.cfg))

    def test_secret_in_manifest_aborts(self):
        with mock.patch.object(pack, "_attribution_text", lambda m: "attribution"), \
                self.assertRaisesRegex(pack.PackError, "manifest.json"):
            pack.build(self.cfg, self.out, tag="pack-SECRETTAG", now=NOW, secrets=["SECRETTAG"])

    def test_secret_in_output_aborts(self):
        with self.assertRaisesRegex(pack.PackError, "secret"):
            pack.build(self.cfg, self.out, now=NOW, secrets=["Fictional Robotics"])
        self.assertFalse((self.out / "manifest.json").exists())

    def test_empty_store_builds_attribution_only(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(t)
            with store.session(cfg):
                pass
            res = pack.build(cfg, Path(t) / "o", now=NOW, secrets=())
            self.assertEqual(res["summary"]["rows"], {})

    def test_cli_build(self):
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": self.tmp.name}, clear=False), \
                mock.patch.object(pack, "default_secrets", lambda cfg: []), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = cli.main(["pack", "build", "--out", str(self.out), "--tag", "pack-test"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["tag"], "pack-test")
        self.assertTrue((self.out / "manifest.json").exists())


# ---------------------------------------------------------------------------------------------- fake GitHub


class FakeGitHub:
    """http.Client stand-in serving a release list, release assets via a redirect to objects.githubusercontent."""

    def __init__(self, folder: Path, tag: str = "pack-2026-09-27", repo: str = "owner/jev-screen"):
        self.repo, self.tag = repo, tag
        self.files = {p.name: p.read_bytes() for p in folder.iterdir()}
        self.calls: list[str] = []
        self.overrides: dict[str, Any] = {}

    def release(self) -> dict:
        return {"tag_name": self.tag, "draft": False, "prerelease": False, "published_at": "2026-09-27T03:00:00Z",
                "assets": [{"name": n, "size": len(b), "digest": "sha256:" + store.sha256(b),
                            "browser_download_url": f"https://github.com/{self.repo}/releases/download/{self.tag}/{n}"}
                           for n, b in self.files.items()]}

    def request(self, method, url, headers=None, **kw):
        self.calls.append(url)
        if url in self.overrides:
            o = self.overrides[url]
            if isinstance(o, Exception):
                raise o
            return o
        if url == f"{pack.GITHUB_API}/repos/{self.repo}/releases?per_page=100":
            other = {"tag_name": "v0.1.0", "draft": False, "prerelease": False, "assets": []}
            old = {**self.release(), "tag_name": "pack-2026-09-01"}
            return Response(url, 200, json.dumps([other, self.release(), old]).encode(), {}, 0.0)
        if url == f"{pack.GITHUB_API}/repos/{self.repo}/releases/tags/{self.tag}":
            return Response(url, 200, json.dumps(self.release()).encode(), {}, 0.0)
        prefix = f"https://github.com/{self.repo}/releases/download/{self.tag}/"
        if url.startswith(prefix):
            name = url[len(prefix):]
            return Response(url, 302, b"", {"Location": f"https://objects.githubusercontent.com/x/{name}?sig=1"}, 0.0)
        if url.startswith("https://objects.githubusercontent.com/x/"):
            name = url.split("/x/", 1)[1].split("?", 1)[0]
            return Response(url, 200, self.files[name], {}, 0.0)
        return Response(url, 404, b"", {}, 0.0)


def seed_target_store(cfg: Config) -> None:
    """The user's store: a universe with US and JP lines (no official data yet)."""
    with store.session(cfg) as con:
        snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="scan", request=None, raw_path=None,
                                     raw_sha256=None, raw_bytes=None, rows=3, duration_s=None)
        cols = ["security_id", "exchange", "symbol", "name", "company_key", "active", "is_primary", "tv_type",
                "tv_subtype", "first_seen_snapshot"]
        store.upsert_many(con, "securities", cols, [
            ["NASDAQ:FROB", "NASDAQ", "FROB", "Fictional Robotics", "isin:US9000000011", True, True, "stock", "common",
             snap],
            ["TSE:9999", "TSE", "9999", "Test Seiki", "isin:JP9999999991", True, True, "stock", "common", snap],
            ["TSE:285A", "TSE", "285A", "Sample AI", "isin:JP9999999992", True, True, "stock", "common", snap]])
        store.upsert_many(con, "market_daily", ["security_id", "as_of", "market_cap_usd", "snapshot_id"], [
            ["NASDAQ:FROB", dt.date(2026, 9, 26), 5e9, snap], ["TSE:9999", dt.date(2026, 9, 26), 3e9, snap],
            ["TSE:285A", dt.date(2026, 9, 26), 1e9, snap]])


class PullTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = make_cfg(os.path.join(self.tmp.name, "src"))
        seed_source_store(src)
        self.packdir = Path(self.tmp.name) / "built"
        pack.build(src, self.packdir, tag="pack-2026-09-27", now=NOW, secrets=())
        self.cfg = make_cfg(os.path.join(self.tmp.name, "dst"))
        seed_target_store(self.cfg)
        self.gh = FakeGitHub(self.packdir)

    def tearDown(self):
        self.tmp.cleanup()

    def pull(self, **kw):
        fetcher = pack.GitHubFetcher(self.cfg, client=self.gh)
        return pack.pull(self.cfg, repo="owner/jev-screen", fetcher=fetcher, **kw)

    def q(self, sql, params=()):
        with store.session(self.cfg, read_only=True) as con:
            return con.execute(sql, list(params)).fetchall()

    def test_pull_imports_everything_with_pack_provenance(self):
        res = self.pull()
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["tag"], "pack-2026-09-27")
        imp = res["imported"]
        self.assertEqual(imp["sec_cik_identifiers_added"], 1)
        self.assertEqual(imp["edinet_code_identifiers_added"], 2)
        self.assertEqual(imp["edinet_documents_added"], 1)
        self.assertEqual(imp["edinet_descriptions_written"], 1)
        self.assertEqual(self.q("SELECT id_value FROM identifiers WHERE security_id='NASDAQ:FROB'"), [("9000001",)])
        doc = self.q("SELECT d.doc_id, d.security_id, d.cik, d.extractor, d.text_path, d.text_sha256, n.kind, n.note "
                     "FROM documents d JOIN snapshots n USING (snapshot_id)")
        self.assertEqual(len(doc), 1)
        doc_id, sid, cik, extractor, tp, sha, kind, note = doc[0]
        self.assertEqual((doc_id, sid, cik, extractor), ("edinet_yuho:E99901:S100NEW1:business", "TSE:9999",
                                                         "E99901", pack.PACK_EXTRACTOR))
        self.assertEqual((kind, note), ("pack:edinet_business", "pack-imported tag=pack-2026-09-27"))
        self.assertEqual(Path(tp).read_text(encoding="utf-8"), BUSINESS.strip())
        self.assertEqual(store.sha256(Path(tp).read_bytes()), sha)
        folder = self.cfg.raw_dir / "open_pack" / f"pack-2026-09-27-{res['manifest_sha256'][:12]}"
        self.assertTrue((folder / "manifest.json").exists())
        runs = self.q("SELECT command, status FROM runs")
        self.assertEqual(runs, [("pack pull", "ok")])
        # the release list's non-pack and older pack releases were ignored
        self.assertFalse(any("pack-2026-09-01" in c for c in self.gh.calls))

    def test_screen_finds_imported_text_through_identifier(self):
        from jevscreen import screen
        with store.session(self.cfg) as con:
            con.execute("DELETE FROM identifiers")
        self.pull()
        with store.session(self.cfg, read_only=True) as con:
            docs = screen.load_documents(con, min_mcap_usd=0)
        self.assertIn("isin:JP9999999991", docs)
        self.assertEqual(docs["isin:JP9999999991"]["source_id"], "edinet_yuho")

    def test_second_pull_is_a_noop(self):
        self.pull()
        n_calls = len(self.gh.calls)
        res = self.pull()
        self.assertEqual(res["status"], "already_imported")
        self.assertEqual(len(self.gh.calls) - n_calls, 3)   # release list + manifest (redirect + object)
        forced = self.pull(force=True)
        self.assertEqual(forced["imported"]["edinet_documents_unchanged"], 1)
        self.assertEqual(forced["imported"]["edinet_documents_added"], 0)
        self.assertEqual(forced["imported"]["sec_cik_identifiers_added"], 0)
        self.assertEqual(self.q("SELECT count(*) FROM documents")[0][0], 1)

    def test_local_documents_and_mappings_are_never_overwritten(self):
        tp = self.cfg.home / "docs" / "edinet" / "E99901" / "S100NEW1-business.txt"
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text("LOCAL RICHER TEXT", encoding="utf-8")
        with store.session(self.cfg) as con:
            store.upsert_many(con, "documents", ["doc_id", "source_id", "text_path", "extractor"],
                              [["edinet_yuho:E99901:S100NEW1:business", "edinet_yuho", str(tp), "edinet-v2/csv"]])
            store.upsert_many(con, "identifiers", ["security_id", "id_type", "id_value", "method"],
                              [["TSE:9999", "edinet_code", "E99901", "sec_code"]])
            store.upsert_many(con, "descriptions", ["security_id", "source_id", "text"],
                              [["TSE:9999", "edinet_yuho", "LOCAL SHORT"]])
        res = self.pull()
        self.assertEqual(res["imported"]["edinet_documents_kept_local"], 1)
        self.assertEqual(tp.read_text(encoding="utf-8"), "LOCAL RICHER TEXT")
        self.assertEqual(self.q("SELECT extractor FROM documents"), [("edinet-v2/csv",)])
        self.assertEqual(self.q("SELECT text FROM descriptions WHERE security_id='TSE:9999'"), [("LOCAL SHORT",)])
        self.assertEqual(self.q("SELECT method FROM identifiers WHERE security_id='TSE:9999'"), [("sec_code",)])

    def test_unmapped_document_is_linked_on_a_later_pull(self):
        with store.session(self.cfg) as con:
            con.execute("DELETE FROM securities WHERE exchange = 'TSE'")
        res = self.pull()
        self.assertEqual(res["imported"]["edinet_documents_unmapped"], 1)
        self.assertEqual(self.q("SELECT security_id, cik FROM documents"), [(None, "E99901")])
        seed_target_store(self.cfg)
        res = self.pull(force=True)
        self.assertEqual(res["imported"]["edinet_documents_updated"], 1)
        self.assertEqual(self.q("SELECT security_id FROM documents"), [("TSE:9999",)])

    def test_pull_before_universe_then_plain_pull_links_the_text(self):
        """The novice order: pull on a fresh home, build the universe, pull again (no --force)."""
        from jevscreen import screen
        fresh = make_cfg(os.path.join(self.tmp.name, "fresh"))
        fetcher = pack.GitHubFetcher(fresh, client=self.gh)
        res = pack.pull(fresh, repo="owner/jev-screen", fetcher=fetcher)
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["imported"]["edinet_documents_unmapped"], 1)
        seed_target_store(fresh)
        res = pack.pull(fresh, repo="owner/jev-screen", fetcher=pack.GitHubFetcher(fresh, client=self.gh))
        self.assertEqual(res["status"], "already_imported", res)
        self.assertEqual(res["relinked"]["edinet_code_identifiers_added"], 2)
        self.assertEqual(res["relinked"]["sec_cik_identifiers_added"], 1)
        self.assertEqual(res["relinked"]["edinet_documents_linked"], 1)
        self.assertEqual(res["relinked"]["edinet_descriptions_written"], 1)
        with store.session(fresh, read_only=True) as con:
            docs = screen.load_documents(con, min_mcap_usd=0)
            desc = con.execute("SELECT text FROM descriptions WHERE security_id = 'TSE:9999'").fetchall()
        self.assertIn("isin:JP9999999991", docs)
        self.assertEqual(len(desc), 1)
        # a third pull has nothing left to link
        res = pack.pull(fresh, repo="owner/jev-screen", fetcher=pack.GitHubFetcher(fresh, client=self.gh))
        self.assertEqual(res["relinked"]["edinet_documents_linked"], 0)

    def test_relink_never_overwrites_local_descriptions_or_mappings(self):
        with store.session(self.cfg) as con:
            con.execute("DELETE FROM securities WHERE exchange = 'TSE'")
        self.pull()
        seed_target_store(self.cfg)
        with store.session(self.cfg) as con:
            store.upsert_many(con, "identifiers", ["security_id", "id_type", "id_value", "method"],
                              [["TSE:9999", "edinet_code", "E99901", "sec_code"]])
            store.upsert_many(con, "descriptions", ["security_id", "source_id", "text"],
                              [["TSE:9999", "edinet_yuho", "LOCAL SHORT"]])
        res = self.pull()
        self.assertEqual(res["relinked"]["edinet_documents_linked"], 1)
        self.assertEqual(res["relinked"]["edinet_descriptions_written"], 0)
        self.assertEqual(self.q("SELECT text FROM descriptions WHERE security_id='TSE:9999'"), [("LOCAL SHORT",)])
        self.assertEqual(self.q("SELECT method FROM identifiers WHERE security_id='TSE:9999'"), [("sec_code",)])

    def test_sha_mismatch_imports_nothing(self):
        good = self.gh.files["edinet_business.jsonl.gz"]
        # same size, one byte flipped; GitHub's asset digest matches the tampered file, the manifest does not
        self.gh.files["edinet_business.jsonl.gz"] = good[:-5] + bytes([good[-5] ^ 1]) + good[-4:]
        res = self.pull()
        self.assertEqual(res["status"], "error")
        self.assertIn("sha256 mismatch", res["note"])
        self.assertEqual(self.q("SELECT count(*) FROM documents")[0][0], 0)
        self.assertEqual(self.q("SELECT count(*) FROM snapshots WHERE kind LIKE 'pack:%'")[0][0], 0)

    def test_size_mismatch_imports_nothing(self):
        self.gh.files["sec_tickers.jsonl.gz"] = pack.encode_jsonl_gz([{"x": 1}])[0]
        res = self.pull()
        self.assertEqual(res["status"], "error")
        self.assertIn("size", res["note"])
        self.assertEqual(self.q("SELECT count(*) FROM identifiers")[0][0], 0)

    def test_corrupt_gzip_with_matching_sha_imports_nothing(self):
        bad = b"not a gzip stream"
        self.gh.files["edinet_business.jsonl.gz"] = bad
        m = json.loads(self.gh.files["manifest.json"])
        for f in m["files"]:
            if f["name"] == "edinet_business.jsonl.gz":
                f.update(bytes=len(bad), sha256=store.sha256(bad))
        self.gh.files["manifest.json"] = json.dumps(m).encode()
        res = self.pull()
        self.assertEqual(res["status"], "error")
        self.assertIn("import", res["note"])
        self.assertEqual(self.q("SELECT count(*) FROM snapshots WHERE kind LIKE 'pack:%'")[0][0], 0)
        self.assertEqual(self.q("SELECT count(*) FROM identifiers")[0][0], 0)

    def test_failure_mid_import_rolls_back(self):
        with mock.patch.object(pack, "_import_business", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                pack.import_pack(self.cfg, self.packdir)
        self.assertEqual(self.q("SELECT count(*) FROM snapshots WHERE kind LIKE 'pack:%'")[0][0], 0)
        self.assertEqual(self.q("SELECT count(*) FROM identifiers")[0][0], 0)
        self.assertEqual(self.pull()["status"], "ok")      # nothing marked imported: a retry imports everything

    def test_rolled_back_import_leaves_text_files_untouched(self):
        self.pull()
        tp, sha = self.q("SELECT text_path, text_sha256 FROM documents")[0]
        src = make_cfg(os.path.join(self.tmp.name, "src"))
        changed = BUSINESS + "\n当社グループは新たに医療機器向けの精密部品事業を開始しております。"
        rel = "edinet/E99901/S100NEW1-business.txt"
        path, new_sha, n = write_text(src, rel, changed + POLICY)
        with store.session(src) as con:
            con.execute("UPDATE documents SET text_sha256 = ?, text_chars = ? WHERE doc_id = ?",
                        [new_sha, n, "edinet_yuho:E99901:S100NEW1:business"])
        pack_b = Path(self.tmp.name) / "built-b"
        pack.build(src, pack_b, tag="pack-2026-09-28", now=NOW, secrets=())
        real = store.upsert_many

        def failing(con, table, cols, rows):
            if table == "documents":
                raise RuntimeError("boom")
            return real(con, table, cols, rows)
        with mock.patch.object(store, "upsert_many", failing), self.assertRaises(RuntimeError):
            pack.import_pack(self.cfg, pack_b)
        self.assertEqual(store.sha256(Path(tp).read_bytes()), sha)          # the file still matches its row
        self.assertEqual(self.q("SELECT text_sha256 FROM documents"), [(sha,)])
        self.assertEqual([p.name for p in Path(tp).parent.iterdir() if p.name.endswith(".tmp")], [])
        counts = pack.import_pack(self.cfg, pack_b)                          # a retry lands both together
        self.assertEqual(counts["edinet_documents_updated"], 1)
        row_sha = self.q("SELECT text_sha256 FROM documents")[0][0]
        self.assertEqual(store.sha256(Path(tp).read_bytes()), row_sha)
        self.assertEqual(Path(tp).read_text(encoding="utf-8"), changed.strip())

    def test_blocked_stops_at_once(self):
        url = f"https://github.com/owner/jev-screen/releases/download/pack-2026-09-27/manifest.json"
        self.gh.overrides[url] = Blocked(url, 429, "http_429")
        res = self.pull()
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(self.gh.calls[-1], url)
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, pack.COMMAND_PULL))
        self.assertEqual(self.q("SELECT count(*) FROM documents")[0][0], 0)
        # cooldown: the next pull sends nothing; --after-block runs and a clean run clears the marker
        n = len(self.gh.calls)
        res = self.pull()
        self.assertEqual(res["status"], "blocked")
        self.assertIn("cooldown", res["note"])
        self.assertEqual(len(self.gh.calls), n)
        del self.gh.overrides[url]
        self.assertEqual(self.pull(after_block=True)["status"], "ok")
        self.assertIsNone(guard.recent_block_marker(self.cfg, pack.COMMAND_PULL))

    def test_redirect_to_foreign_host_is_refused(self):
        url = f"https://github.com/owner/jev-screen/releases/download/pack-2026-09-27/manifest.json"
        self.gh.overrides[url] = Response(url, 302, b"", {"Location": "https://evil.example/manifest.json"}, 0.0)
        res = self.pull()
        self.assertEqual(res["status"], "error")
        self.assertIn("non-GitHub", res["note"])
        self.assertFalse(any("evil" in c for c in self.gh.calls))

    def test_manifest_listing_a_private_source_is_refused(self):
        m = json.loads(self.gh.files["manifest.json"])
        m["files"][0]["source_id"] = "cninfo_annual_report"
        self.gh.files["manifest.json"] = json.dumps(m).encode()
        res = self.pull()
        self.assertEqual(res["status"], "error")
        self.assertEqual(self.q("SELECT count(*) FROM snapshots WHERE kind LIKE 'pack:%'")[0][0], 0)

    def test_exact_tag_and_missing_repo(self):
        res = self.pull(tag="pack-2026-09-27")
        self.assertEqual(res["status"], "ok")
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(pack, "DEFAULT_PACK_REPO", ""):
            os.environ.pop("JEVSCREEN_PACK_REPO", None)
            res = pack.pull(self.cfg, fetcher=pack.GitHubFetcher(self.cfg, client=self.gh))
        self.assertEqual(res["status"], "error")
        self.assertIn("no pack repository", res["note"])

    def test_builtin_default_repository(self):
        self.assertRegex(pack.DEFAULT_PACK_REPO, r"^[A-Za-z0-9_.-]+/jev-screen$")

    def test_newer_format_is_refused(self):
        m = json.loads(self.gh.files["manifest.json"])
        m["format_version"] = pack.FORMAT_VERSION + 1
        with self.assertRaisesRegex(pack.PackError, "upgrade"):
            pack.validate_manifest(m)

    def test_bad_rows_are_rejected_not_imported(self):
        row = pack._clean_business_row({"edinet_code": "E99901", "doc_id": "S100NEW1", "text": BUSINESS,
                                        "source_url": "https://evil.example/x"})
        self.assertIsNone(row["url"])
        bad = [{"edinet_code": "../../etc", "doc_id": "S100NEW1", "text": BUSINESS},
               {"edinet_code": "E99901", "doc_id": "../x", "text": BUSINESS},
               {"edinet_code": "E99902", "doc_id": "S100BBB2", "text": "short"}]
        self.assertEqual([pack._clean_business_row(r) for r in bad], [None, None, None])


class FetchOpenTest(unittest.TestCase):
    class Fake:
        def __init__(self, body: bytes, status: int = 200, exc: Exception | None = None):
            self.body, self.status, self.exc, self.calls = body, status, exc, []

        def request(self, method, url, **kw):
            self.calls.append((url, kw.get("headers")))
            if self.exc:
                raise self.exc
            return Response(url, self.status, self.body, {}, 0.0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_lines_then_edinet_mapping_works(self):
        sec = self.Fake(json.dumps(TICKERS).encode())
        ed = self.Fake(CODELIST_ZIP)
        res = pack.fetch_open(self.cfg, seed_lines=True, sec_client=sec, edinet_client=ed)
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["sec_tickers"], {"rows": 2})
        self.assertEqual(res["edinet_codelist"]["rows"], 7)
        # listed filers with a code: 99990, 285A0, 99980 (two filers, one line), 99960; 99970 is delisted
        self.assertEqual(res["edinet_codelist"]["lines_seeded"], 4)
        with store.session(self.cfg, read_only=True) as con:
            lines = edinet.load_lines(con, edinet.JP_VENUES)
        self.assertEqual(sorted(ln["security_id"] for ln in lines), ["TSE:285A", "TSE:9996", "TSE:9998", "TSE:9999"])
        entries = edinet.parse_codelist(CODELIST_ZIP)
        mapped = dict((s, c) for s, c, _ in edinet.map_securities_to_edinet(lines, entries))
        self.assertEqual(mapped["TSE:9999"], "E99901")
        self.assertEqual(mapped["TSE:285A"], "E99902")

    def test_reseed_deactivates_lines_that_left_the_code_list(self):
        pack.fetch_open(self.cfg, seed_lines=True, sec_client=self.Fake(json.dumps(TICKERS).encode()),
                        edinet_client=self.Fake(CODELIST_ZIP))
        entries = [e for e in edinet.parse_codelist(CODELIST_ZIP) if e["edinet_code"] != "E99906"]
        with store.session(self.cfg) as con:
            pack.seed_jp_lines(con, entries, "snap")
            active = dict(con.execute("SELECT security_id, active FROM securities").fetchall())
        self.assertFalse(active["TSE:9996"])
        self.assertTrue(active["TSE:9999"])

    def test_seed_refused_in_a_store_with_tradingview_data(self):
        seed_target_store(self.cfg)
        res = pack.fetch_open(self.cfg, seed_lines=True, sec_client=self.Fake(json.dumps(TICKERS).encode()),
                              edinet_client=self.Fake(CODELIST_ZIP))
        self.assertEqual(res["status"], "error")
        self.assertIn("TradingView", res["note"])

    def test_sec_skipped_cleanly_without_user_agent(self):
        with mock.patch.object(Config, "sec_user_agent", return_value=None):
            res = pack.fetch_open(self.cfg, edinet_client=self.Fake(CODELIST_ZIP))
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["sec_tickers"].startswith("skipped"))

    def test_blocked_stops_and_ua_never_recorded(self):
        with mock.patch.object(Config, "sec_user_agent", return_value=FAKE_UA):
            with mock.patch("jevscreen.http.Client.request",
                            side_effect=Blocked("https://www.sec.gov/files/company_tickers_exchange.json", 403,
                                                "http_403")):
                res = pack.fetch_open(self.cfg)
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["requests"], 1)
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, pack.COMMAND_FETCH))
        ed = self.Fake(CODELIST_ZIP)
        again = pack.fetch_open(self.cfg, sec_client=self.Fake(b"{}"), edinet_client=ed)
        self.assertEqual((again["status"], ed.calls), ("blocked", []))
        with store.session(self.cfg, read_only=True) as con:
            dump = json.dumps(con.execute("SELECT * FROM runs").fetchall(), default=str)
            dump += json.dumps(con.execute("SELECT * FROM snapshots").fetchall(), default=str)
        self.assertNotIn("example.invalid", dump)

    def test_sec_network_error_does_not_cost_edinet(self):
        with mock.patch.object(Config, "sec_user_agent", return_value=FAKE_UA):
            res = pack.fetch_open(self.cfg, sec_client=self.Fake(b"", exc=OSError("boom")),
                                  edinet_client=self.Fake(CODELIST_ZIP))
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["edinet_codelist"]["rows"], 7)


class CliTest(unittest.TestCase):
    """`jevscreen pack pull` / `pack fetch-open` through cli.main: exit codes 0 / 1 / 2 / 4 and the stderr notes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = make_cfg(os.path.join(self.tmp.name, "src"))
        seed_source_store(src)
        packdir = Path(self.tmp.name) / "built"
        pack.build(src, packdir, tag="pack-2026-09-27", now=NOW, secrets=())
        self.home = os.path.join(self.tmp.name, "home")
        self.cfg = make_cfg(self.home)
        self.gh = FakeGitHub(packdir)
        real_fetcher = pack.GitHubFetcher
        self.patches = [
            mock.patch.dict(os.environ, {"JEVSCREEN_HOME": self.home}, clear=False),
            mock.patch.object(pack, "GitHubFetcher", lambda cfg, client=None: real_fetcher(cfg, client=self.gh)),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop("JEVSCREEN_PACK_REPO", None)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv) -> tuple[int, dict | None, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = cli.main(list(argv))
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else None), err.getvalue()

    def test_pull_exit_codes(self):
        with mock.patch.object(pack, "DEFAULT_PACK_REPO", ""):
            code, res, err = self.run_cli("pack", "pull")                      # no repository configured
        self.assertEqual((code, res["status"]), (1, "error"))
        self.assertIn("--repo OWNER/NAME", err)
        self.assertEqual(self.gh.calls, [])
        code, res, err = self.run_cli("pack", "pull", "--repo", "owner/jev-screen")
        self.assertEqual((code, res["status"]), (0, "ok"))
        self.assertIn("have no line yet", err)                                 # empty home: the text is unmapped
        code, res, _ = self.run_cli("pack", "pull", "--repo", "owner/jev-screen")
        self.assertEqual((code, res["status"]), (0, "already_imported"))
        with mock.patch.dict(os.environ, {"JEVSCREEN_PACK_REPO": "owner/jev-screen"}):
            self.assertEqual(self.run_cli("pack", "pull", "--force")[0], 0)

    def test_pull_blocked_is_2_and_busy_is_4(self):
        url = "https://github.com/owner/jev-screen/releases/download/pack-2026-09-27/manifest.json"
        self.gh.overrides[url] = Blocked(url, 429, "http_429")
        code, res, err = self.run_cli("pack", "pull", "--repo", "owner/jev-screen")
        self.assertEqual((code, res["status"]), (2, "blocked"))
        self.assertIn("error: pack pull", err)
        with guard.budget_lock(self.cfg, "api.github.com"):
            code, res, err = self.run_cli("pack", "pull", "--repo", "owner/jev-screen", "--after-block")
        self.assertEqual((code, res), (4, None))

    def test_fetch_open_exit_codes(self):
        real = pack.fetch_open
        fake = FetchOpenTest.Fake

        def with_clients(sec, ed):
            return mock.patch.object(pack, "fetch_open", lambda cfg, **kw: real(cfg, sec_client=sec,
                                                                                edinet_client=ed, **kw))
        with with_clients(fake(json.dumps(TICKERS).encode()), fake(CODELIST_ZIP)):
            code, res, _ = self.run_cli("pack", "fetch-open", "--seed-lines")
        self.assertEqual((code, res["edinet_codelist"]["lines_seeded"]), (0, 4))
        with with_clients(fake(json.dumps(TICKERS).encode()), fake(b"", status=500)):
            self.assertEqual(self.run_cli("pack", "fetch-open")[0], 1)
        blocked = Blocked(edinet.CODELIST_URL, 403, "http_403")
        with with_clients(fake(json.dumps(TICKERS).encode()), fake(b"", exc=blocked)):
            self.assertEqual(self.run_cli("pack", "fetch-open")[0], 2)
        with mock.patch.object(pack, "fetch_open", side_effect=guard.Busy("held")):
            self.assertEqual(self.run_cli("pack", "fetch-open", "--after-block")[0], 4)

    def test_pack_is_a_registered_command(self):
        self.assertIn("pack", cli.COMMANDS)


class RoundTripTest(unittest.TestCase):
    def test_pack_built_from_an_imported_store_is_identical_in_content(self):
        with tempfile.TemporaryDirectory() as t:
            src = make_cfg(os.path.join(t, "a"))
            seed_source_store(src)
            p1 = Path(t) / "p1"
            pack.build(src, p1, tag="pack-x", now=NOW, secrets=())
            dst = make_cfg(os.path.join(t, "b"))
            with store.session(dst):
                pass
            pack.import_pack(dst, p1)
            p2 = Path(t) / "p2"
            pack.build(dst, p2, tag="pack-x", now=NOW, secrets=())
            for name in ("sec_tickers.jsonl.gz", "edinet_codes.jsonl.gz"):
                self.assertEqual((p1 / name).read_bytes(), (p2 / name).read_bytes(), name)
            a = list(pack.decode_jsonl_gz((p1 / "edinet_business.jsonl.gz").read_bytes()))
            b = list(pack.decode_jsonl_gz((p2 / "edinet_business.jsonl.gz").read_bytes()))
            self.assertEqual([r["text"] for r in a], [r["text"] for r in b])


if __name__ == "__main__":
    unittest.main()


class LicenceDocsTest(unittest.TestCase):
    """DATA_LICENSES.md and docs/OPEN_PACK.md describe every registered source and what the pack ships."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_data_licenses_has_a_row_per_source(self):
        text = (self.ROOT / "DATA_LICENSES.md").read_text(encoding="utf-8")
        rows = {ln.split("|")[1].strip().strip("`") for ln in text.splitlines() if ln.startswith("| `")}
        self.assertTrue(set(provenance.SOURCES) <= rows, sorted(set(provenance.SOURCES) - rows))
        for sid in rows & set(provenance.SOURCES):
            row = next(ln for ln in text.splitlines() if ln.startswith(f"| `{sid}` |"))
            self.assertIn(f"| {provenance.tier_of(sid).value} |", row, sid)

    def test_data_licenses_describes_the_shipped_pack(self):
        text = (self.ROOT / "DATA_LICENSES.md").read_text(encoding="utf-8")
        self.assertNotIn("official-open` data only", text)
        self.assertNotIn("kept local. Needs", text)
        for sid in pack.PACK_SOURCES:
            self.assertIn(f"`{sid}` (", text, sid)
        for sid in pack.EXCLUDED_REASONS:
            if provenance.tier_of(sid) is provenance.LicenseTier.OFFICIAL_PRIVATE and sid != "sec_filing_text":
                self.assertIn(f"`{sid}`", text.split("Everything else never ships", 1)[1], sid)

    def test_open_pack_names_every_excluded_official_source(self):
        text = (self.ROOT / "docs" / "OPEN_PACK.md").read_text(encoding="utf-8")
        section = text.split("## What it does not contain", 1)[1].split("\n## ", 1)[0]
        for sid in pack.EXCLUDED_REASONS:
            if provenance.tier_of(sid) is provenance.LicenseTier.OFFICIAL_PRIVATE and sid != "sec_filing_text":
                self.assertIn(f"`{sid}`", section, sid)
        self.assertNotIn("if adapters are added", section)
