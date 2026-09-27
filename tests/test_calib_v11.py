"""Calibration cards v1.1: one class per fix from the live accuracy test (2026-09-26/27).

Each class pins one finding: keyword mining precision, noise-aware rule trials, a deck seeded by its inputs,
should_pass protection, chips without facets and role chips, the trial's casualty attribution, --from-run paths, the
band-read estimate, profile-only labels, df-0 seed replacements, and simplified/traditional Chinese matching.

No network and no paid calls: every Jev client is a fake. Company names and texts are invented.
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import calib, screen  # noqa: E402
from test_calib import synthetic_run  # noqa: E402
from test_cli_calib import IDEA, CliCase  # noqa: E402
from test_screen import FakeJev, StoreCase, band_factory  # noqa: E402


class AllExplicitJev(FakeJev):
    """FakeJev whose L2 answer is 'explicit' (p_pos 0.9) for every item."""

    def classify(self, items, question):
        out = super().classify(items, question)
        if question.key != "fit":
            for x in out:
                if x["status"] == "ok":
                    x.update(label="explicit", probs={"explicit": 0.8, "partial": 0.1, "contradicted": 0.05,
                                                      "insufficient": 0.05})
        return out


def all_explicit_factory(log):
    def factory(cfg, *, run_id, layer, budget_usd, dry_run, **kw):
        return AllExplicitJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=log)
    return factory


class TestDeckSeed(unittest.TestCase):
    """Fix 3: the deck is seeded by idea_key + a hash of the L2 inputs, not by run_id: a $0 cached rerun (a new
    run_id, the same inputs) gives the same deck."""

    def test_identical_inputs_give_identical_decks(self):
        result, inputs, pool, sv = synthetic_run()
        other = {**result, "run_id": "scr-test-2-cached-rerun"}
        a = calib.select_cards(result, inputs, sv, pool=pool, draws=400)
        b = calib.select_cards(other, inputs, sv, pool=pool, draws=400)
        self.assertEqual(a, b)
        s1 = calib.deck_seed(result, inputs)
        self.assertEqual(s1, calib.deck_seed(other, inputs))
        # another text for one company changes the seed; another idea too
        changed = {k: dict(v) for k, v in inputs.items()}
        next(iter(changed.values()))["evidence_sha"] = "sha-other"
        self.assertNotEqual(s1, calib.deck_seed(result, changed))
        self.assertNotEqual(s1, calib.deck_seed({**result, "idea": "another idea"}, inputs))
        # the order of the inputs does not matter
        self.assertEqual(s1, calib.deck_seed(result, list(reversed(list(inputs.values())))))


class TestFromRunPaths(StoreCase):
    """Fix 7: --from-run takes a run id, the run's output directory or its results.json."""

    def test_out_dir_and_results_json(self):
        base = self.run_screen(translate=False, keywords=["humanoid robot"], max_out=5, reads=1,
                               jev_factory=band_factory(self.log), out_dir=self.home / "base")
        for i, target in enumerate((self.home / "base", self.home / "base" / "results.json", base["run_id"])):
            with self.subTest(target=str(target)):
                res = self.run_screen(from_run=str(target), jev_factory=band_factory(self.log, no_l1=True),
                                      out_dir=self.home / f"next{i}")
                self.assertEqual((res["params"]["from_run"], res["layers"]["l1"]["from_run"]),
                                 (base["run_id"], base["run_id"]))
        dry = self.run_screen(dry_run=True, out_dir=self.home / "dry")
        self.assertEqual(dry["status"], "dry_run")
        with self.assertRaisesRegex(ValueError, "dry run"):
            self.run_screen(from_run=str(self.home / "dry"))
        (self.home / "junk").mkdir()
        with self.assertRaisesRegex(ValueError, "results.json"):
            self.run_screen(from_run=str(self.home / "junk"))


class TestFromRunPathsCli(CliCase):
    def test_cli_screen_and_cards_take_a_results_json(self):
        code, out, err = self.main(["screen", IDEA, "--reads", "1", "--cards", "0"])
        self.assertEqual(code, 0, err)
        res_json = next((self.home / "screens").glob("*/results.json"))
        code, out, err = self.main(["screen", IDEA, "--from-run", str(res_json), "--cards", "0"])
        self.assertEqual(code, 0, err)
        self.assertIn("L1 loaded from run", out + err)
        code, out, err = self.main(["cards", str(res_json)])
        self.assertEqual(code, 0, err)


class TestBandEstimate(StoreCase):
    """Fix 8: a dry run from an earlier run sizes the band reads by that run's real read-0 band (2 of 3 L2 items
    here), not by the 28% share; the dry-run output says which basis it used."""

    def test_real_band_size_from_the_base_run(self):
        base = self.run_screen(reads=3, jev_factory=band_factory(self.log), out_dir=self.home / "base")
        self.assertEqual(base["layers"]["l2"]["band"]["items"], 2)
        dry = self.run_screen(from_run=base["run_id"], dry_run=True, out_dir=self.home / "dry")
        est = dry["layers"]["l2"]["estimate"]
        self.assertEqual(dry["layers"]["l2"]["inputs"], 3)
        self.assertEqual(est["band_reads"]["items"], 2)
        self.assertEqual(est["band_reads"]["est_items"], 4)               # 2 band items x 2 more reads
        self.assertEqual(est["band_reads"]["basis"], "run")
        self.assertAlmostEqual(est["est_cost_usd"], 0.01 * (1 + 2 / 3 * 2), places=6)
        from jevscreen import report
        text = report.format_console(dry)
        self.assertIn("band reads: 2 of 3 L2 items", text)
        self.assertIn(base["run_id"], text)
        # without a base run: the share estimate, said as such
        plain = self.run_screen(dry_run=True, out_dir=self.home / "dry2")
        br = plain["layers"]["l2"]["estimate"]["band_reads"]
        self.assertEqual(br["basis"], "share")
        self.assertIn("about 28%", report.format_console(plain))


class TestProfileOnlyCap(StoreCase):
    """Fix 9: a label read from a company profile alone (no annual report) is at most partial, never explicit, and
    is marked 仅简介; the probabilities stay as read."""

    def test_profile_explicit_becomes_partial(self):
        res = self.run_screen(reads=1, jev_factory=all_explicit_factory(self.log))
        rows = {r["security_id"]: r for r in res["rows"]}
        servo, robo = rows["TSE:6000"], rows["NYSE:ROBO"]
        self.assertEqual((robo["l2_evidence"], robo["l2_label"]), ("annual_report", "explicit"))
        self.assertEqual((servo["l2_evidence"], servo["l2_label"], servo["l2_label_cap"]),
                         ("profile", "partial", "仅简介"))
        self.assertAlmostEqual(servo["l2_p_explicit"] or 0.8, 0.8)          # the read itself is not rewritten
        self.assertEqual(servo["score"], round(screen.score_of("partial", servo["l1_p_core"],
                                                               servo["market_cap_usd"], "profile"), 4))
        self.assertIsNone(robo.get("l2_label_cap"))
        stored = dict(self.query("SELECT security_id, label FROM screen_results WHERE run_id = ? AND layer = 'l2'",
                                 [res["run_id"]]))
        self.assertEqual(stored["TSE:6000"], "partial")
        from jevscreen import report
        self.assertIn("仅简介", report.format_console(res))
        self.assertIn("仅简介", report.render_markdown(res))
        # --rank ev: a profile's P(explicit) counts as partial
        self.assertAlmostEqual(screen.score_ev(0.8, 0.1, 0.5, 1e9, "profile"),
                               screen.score_ev(0.0, 0.9, 0.5, 1e9, "profile"))
        # the card's inclusion draw labels a profile row partial too
        cand = {"company_key": "p", "security_id": "p", "market_cap_usd": 1e9, "l1_p_core": 0.5,
                "l2_evidence": "profile", "l2_label": "partial", "l2_p_pos": 0.9, "l2_p_explicit": 0.8,
                "l2_p_partial": 0.1, "l2_reads": 1}
        rival = {**cand, "company_key": "q", "security_id": "q", "l2_evidence": "annual_report",
                 "l2_p_explicit": 0.1, "l2_p_partial": 0.8, "market_cap_usd": 1e8}
        self.assertEqual(calib.inclusion_probability([cand, rival], max_out=1, seed=1)["q"], 1.0)


# an invented Taiwanese filing (Traditional script) and an invented CNINFO one (Simplified)
TW_TEXT = ("本公司主要從事工業自動化設備之研發、製造與銷售，產品包括伺服馬達與控制器，客戶遍及亞洲各地之製造業者，"
           "並提供安裝與維護服務。\n\n"
           "本公司於本年度推出人形機器人關節模組，整合精密減速機與伺服驅動器，並與國際客戶合作開發身份認證與權限管理之"
           "工業物聯網平台。")
CN_TEXT = "公司主要从事人形机器人关节模组的研发与销售，并提供身份认证与权限管理服务。"


class TestTraditionalChinese(unittest.TestCase):
    """Fix 11: MOPS text is Traditional while the zh keywords are Simplified; both forms match through a small
    built-in table (no dependency), in excerpts, matched terms and the background df."""

    def test_simplified_terms_match_traditional_text(self):
        for term in ("人形机器人", "身份认证", "权限管理", "精密减速机"):
            with self.subTest(term=term):
                self.assertTrue(screen._term_pattern(term).search(TW_TEXT))
                self.assertTrue(screen._term_pattern(term).search(CN_TEXT) or term == "精密减速机")
        # a Traditional term finds Simplified text too; unrelated characters still do not match
        self.assertTrue(screen._term_pattern("人形機器人").search(CN_TEXT))
        self.assertIsNone(screen._term_pattern("人形汽车").search(TW_TEXT))
        # Japanese (shinjitai) and Latin terms are unchanged
        self.assertTrue(screen._term_pattern("認証").search("当社は認証基盤を提供"))
        self.assertIsNone(screen._term_pattern("認証").search(TW_TEXT))       # 認證 is not the Japanese 認証
        self.assertTrue(screen._term_pattern("AI 代理权限控制").search("提供AI代理權限控制"))
        self.assertEqual(screen.matched_terms(TW_TEXT, ["人形机器人", "身份认证", "汽车"]), ["人形机器人", "身份认证"])
        ex = screen.build_excerpts(TW_TEXT, terms=["人形机器人"], lang="zh")
        self.assertIn("keywords", [e["kind"] for e in ex])
        self.assertIn("人形機器人", next(e["text"] for e in ex if e["kind"] == "keywords"))

    def test_fold_and_background_df_count_both_scripts(self):
        self.assertEqual(calib._fold("身份認證"), calib._fold("身份认证"))
        df = calib.doc_freq([("tw", TW_TEXT), ("cn", CN_TEXT), ("x", "公司生产食品。")], ["人形机器人", "身份認證"])
        self.assertEqual((calib.df_count(df, "人形机器人"), calib.df_count(df, "身份認證")), (2, 2))

    def test_one_to_many_characters_and_one_fold_per_group(self):
        # review v1.1: 复/復, 系/係, 于/於, 历/曆 were missing; every group folds to one Simplified form
        from jevscreen import zhvariants
        self.assertEqual(zhvariants.to_simplified("恢復 關係 基於 日曆 收穫"), "恢复 关系 基于 日历 收获")
        for simp, trad in (("客户关系管理", "客戶關係管理"), ("基于云端", "基於雲端"), ("数据恢复", "數據恢復"),
                           ("日历", "日曆"), ("收获", "收穫"), ("纯碱", "純鹼"), ("余额", "餘額")):
            with self.subTest(term=simp):
                self.assertTrue(screen._term_pattern(simp).search(f"公司{trad}業務"))
                self.assertEqual(calib._fold(simp), calib._fold(trad))
        bad = {g for g in zhvariants.VARIANTS.values() if len({zhvariants.to_simplified(ch) for ch in g}) > 1}
        self.assertEqual(bad, set())
        df = calib.doc_freq([("A", "公司主要生產純鹼與燒鹼"), ("B", "期末馀额")], ["纯碱", "烧碱", "余额"])
        self.assertEqual([calib.df_count(df, t) for t in ("纯碱", "烧碱", "余额")], [1, 1, 1])

    def test_background_df_cache_is_keyed_by_the_matcher(self):
        from unittest import mock
        from jevscreen import zhvariants
        with tempfile.TemporaryDirectory() as d:
            cfg = type("Cfg", (), {"home": d})()
            txt = Path(d) / "a.txt"
            txt.write_text("公司主要生產純鹼", encoding="utf-8")
            with mock.patch.object(calib, "_source_doc_map", lambda con, src: {"d1": (str(txt), "A", "sig1")}):
                self.assertEqual(calib.df_count(calib.background_df(cfg, None, "mops_x", ["纯碱"]), "纯碱"), 1)
                cache = Path(d) / "calib" / "df-mops_x.json"
                import json
                stale = json.loads(cache.read_text(encoding="utf-8"))
                self.assertEqual(stale["matcher"], zhvariants.TABLE_VERSION)
                # a cache written by an older matcher (same documents) is not reused
                stale["matcher"] = "old-table"
                stale["hits"]["纯碱"] = []
                cache.write_text(json.dumps(stale), encoding="utf-8")
                self.assertEqual(calib.df_count(calib.background_df(cfg, None, "mops_x", ["纯碱"]), "纯碱"), 1)

    def test_table_has_no_conflicts(self):
        from jevscreen import zhvariants
        pairs = [p for p in zhvariants._PAIRS.split() if len(p) == 2 and p[0] != p[1]]
        simp, trad = {p[0] for p in pairs}, {p[1] for p in pairs}
        self.assertEqual(simp & trad, set())          # no Traditional form is itself another Simplified character
        self.assertGreater(len(set(pairs)), 1000)
        self.assertEqual(zhvariants.to_simplified("權限與認證"), "权限与认证")


# three invented Chinese vendors (yes companies) with the current seed 权限管理
YES_ZH = [
    "北辰云安公司提供权限管理平台，以及集群管理和自动化运维管理服务，支持配置和管理多云资源，并提供特权账号管理与零信任"
    "身份认证，兼容SAML、HTTP接口、SQL审计、VPN与POS终端接入，多因素认证可选。",
    "恒远数盾为金融客户提供权限管理系统，覆盖集群管理、智能化运维管理，并可配置和管理特权账号管理策略与零信任身份认证；"
    "支持SAML、HTTP、SQL、VPN、POS，多因素认证。",
    "瀚川智控的权限管理产品包括集群管理、数字化运维管理、零信任身份认证模块和特权账号管理，可设置和管理访问策略，同时提供"
    "SAML单点登录、HTTP网关、SQL审计、VPN与POS。",
]


def zh_corpus():
    docs = [(f"Y{i}", t) for i, t in enumerate(YES_ZH)]
    return docs + [(f"F{i}", "公司生产食品并在全国销售，主要客户为连锁超市。") for i in range(40)]


ZH_REL = ["Y0", "Y1", "Y2"] + [f"F{i}" for i in range(8)]


class TestMiningPrecision(unittest.TestCase):
    """Fix 1: word / run boundaries (no 限管理, クセス制御, 化运维管理, 置和管理), generic acronyms (SQL, VPN, POS,
    HTTP) only when seeded, support from >= 3 distinct yes companies, and no new term that worsens a yes company's
    keyword excerpt."""

    def mine(self, **kw):
        corpus = zh_corpus()
        return calib.mine_terms("zh", YES_ZH, ["权限管理"], [], [], lambda t: calib.doc_freq(corpus, t), ZH_REL,
                                **kw)

    def test_fragments_acronyms_and_support(self):
        got = self.mine()
        add = set(got["add"])
        self.assertTrue({"特权账号管理", "零信任身份认证", "运维管理", "SAML"} <= add, got["add"])
        self.assertFalse({"限管理", "化运维管理", "置和管理", "群管理"} & add)
        self.assertFalse({"SQL", "VPN", "POS", "HTTP"} & add)
        self.assertNotIn("多因素认证", add)                                     # 2 of 3 yes companies
        why = {r["term"]: r["why"] for r in got["rejected"]}
        self.assertEqual(why["限管理"], "fragment")
        self.assertEqual(why["化运维管理"], "fragment")
        self.assertEqual(why["置和管理"], "fragment")
        self.assertEqual((why["SQL"], why["HTTP"]), ("acronym", "acronym"))
        self.assertEqual(why["多因素认证"], "yes_docs")
        # a seeded acronym (any language of the run) is allowed; so is one of >= 4 letters
        got = self.mine(seeded=["VPN"])
        self.assertIn("VPN", got["add"])
        # fewer yes companies than 3: all of them are needed
        corpus = zh_corpus()
        two = calib.mine_terms("zh", YES_ZH[:2], ["权限管理"], [], [], lambda t: calib.doc_freq(corpus, t), ZH_REL)
        self.assertIn("多因素认证", two["add"])
        # the same company's filings count once: 自动化运维管理 is Y0's alone
        docs = [YES_ZH[0], YES_ZH[0], YES_ZH[0], YES_ZH[1]]
        per_doc = calib.mine_terms("zh", docs, ["权限管理"], [], [], lambda t: calib.doc_freq(corpus, t), ZH_REL)
        self.assertIn("自动化运维管理", per_doc["add"])
        dup = calib.mine_terms("zh", docs, ["权限管理"], [], [], lambda t: calib.doc_freq(corpus, t), ZH_REL,
                               yes_keys=["Y0", "Y0", "Y0", "Y1"])
        self.assertNotIn("自动化运维管理", dup["add"])

    def test_katakana_run_boundaries(self):
        self.assertNotIn("クセス制御", calib._candidates_in("当社はアクセス制御を提供", "ja"))
        yes = ["当社はアクセス制御製品とシングルサインオンを提供。", "アクセス制御とシングルサインオンの基盤。",
               "アクセス制御、シングルサインオン対応。"]
        corpus = [(f"Y{i}", t) for i, t in enumerate(yes)] + [(f"F{i}", "食品を製造販売。") for i in range(40)]
        got = calib.mine_terms("ja", yes, ["アクセス制御"], [], [], lambda t: calib.doc_freq(corpus, t),
                               ["Y0", "Y1", "Y2", "F0", "F1"])
        self.assertIn("シングルサインオン", got["add"])
        self.assertNotIn("クセス制御", got["add"])


class TestExcerptRegression(unittest.TestCase):
    """Fix 1 (d): a mined term that would move a yes company's keyword excerpt away from an old term it matched is
    dropped, whatever the other gates said."""
    OVERVIEW = "当社グループは、企業向けソフトウェアの開発・販売を主な事業としております。" * 9

    def entry(self, d: Path, key, name, *paras):
        p = d / f"{key}.txt"
        p.write_text("\n\n".join([self.OVERVIEW, *paras]), encoding="utf-8")
        c = {"company_key": key, "security_id": f"TSE:{key}", "name": name,
             "desc": {"plain": f"{name} makes software.", "text": f"[tradingview_profile] {name} makes software.",
                      "tier": "gray-private"}}
        return c, {"doc_id": key, "source_id": "edinet_yuho", "form": "有価証券報告書", "filing_date": "2026-06-20",
                   "url": None, "text_path": str(p)}

    def test_a_term_that_moves_a_yes_excerpt_is_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pad = "当社は、全国の拠点において営業体制の強化と人材の育成を継続して進めており、お客様との長期的な関係を重視しております。"
            yes = self.entry(d, "9475", "KUMOGA",
                             "当社は、企業のお客様にシングルサインオンの基盤サービスを開発して提供しております。" + pad, pad,
                             "当社は、ISMSの認証を取得し、クラウド監視サービスとログ保管サービスを開始しております。" + pad)
            other = self.entry(d, "9326", "DigiArc", "当社は、クラウド監視サービスとログ保管の認証連携を提供しております。")
            old = {"terms": ["シングルサインオン", "認証"], "weak": []}
            kept, dropped = calib.drop_worsening_terms([yes, other], "ja", old, ["クラウド監視", "ログ保管"],
                                                       protected=["TSE:9475"], today=dt.date(2026, 9, 27))
            self.assertEqual(kept, [])
            self.assertEqual([x["term"] for x in dropped], ["クラウド監視", "ログ保管"])
            self.assertEqual(dropped[0]["why"], "worsens KUMOGA")
            # not protected: nothing is dropped
            kept, dropped = calib.drop_worsening_terms([yes, other], "ja", old, ["クラウド監視"], protected=[],
                                                       today=dt.date(2026, 9, 27))
            self.assertEqual((kept, dropped), (["クラウド監視"], []))
            # the peer preview refuses the same change as a whole
            prev = calib.peer_preview([yes, other], "ja", old, {"terms": old["terms"] + ["クラウド監視"], "weak": []},
                                      protected=["TSE:9475"], today=dt.date(2026, 9, 27))
            self.assertTrue(prev["rejected"])
            self.assertIn("KUMOGA", prev["why_zh"])
            self.assertIn("变差", prev["why_zh"])

    def test_weakening_a_noisy_seed_on_a_yes_company_is_still_adopted(self):
        # review v1.1: 認証 (ISMS boilerplate in a dozen paragraphs) is made weak; the yes company's excerpt moves off
        # the boilerplate to its シングルサインオン paragraph. That is the point of down-weighting, not 'worse'.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pad = "当社は、全国の拠点において営業体制の強化と人材の育成を継続して進めており、お客様との長期的な関係を重視しております。"
            isms = [f"当社は、ISMSの認証を取得し、第{i}工場の品質管理体制を強化しております。" + pad for i in range(12)]
            yes = self.entry(d, "9475", "KUMOGA", *isms,
                             "当社は、企業のお客様にシングルサインオンの基盤サービスを開発して提供しております。" + pad)
            old = {"terms": ["認証", "シングルサインオン"], "weak": []}
            new = {"terms": ["シングルサインオン"], "weak": ["認証"]}
            prev = calib.peer_preview([yes], "ja", old, new, protected=["TSE:9475"], today=dt.date(2026, 9, 27))
            self.assertEqual([x["name"] for x in prev["changes"]], ["KUMOGA"])      # the excerpt did move
            self.assertIn("シングルサインオン", prev["changes"][0]["kw_after"])
            self.assertEqual(prev["changes"][0]["terms_lost"], [])
            self.assertFalse(prev["rejected"], prev["why_zh"])
            # a term that stays strong and is lost still counts as worse
            new2 = {"terms": ["シングルサインオン", "クラウド監視"], "weak": ["認証"]}
            prev2 = calib.peer_preview([yes], "ja", {"terms": ["シングルサインオン"], "weak": ["認証"]}, new2,
                                       protected=["TSE:9475"], today=dt.date(2026, 9, 27))
            self.assertFalse(prev2["rejected"])                                  # no クラウド監視 here: unchanged


# invented Japanese reducer makers (verified companies) whose texts never use the model's seed words
REDUCER_JA = [
    "当社は、産業用ロボットの関節に用いられる精密減速機と波動歯車装置を製造しております。サーボモータ向けの製品も展開しております。",
    "当社グループの主力製品は精密減速機であり、協働ロボットや人型ロボットの関節向けに波動歯車装置を供給しております。",
    "精密減速機事業では、波動歯車装置と遊星減速機を、ロボットメーカー各社に販売しております。",
]
FILLER_JA = ["当社は、食品を製造し、全国のスーパーマーケットに販売しております。",
             "当社グループは、建設機械の販売とレンタルを主な事業としております。",
             "当社は、ロボットを使った物流倉庫の運営サービスを提供しております。"]
KO_TEXTS = ["당사는 펩타이드 원료의약품을 생산하며 비만 치료제용 펩타이드 합성 설비를 보유하고 있습니다.",
            "당사의 주요 사업은 펩타이드 원료의약품 위탁생산이며, 글로벌 제약사에 공급합니다.",
            "펩타이드 원료의약품과 주사제 완제품을 생산하는 위탁개발생산 기업입니다."]


class TestSeedReplacements(unittest.TestCase):
    """Fix 10: ja / ko seed terms the model wrote that appear in no filing of their source (df 0) are replaced by
    terms mined from the real texts of the verified companies, behind the same gates (3 companies, df, lift, whole
    words)."""

    def corpus(self, texts, fillers):
        docs = [(f"Y{i}", t) for i, t in enumerate(texts)]
        return docs + [(f"F{i}", fillers[i % len(fillers)]) for i in range(60)]

    def test_ja_replacements(self):
        corpus = self.corpus(REDUCER_JA, FILLER_JA)
        rel = ["Y0", "Y1", "Y2"] + [f"F{i}" for i in range(10)]
        got = calib.mine_replacements("ja", REDUCER_JA, ["Y0", "Y1", "Y2"], ["人型ロボット関節用減速ギア"], [], [],
                                      lambda t: calib.doc_freq(corpus, t), rel)
        self.assertEqual(got["add"], ["波動歯車装置", "精密減速機"])
        self.assertTrue(all(e["source"] == "replacement" for e in got["log"]))
        why = {r["term"]: r["why"] for r in got["rejected"]}
        self.assertEqual((why["密減速機"], why["減速機"]), ("fragment", "fragment"))
        self.assertNotIn("遊星減速機", got["add"])                  # one company only

    def test_ko_replacements_strip_particles(self):
        corpus = self.corpus(KO_TEXTS, ["당사는 식품을 제조하여 판매합니다.", "당사는 건설 장비를 임대합니다."])
        rel = ["Y0", "Y1", "Y2"] + [f"F{i}" for i in range(10)]
        got = calib.mine_replacements("ko", KO_TEXTS, ["Y0", "Y1", "Y2"], ["펩타드 원료"], [], [],
                                      lambda t: calib.doc_freq(corpus, t), rel)
        self.assertIn("펩타이드", got["add"])
        self.assertIn("원료의약품", got["add"])
        self.assertNotIn("원료의약품을", got["add"])

    def test_propose_keywords_uses_them_when_seeds_are_absent(self):
        from unittest import mock
        corpus = self.corpus(REDUCER_JA, FILLER_JA)
        keys = [f"ck:TSE:{9000 + i}" for i in range(3)]
        ck: dict[str, str] = {}
        tag = "[annual report excerpts: EDINET 有価証券報告書 filed 2026-06-20; language ja]\n\n"
        inputs = {k: {"company_key": k, "security_id": f"TSE:{9000 + i}", "evidence": "annual_report",
                      "source_id": "edinet_yuho", "lang": "ja", "text": tag + REDUCER_JA[i], "excerpts": [],
                      "keyword_hit": False, "matched_terms": [], "evidence_sha": f"s{i}"} for i, k in enumerate(keys)}
        rows = [{"company_key": k, "security_id": inputs[k]["security_id"], "name": f"Gearwise {i}", "rank": i + 1,
                 "l2_label": "partial", "l2_p_pos": 0.85} for i, k in enumerate(keys)]
        # verified companies below p̄ 0.8 (weak yeses) do not feed the replacements
        for j in range(3):
            weak_key = f"ck:TSE:91{j:02d}"
            inputs[weak_key] = {**inputs[keys[0]], "company_key": weak_key, "security_id": f"TSE:91{j:02d}",
                                "text": tag + "当社は、ロボット用の遊星減速機を販売しております。"}
            rows.append({"company_key": weak_key, "security_id": f"TSE:91{j:02d}", "name": f"Weakgear {j}",
                         "rank": 4 + j, "l2_label": "partial", "l2_p_pos": 0.6})
            ck[weak_key] = f"W{j}"
        result = {"run_id": "scr-x", "idea": "humanoid robot joint reducers", "rows": rows,
                  "terms_by_lang": {"en": ["joint reducer"], "ja": ["人型ロボット関節用減速ギア"]}}
        rel = set(keys) | {f"F{i}" for i in range(10)}
        ck.update({k: f"Y{i}" for i, k in enumerate(keys)})

        def fake_df(cfg, con, src, terms):
            got = calib.doc_freq(corpus, terms)
            got["doc_companies"] = [{v: k for k, v in ck.items()}.get(c, c) for c in got["doc_companies"]]
            got["hits"] = {t: [{v: k for k, v in ck.items()}.get(c, c) for c in h] for t, h in got["hits"].items()}
            return got
        with mock.patch.object(calib, "background_df", fake_df), \
                mock.patch.object(calib, "l1_relevant", lambda con, run_id: rel):
            props = calib.propose_keywords(None, None, result, None, inputs, companies=({}, {}))
        ja = next(p for p in props if p["lang"] == "ja")
        self.assertEqual(ja["noisy"]["absent"], ["人型ロボット関節用減速ギア"])
        self.assertEqual(ja["add"], ["波動歯車装置", "精密減速機"])
        self.assertEqual({e["source"] for e in ja["log"]}, {"replacement"})
        self.assertEqual(ja["log"][0]["replaces"], ["人型ロボット関節用減速ギア"])
        self.assertNotIn("遊星減速機", ja["add"])
        # a company-name katakana word is never a replacement
        texts = [t + "子会社ハーモニクス・インコーポレイテッドを通じて販売。" for t in REDUCER_JA]
        corpus2 = self.corpus(texts, FILLER_JA)
        got = calib.mine_replacements("ja", texts, ["Y0", "Y1", "Y2"], ["x"], [], [],
                                      lambda t: calib.doc_freq(corpus2, t), ["Y0", "Y1", "Y2"] + [f"F{i}" for i in range(9)])
        self.assertNotIn("インコーポレイテッド", got["add"])


class TestLegacyMinedAdds(unittest.TestCase):
    """Review v1.1: a sieve written by v1 holds mined noise in keywords.add (zh SQL / VPN / 化运维管理, ja クセス制御).
    Those learned adds are not seeds (they must not let SQL through the acronym gate of another language), and the
    next answer re-runs the v1.1 gates on them and removes the ones that fail, with a log entry."""
    JA = ["当社は、アクセス制御製品とシングルサインオンを提供し、SQL監査にも対応しております。",
          "アクセス制御とシングルサインオンの基盤を開発し、SQLデータベースの監査機能を提供。",
          "アクセス制御、シングルサインオン、SQL監査の各サービスを展開しております。"]
    FILL = ["当社は、食品を製造し、全国のスーパーマーケットに販売しております。",
            "当社グループは、建設機械の販売とレンタルを主な事業としております。"]

    def v1_sieve(self):
        def mined(ts):
            return [{"term": t, "action": "add", "source": "mined", "df": 20, "lift": 9.0, "yes_docs": 3,
                     "at": "2026-09-26T17:39:59+00:00"} for t in ts]
        zh = ["SQL", "VPN", "化运维管理", "置和管理", "特权账号管理", "UEBA"]
        return {**calib.new_sieve("企业身份与访问控制"), "keywords": {
            "zh": {"add": zh, "weak": [], "log": mined(zh)},
            "ja": {"add": ["クセス制御"], "weak": [], "log": mined(["クセス制御"])}}}

    def propose(self, sieve):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            keys = [f"ck:TSE:{9500 + i}" for i in range(3)]
            tag = "[annual report excerpts: EDINET 有価証券報告書 filed 2026-06-20; language ja]\n\n"
            inputs, cs, docs, rows = {}, {}, {}, []
            for i, k in enumerate(keys):
                p = d / f"{i}.txt"
                p.write_text(self.JA[i], encoding="utf-8")
                sid = f"TSE:{9500 + i}"
                inputs[k] = {"company_key": k, "security_id": sid, "evidence": "annual_report",
                             "source_id": "edinet_yuho", "lang": "ja", "text": tag + self.JA[i], "excerpts": [],
                             "keyword_hit": True, "matched_terms": [], "evidence_sha": f"s{i}"}
                cs[k] = {"company_key": k, "security_id": sid, "name": f"Kagiya {i}",
                         "desc": {"plain": "Kagiya makes software.", "text": "[tradingview_profile] Kagiya makes "
                                  "software.", "tier": "gray-private"}}
                docs[k] = {"doc_id": k, "source_id": "edinet_yuho", "form": "有価証券報告書",
                           "filing_date": "2026-06-20", "url": None, "text_path": str(p)}
                rows.append({"company_key": k, "security_id": sid, "name": f"Kagiya {i}", "rank": i + 1,
                             "l2_label": "explicit", "l2_p_pos": 0.9})
            corpus = [(k, t) for k, t in zip(keys, self.JA)] + [(f"F{i}", self.FILL[i % 2]) for i in range(60)]
            rel = set(keys) | {f"F{i}" for i in range(4)}
            kw = {lg: (sieve.get("keywords") or {}).get(lg) or {"add": []} for lg in ("ja", "zh")}
            result = {"run_id": "scr-legacy", "idea": "企业身份与访问控制", "rows": rows,
                      "keywords": {"generated": {"en": ["access control"], "ja": ["アクセス制御"], "zh": ["访问控制"]}},
                      "terms_by_lang": {"en": ["access control"], "ja": ["アクセス制御"] + kw["ja"]["add"],
                                        "zh": ["访问控制"] + kw["zh"]["add"]}}      # as resolve_keywords appends them
            with mock.patch.object(calib, "background_df",
                                   lambda cfg, con, src, terms: calib.doc_freq(corpus, terms)), \
                    mock.patch.object(calib, "l1_relevant", lambda con, run_id: rel):
                return calib.propose_keywords(None, None, result, sieve, inputs, companies=(cs, docs))

    def test_learned_adds_are_not_seeds(self):
        plain = self.propose({**calib.new_sieve("企业身份与访问控制")})
        v1 = self.propose(self.v1_sieve())
        for props in (plain, v1):
            ja = next(p for p in props if p["lang"] == "ja")
            self.assertIn("シングルサインオン", ja["add"])
            self.assertNotIn("SQL", ja["add"])
            self.assertEqual({r["term"]: r["why"] for r in ja["mined"]["rejected"]}.get("SQL"), "acronym")

    def test_failing_legacy_adds_are_removed_with_a_log_entry(self):
        sv = self.v1_sieve()
        props = self.propose(sv)
        by = {p["lang"]: p for p in props}
        self.assertEqual({x["term"]: x["why"] for x in by["ja"]["remove"]}, {"クセス制御": "fragment"})
        self.assertEqual({x["term"]: x["why"] for x in by["zh"]["remove"]},
                         {"SQL": "acronym", "VPN": "acronym", "化运维管理": "fragment", "置和管理": "fragment"})
        for p in props:
            sv = calib.merge_keywords(sv, p["lang"], add=p["add"] if p["adopted"] else [],
                                      weak=p["weak"] if p["adopted"] else [], remove=[x["term"] for x in p["remove"]],
                                      log=(p["log"] if p["adopted"] else []) + p["remove_log"], now="t")
        self.assertEqual(sv["keywords"]["zh"]["add"], ["特权账号管理", "UEBA"])
        self.assertNotIn("クセス制御", sv["keywords"]["ja"]["add"])
        removed = [e for e in sv["keywords"]["zh"]["log"] if e["action"] == "remove"]
        self.assertEqual({e["term"] for e in removed}, {"SQL", "VPN", "化运维管理", "置和管理"})
        self.assertTrue(all(e["source"] == "regate" and e["why"] for e in removed))
        self.assertEqual(calib.validate_sieve(sv), [])
        self.assertIn("-クセス制御", calib.keyword_log_zh("ja", [], None, removed=by["ja"]["remove"]))
        # a user's own add (no 'mined' log entry) is never re-gated, and new mined adds carry the gate version
        sv2 = self.v1_sieve()
        sv2["keywords"]["zh"]["log"] = [e for e in sv2["keywords"]["zh"]["log"] if e["term"] != "VPN"]
        zh2 = next(p for p in self.propose(sv2) if p["lang"] == "zh")
        self.assertNotIn("VPN", [x["term"] for x in zh2["remove"]])
        ja = by["ja"]
        self.assertTrue(ja["log"] and all(e.get("gate") == calib.MINE_GATE_VERSION for e in ja["log"]
                                          if e["action"] == "add"))


class TestAnswerEstimateWording(CliCase):
    def test_the_estimate_names_the_extra_reads_and_the_cap(self):
        from test_cli_calib import rule_factory
        log: list = []
        rid, _ = self.screen_with_deck(factory=rule_factory(log))
        code, out, err = self.main(["answer", "1不要c", "--run", rid, "--verbose"], rule_factory(log))
        self.assertEqual(code, 0, out + err)
        line = next(x for x in out.splitlines() if x.startswith("预计"))
        self.assertNotIn("预计最多：规则试验", line)
        self.assertIn("规则试验约 $0.0200", line)
        self.assertIn("边界公司多读和对照读最多再加 $0.0200", line)             # the control (no yes company here)
        self.assertIn("连同多读最多 $0.0556", line)
        self.assertIn("到上限就停", line)
        self.assertIn("预算只够计划内的试验", out)                                 # 0.0356 <= $0.05 < 0.0556


class TestLegacyMinedAddsCli(CliCase):
    def test_answer_removes_failing_learned_adds(self):
        from test_cli_calib import rule_factory
        log: list = []
        rid, _ = self.screen_with_deck(factory=rule_factory(log))
        path = calib.sieve_path(self.cfg, IDEA)
        sv = calib.load_sieve(path)
        mined = [{"term": t, "action": "add", "source": "mined", "df": 20, "lift": 9.0, "yes_docs": 3,
                  "at": "2026-09-26T17:39:59+00:00"} for t in ("SQL", "化运维管理", "特权账号管理")]
        sv["keywords"] = {"zh": {"add": ["SQL", "化运维管理", "特权账号管理", "VPN"], "weak": [], "log": mined}}
        calib.save_sieve(path, sv)
        code, out, err = self.main(["answer", "1不要c", "--run", rid, "--verbose"], rule_factory(log))
        self.assertEqual(code, 0, out + err)
        self.assertIn("-SQL（泛用缩写，旧版挖出的）", out)
        self.assertIn("-化运维管理（词的碎片，旧版挖出的）", out)
        kw = calib.load_sieve(path)["keywords"]["zh"]
        self.assertEqual(kw["add"], ["特权账号管理", "VPN"])          # VPN is the user's own: never re-gated
        self.assertEqual([(e["term"], e["why"]) for e in kw["log"] if e["action"] == "remove"],
                         [("SQL", "acronym"), ("化运维管理", "fragment")])


RULE_MARKERS = {"user_not_supplier": "A company that only builds", "laundry_list": "A service provider",
                "mention_only": "A single mention", "homonym": "count only when they mean"}


class FnJev:
    """Fake trial client: p_pos = fn(item_id, read, frozenset(rule ids in the question))."""

    def __init__(self, fn, cost=0.001, afford=None):
        self.fn, self.cost, self.afford = fn, cost, afford
        self.spent_usd, self.calls = 0.0, []

    def estimate(self, items, question):
        return {"est_cost_usd": self.cost}

    def classify(self, items, question):
        rules = frozenset(r for r, m in RULE_MARKERS.items() if m in question.criteria["insufficient"])
        self.calls.append((question.read, sorted(it.item_id for it in items), sorted(rules)))
        self.spent_usd += self.cost
        out = []
        for it in items:
            p = self.fn(it.item_id, question.read, rules)
            out.append({"item_id": it.item_id, "label": "partial" if p >= 0.5 else "insufficient",
                        "probs": {"explicit": p / 2, "partial": p / 2, "contradicted": 0.0, "insufficient": 1 - p},
                        "request_id": "r", "status": "ok", "error": None, "cached": False})
        return out


class CachingFnJev(FnJev):
    """FnJev with the real client's item cache: an answer is keyed by jev.item_key (model, question incl. its read
    index, issuer, text), never by the packet, and a cached answer costs nothing. `prefill` = the base run's reads."""

    def __init__(self, fn, **kw):
        super().__init__(fn, **kw)
        self.cache: dict[str, float] = {}
        self.hits: list[tuple[int, str]] = []

    def key(self, question, it):
        from jevscreen import jev
        return jev.item_key(question, it.issuer, it.text)

    def prefill(self, question, items, p_of):
        import dataclasses
        for r, ps in p_of.items():
            for it in items:
                if it.item_id in ps:
                    self.cache[self.key(dataclasses.replace(question, read=r), it)] = ps[it.item_id]

    def classify(self, items, question):
        rules = frozenset(r for r, m in RULE_MARKERS.items() if m in question.criteria["insufficient"])
        self.calls.append((question.read, sorted(it.item_id for it in items), sorted(rules)))
        out, miss = [], False
        for it in items:
            k = self.key(question, it)
            if k in self.cache:
                p, cached = self.cache[k], True
                self.hits.append((question.read, it.item_id))
            else:
                p, cached, miss = self.fn(it.item_id, question.read, rules), False, True
                self.cache[k] = p
            out.append({"item_id": it.item_id, "label": "partial" if p >= 0.5 else "insufficient",
                        "probs": {"explicit": p / 2, "partial": p / 2, "contradicted": 0.0, "insufficient": 1 - p},
                        "request_id": "r", "status": "ok", "error": None, "cached": cached})
        if miss:
            self.spent_usd += self.cost
        return out


class TestNoiseAwareTrials(unittest.TestCase):
    """Fix 2: a would-be casualty on the boundary (p in [0.40, 0.60] or a high read sd) is re-read twice more before
    it can veto a rule; when the re-reads cannot be made it does not veto.
    Fix 6 (the glp1 anomaly: every rule was 'rejected for harming 002693', which had been answered yes): the drop
    of a yes company is attributed to a rule only when the same company stays up without that rule (a control read
    of the same packets); a company that drops under every question is read noise, never a veto."""
    BASE = {"HUBS": 0.7, "RXT": 0.62, "EDGE": 0.5467, "SAIL": 0.9, "A1": 0.95, "A2": 0.95, "A3": 0.95}

    def setup(self):
        from test_calib import sieve_doc, FACETS
        sv = sieve_doc(facets=FACETS, examples=[
            {"company_key": "HUBS", "want": "no", "chip": "c", "source": "card", "pin": True},
            {"company_key": "RXT", "want": "no", "chip": "e", "source": "card", "pin": True},
            {"company_key": "EDGE", "want": "partial", "chip": "b", "source": "card", "pin": True},
            {"security_id": "X:SAIL", "want": "explicit", "source": "sieve", "pin": False}])
        base_reads = {k: {"security_id": f"X:{k}", "name": k.title(), "p_pos": p, "l2_label": "partial",
                          "keyword_hit": True, "market_cap_usd": 1e9, "p_pos_sd": 0.05}
                      for k, p in self.BASE.items()}
        inputs = {k: {"company_key": k, "text": f"[annual report excerpts: SEC 10-K filed 2026-01-01; language en]"
                                                f"\n\n{k} text"} for k in self.BASE}
        return sv, base_reads, inputs

    def trial(self, fn, cands=("user_not_supplier",), **kw):
        sv, base_reads, inputs = self.setup()
        client = FnJev(fn)
        out = calib.trial_rules([{"id": c, "version": 1} for c in cands], sv, base_reads, client, inputs=inputs,
                                idea="idea x", now="t", **kw)
        return out, client

    @staticmethod
    def rule_fixes_hubs(edge):
        def fn(k, read, rules):
            if k == "HUBS" and rules:
                return 0.3
            if k == "EDGE":
                return edge(read, rules)
            return TestNoiseAwareTrials.BASE[k]
        return fn

    def test_a_boundary_casualty_that_recovers_on_reread_does_not_veto(self):
        out, client = self.trial(self.rule_fixes_hubs(lambda read, rules: 0.45 if read < 2 else 0.62))
        self.assertEqual([c["id"] for c in out["adopted"]], ["user_not_supplier"])
        t = out["trials"][0]
        self.assertEqual(t["flips"], [])
        self.assertEqual([(n["name"], n["why"]) for n in t["noise"]], [("Edge", "reread")])
        self.assertEqual([c[0] for c in client.calls], [0, 1, 2, 3])        # re-reads 2 and 3 of EDGE alone
        self.assertEqual(client.calls[2][1], ["EDGE"])
        self.assertIn("读数噪声", calib.render_trial_zh(out))

    def test_a_boundary_casualty_without_rereads_leaves_the_rule_untried(self):
        # review v1.1: an unconfirmed casualty is a gap, not noise: a tight budget must never adopt what a larger
        # one vetoes (EDGE reads 0.45 with the rule and 0.6 without it: the rule harms it)
        harmful = self.rule_fixes_hubs(lambda read, rules: 0.45 if rules else 0.6)
        for budget in (0.0025, 0.0035):
            with self.subTest(budget=budget):
                out, _ = self.trial(harmful, budget_usd=budget)
                self.assertEqual((out["adopted"], out["rejected"], out["untried"], out["status"]),
                                 ([], [], ["user_not_supplier"], "budget"))
                t = out["trials"][0]
                self.assertFalse(t["adopted"])
                self.assertFalse(t["complete"])
                self.assertIn("Edge", t["why_zh"])
                self.assertEqual(t["noise"], [])
                text = calib.render_trial_zh(out)
                self.assertNotIn("读数噪声", text)
                self.assertIn("user_not_supplier：没试（预算不够）", text)
        out, _ = self.trial(harmful, budget_usd=0.05)
        self.assertEqual(out["rejected"][0]["why_zh"], "这条规则会误伤 Edge，未采用")

    def test_a_rule_that_crushes_a_yes_company_vetoes_even_when_its_control_is_low(self):
        # review v1.1: a control just under 0.5 excuses only a drop to about the control, not any drop
        for trial_p, ctrl_p in ((0.02, 0.45), (0.05, 0.48)):
            with self.subTest(trial=trial_p, control=ctrl_p):
                out, _ = self.trial(self.rule_fixes_hubs(lambda read, rules: trial_p if rules else ctrl_p))
                self.assertEqual(out["adopted"], [])
                self.assertEqual(out["rejected"][0]["why_zh"], "这条规则会误伤 Edge，未采用")
                self.assertNotIn("读数噪声", calib.render_trial_zh(out))

    def test_the_control_is_a_fresh_read_not_the_base_runs_cache(self):
        # review v1.1: with no rule adopted since the base run, the control question IS the base run's question;
        # reading it at the base run's read indices would replay the base reads from the item cache. EDGE's base
        # p̄ 0.5467 came from reads 0.45 / 0.45 / 0.74; fresh reads give 0.6 without the rule and 0.3 with it.
        sv, base_reads, inputs = self.setup()
        client = CachingFnJev(self.rule_fixes_hubs(lambda read, rules: 0.3 if rules else 0.6))
        items = calib._trial_items(list(self.BASE), base_reads, inputs)
        q2 = screen.build_l2_question("idea x", None, rules=sv.get("rules") or [], facets=calib._facets(sv))
        base0 = dict(self.BASE, EDGE=0.45)
        client.prefill(q2, items, {0: base0, 1: {"EDGE": 0.45}, 2: {"EDGE": 0.74}})
        out = calib.trial_rules([{"id": "user_not_supplier", "version": 1}], sv, base_reads, client,
                                inputs=inputs, idea="idea x", now="t")
        self.assertEqual(out["adopted"], [])
        self.assertEqual(out["rejected"][0]["why_zh"], "这条规则会误伤 Edge，未采用")
        controls = [c for c in client.calls if c[2] == []]
        self.assertEqual(sorted({c[0] for c in controls}), list(calib.CONTROL_READS))
        self.assertFalse(set(calib.CONTROL_READS) & {0, 1, 2, 3, 4})       # never a read index of a base run
        self.assertEqual([h for h in client.hits if h[1] == "EDGE"], [])

    def test_a_drop_under_every_question_is_noise_not_the_rule(self):
        # the glp1 case: EDGE (answered yes, p̄ 0.55) reads 0.45 whatever the question, even with no new rule
        cands = ("laundry_list", "homonym", "user_not_supplier")
        out, client = self.trial(self.rule_fixes_hubs(lambda read, rules: 0.45), cands=cands)
        text = calib.render_trial_zh(out)
        self.assertNotIn("误伤 Edge", text)
        for r in out["rejected"]:
            self.assertNotIn("Edge", r["why_zh"])
        self.assertIn("user_not_supplier", [c["id"] for c in out["adopted"]])
        noise = [n for t in out["trials"] for n in t["noise"]]
        self.assertTrue(noise and all(n["why"] == "control" for n in noise))
        controls = [c for c in client.calls if c[2] == []]                 # the control question: no new rule
        self.assertEqual(sorted({c[0] for c in controls}), list(calib.CONTROL_READS))   # once, then reused
        self.assertEqual(len(controls), len(calib.CONTROL_READS))

    def test_the_estimate_bounds_rereads_and_the_control(self):
        # review v1.1: the planned reads alone are not an upper bound once re-reads and a control read exist
        sv, base_reads, inputs = self.setup()
        cands = [{"id": c, "version": 1} for c in ("laundry_list", "homonym", "user_not_supplier")]
        est = calib.estimate_trials(cands, sv, base_reads, FnJev(lambda *a: 0.5), inputs=inputs, idea="idea x")
        self.assertAlmostEqual(est["est_cost_usd"], 0.001 * 2 * 5)                  # 5 planned trials x 2 reads
        # control once (2 reads) + 2 re-reads per trial of the companies that can be casualties
        self.assertAlmostEqual(est["extra_max_usd"], 0.001 * 2 + 0.001 * 2 * 5)
        for n in (1, 3):
            with self.subTest(candidates=n):
                est = calib.estimate_trials(cands[:n], sv, base_reads, FnJev(lambda *a: 0.5), inputs=inputs,
                                            idea="idea x")
                out, _ = self.trial(self.rule_fixes_hubs(lambda read, rules: 0.45),
                                    cands=tuple(c["id"] for c in cands[:n]))
                self.assertLessEqual(out["cost_usd"], est["est_cost_usd"] + est["extra_max_usd"] + 1e-9)
                if n == 1:            # re-reads 2 + control 2 on top of the 2 planned reads
                    self.assertGreater(out["cost_usd"], est["est_cost_usd"])

    def test_a_real_casualty_still_vetoes(self):
        # without the rule EDGE stays up (0.6); with it it drops on every read: the rule harms it
        out, _ = self.trial(self.rule_fixes_hubs(lambda read, rules: 0.3 if rules else 0.6))
        self.assertEqual(out["adopted"], [])
        self.assertEqual(out["rejected"][0]["why_zh"], "这条规则会误伤 Edge，未采用")
        # far from the boundary (base 0.9): no re-read, the control still decides
        def fn(k, read, rules):
            if k == "HUBS" and rules:
                return 0.3
            if k == "SAIL":
                return 0.2 if rules else 0.9
            return self.BASE[k]
        out, client = self.trial(fn)
        self.assertEqual(out["rejected"][0]["why_zh"], "这条规则会误伤 Sail，未采用")
        self.assertNotIn(2, [c[0] for c in client.calls])


class TestShouldPassProtection(StoreCase):
    """Fix 4: a should_pass company (the user's AI's check) that comes out unverified, or below the level it should
    pass at, or lower than in the base run, gets 2 more reads and is flagged; it is never silently lost (glp1:
    000935 fell from explicit #1 to partial #6 on a single read)."""

    def sieve(self, want="explicit"):
        from test_screen import humanoid_sieve
        return humanoid_sieve(examples=[{"security_id": "NASDAQ:ROB2", "want": want, "source": "sieve",
                                         "pin": False}])

    def reads_of(self, item_id):
        l2 = [c for c in self.log if c.layer == "l2"]
        return [r for c in l2 for r, ids in c.reads_seen if item_id in ids]

    def test_unverified_should_pass_is_read_twice_more_and_flagged(self):
        from test_cli_calib import ROB2
        res = self.run_screen(reads=1, sieve=self.sieve(), jev_factory=band_factory(self.log))
        self.assertEqual(self.reads_of(ROB2), [0, 1, 2])
        row = next(r for r in res["rows"] if r["security_id"] == "NASDAQ:ROB2")
        self.assertEqual((row["l2_label"], row["l2_reads"]), ("partial", 3))
        self.assertEqual(row["l2_should_pass"], {"want": "explicit", "want_eff": "explicit", "capped": False,
                                                 "before": "insufficient", "after": "partial", "extra_reads": 2,
                                                 "planned": 2, "held": False})
        check = res["calibration"]["checks"][0]
        self.assertEqual((check["extra_reads"], check["flag_zh"]), (2, "应通过：多读 2 次，仍低于「明确符合」"))
        from jevscreen import report
        self.assertIn("应通过：多读 2 次，仍低于「明确符合」", report.render_markdown(res))
        self.assertIn("should_pass", report.format_console(res))

    def test_a_should_pass_below_its_level_after_the_band_reads_gets_two_more(self):
        from test_cli_calib import ROB2
        res = self.run_screen(reads=3, sieve=self.sieve(), jev_factory=band_factory(self.log))
        self.assertEqual(sorted(self.reads_of(ROB2)), [0, 1, 2, 3, 4])
        row = next(r for r in res["rows"] if r["security_id"] == "NASDAQ:ROB2")
        self.assertEqual((row["l2_label"], row["l2_reads"]), ("explicit", 5))
        self.assertEqual(row["l2_should_pass"]["held"], True)
        # a should_pass already at its level is not read again
        self.log.clear()
        res = self.run_screen(reads=3, sieve=self.sieve("partial"), jev_factory=band_factory(self.log),
                              out_dir=self.home / "o2")
        self.assertEqual(sorted(self.reads_of(ROB2)), [0, 1, 2])
        self.assertIsNone(next(r for r in res["rows"] if r["security_id"] == "NASDAQ:ROB2").get("l2_should_pass"))


L2_PROBS = {"explicit": {"explicit": 0.8, "partial": 0.1, "contradicted": 0.05, "insufficient": 0.05},
            "partial": {"explicit": 0.1, "partial": 0.8, "contradicted": 0.05, "insufficient": 0.05},
            "insufficient": {"explicit": 0.03, "partial": 0.04, "contradicted": 0.03, "insufficient": 0.9}}


class TestShouldPassEstimate(StoreCase):
    def test_dry_run_counts_the_should_pass_reads(self):
        from test_screen import humanoid_sieve
        sv = humanoid_sieve(examples=[{"security_id": "NASDAQ:ROB2", "want": "explicit", "source": "sieve",
                                       "pin": False}])
        plain = self.run_screen(dry_run=True, reads=1, sieve=humanoid_sieve(), out_dir=self.home / "d0")
        dry = self.run_screen(dry_run=True, reads=1, sieve=sv, out_dir=self.home / "d1")
        e0, e1 = plain["layers"]["l2"]["estimate"], dry["layers"]["l2"]["estimate"]
        self.assertEqual(e1["should_pass_reads"], {"items": 1, "extra_reads": 2, "est_cost_usd": 0.02})
        self.assertAlmostEqual(e1["est_cost_usd"], e0["est_cost_usd"] + 0.02)
        self.assertNotIn("should_pass_reads", e0)


class TestShouldPassProfileCap(StoreCase):
    """Review v1.1: a should_pass company read from its profile alone is judged on the CAPPED label (at most
    partial), against a wanted level capped at partial, with the reason 仅简介，最多算相关 in its flag."""
    SID = "TSE:6000"                  # ServoJP: profile only in the seed

    def run_with(self, label_of_read):
        sid, seen = self.SID, []

        class ReadJev(FakeJev):
            def classify(self, items, question):
                out = super().classify(items, question)
                if question.key != "fit":
                    by_id = {it.item_id: it for it in items}
                    for x in out:
                        it = by_id.get(x["item_id"])
                        if x["status"] == "ok" and it is not None and it.meta.get("security_id") == sid:
                            seen.append(question.read)
                            lab = label_of_read(question.read)
                            x.update(label=lab, probs=L2_PROBS[lab])
                return out

        def factory(cfg, *, run_id, layer, budget_usd, dry_run, **kw):
            return ReadJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=self.log)
        from test_screen import humanoid_sieve
        sv = humanoid_sieve(examples=[{"security_id": sid, "want": "explicit", "source": "sieve", "pin": False}])
        res = self.run_screen(reads=1, sieve=sv, jev_factory=factory, out_dir=self.home / f"o{len(self.log)}")
        row = next(r for r in res["rows"] + res.get("unverified", []) if r["security_id"] == sid)
        check = next(c for c in res["calibration"]["checks"] if c["security_id"] == sid)
        return res, row, check, seen

    def test_explicit_reads_of_a_profile_are_flagged_not_passed_silently(self):
        res, row, check, seen = self.run_with(lambda r: "explicit")
        self.assertEqual((row["l2_label"], row["l2_label_cap"]), ("partial", "仅简介"))
        self.assertEqual(seen, [0])                                        # at its (capped) level: no extra reads
        sp = row["l2_should_pass"]
        self.assertEqual((sp["want"], sp["want_eff"], sp["capped"], sp["extra_reads"], sp["held"]),
                         ("explicit", "partial", True, 0, True))
        self.assertEqual(check["flag_zh"], "应通过：仅简介，最多算相关")
        from jevscreen import report
        self.assertIn("仅简介，最多算相关", report.format_console(res))
        self.assertNotEqual(res["status"], "partial")

    def test_partial_reads_of_a_profile_are_not_read_again(self):
        _, row, check, seen = self.run_with(lambda r: "partial")
        self.assertEqual(seen, [0])
        self.assertEqual(row["l2_should_pass"]["held"], True)
        self.assertEqual(check["flag_zh"], "应通过：仅简介，最多算相关")

    def test_a_low_profile_read_is_read_again_and_judged_capped(self):
        _, row, check, seen = self.run_with(lambda r: "insufficient" if r == 0 else "explicit")
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual((row["l2_label"], row["l2_label_cap"]), ("partial", "仅简介"))
        sp = row["l2_should_pass"]
        self.assertEqual((sp["after"], sp["held"], sp["extra_reads"]), ("partial", True, 2))
        self.assertEqual(check["flag_zh"], "应通过：多读 2 次后通过（仅简介，最多算相关）")


SEA = {"category": "digital payments and e-wallets", "target": "Southeast Asia",
       "mechanism": "operates the payment service or wallet itself"}
SEA_ZH = {"category": "数字支付/电子钱包", "target": "东南亚", "mechanism": "自己运营支付服务或钱包"}
SMB = {"category": "payroll software", "target": "small businesses", "mechanism": "sells it to them"}


class TestChips(unittest.TestCase):
    """Fix 5: g / scope chips without facets (generic text, so --sieve none decks can say 只有大类), role chips h-k
    mapped to library rules, and rule templates per facet type (technology / geography / customer segment) so a
    geography target reads right ('offered in Southeast Asia', not 'uses Southeast Asia')."""

    def deck(self, sieve, n=4):
        cards = [{"n": i, "type": "boundary_in", "name": f"Co {i}", "security_id": f"NYSE:C{i}",
                  "quote": {"text": f"Co {i} text.", "matched_terms": []}} for i in range(1, n + 1)]
        return {"deck_id": "deck-r-1", "cards": cards, "chips": calib.deck_chips(sieve)}

    def test_g_without_facets(self):
        chips = calib.deck_chips(None)
        self.assertEqual(chips["no"]["g"], "只有大类，没提想法里的具体对象")
        deck = self.deck(None)
        ans = calib.parse_answers("1不要g", deck)
        self.assertEqual(ans[0].chip, "g")
        sv = calib.new_sieve("humanoid robot reducers")
        cand, dropped = calib.candidate_rules(sv, deck, ans)
        self.assertEqual(([c["id"] for c in cand], dropped), (["scope_narrow"], []))
        text = dict(((r, l), t) for r, l, t in calib.render_rules(["scope_narrow"], None))
        self.assertIn(("scope_narrow", "insufficient"), text)
        self.assertIn("broad category", text[("scope_narrow", "insufficient")])
        sv2, _ = calib.record_answers(sv, deck, ans, now="t")
        self.assertEqual(calib.validate_sieve(sv2), [])
        # the card itself offers every chip
        result, inputs, pool, sv = synthetic_run(target_terms=False, facets=False)
        card = calib.select_cards(result, inputs, sv, pool=pool, draws=200)[1]
        self.assertEqual(card["chips"], list("abcdefghijk"))

    def test_role_chips_map_to_library_rules(self):
        deck = self.deck(None)
        ans = calib.parse_answers("1不要h 2不要i 3不要j 4不要k", deck)
        cand, _ = calib.candidate_rules(calib.new_sieve("x"), deck, ans)
        self.assertEqual([c["id"] for c in cand], ["buyer_not_supplier", "holding_only", "upstream_parts",
                                                   "hardware_to_operators"])
        for rid in ("buyer_not_supplier", "holding_only", "upstream_parts", "hardware_to_operators"):
            with self.subTest(rule=rid):
                for facets in (None, SEA, SMB, {"category": "precision reducers", "target": "humanoid robot joints"}):
                    texts = calib.render_rules([rid], facets)
                    self.assertTrue(texts)
                    for _, _, t in texts:
                        self.assertNotIn("{", t)
                        self.assertEqual(calib.validate_rule_text(t), [])
        self.assertEqual(calib.parse_answers("其余不要k", deck)[0].chip, "k")
        md = calib.render_cards_md({**deck, "idea": "x", "cards": [
            {**c, "why_zh": "w", "what": {}, "quote": {"source": "公司简介"}, "verdict": {}} for c in deck["cards"]]})
        self.assertIn("h买方/客户", md)
        self.assertIn("k卖硬件给运营方", md)

    def test_templates_per_facet_type(self):
        self.assertEqual(calib.facet_type(SEA), "geography")
        self.assertEqual(calib.facet_type(SMB), "customer")
        self.assertEqual(calib.facet_type({"category": "x", "target": "AI agents"}), "technology")
        self.assertEqual(calib.facet_type({**SMB, "type": "technology"}), "technology")     # the sieve's own
        t = {(r, l): x for r, l, x in calib.render_rules(["user_not_supplier", "scope_narrow"], SEA)}
        self.assertNotIn("uses Southeast Asia", t[("user_not_supplier", "insufficient")])
        self.assertIn("without itself offering digital payments and e-wallets in Southeast Asia",
                      t[("user_not_supplier", "insufficient")])
        self.assertEqual(t[("scope_narrow", "explicit")],
                         "Explicit means digital payments and e-wallets offered in Southeast Asia.")
        self.assertEqual(t[("scope_narrow", "insufficient")],
                         "Offering digital payments and e-wallets only outside Southeast Asia is insufficient.")
        self.assertEqual(calib.facet_type({"category": "network security", "target": "east-west traffic"}),
                         "technology")
        c = {(r, l): x for r, l, x in calib.render_rules(["scope_narrow"], SMB)}
        self.assertEqual(c[("scope_narrow", "explicit")], "Explicit means payroll software offered to small businesses.")
        sv = {"facets": SEA, "facets_zh": SEA_ZH}
        self.assertEqual(calib.deck_chips(sv)["no"]["g"], "做数字支付/电子钱包，但不面向东南亚")
        live = {"category": "digital payments and e-wallets", "target": "consumers and merchants in Indonesia and "
                                                                        "Vietnam"}
        lt = {(r, l): x for r, l, x in calib.render_rules(["scope_narrow"], live)}
        self.assertEqual(calib.facet_type(live), "geography")
        self.assertEqual(lt[("scope_narrow", "explicit")], "Explicit means digital payments and e-wallets offered to "
                                                           "consumers and merchants in Indonesia and Vietnam.")
        # 'Asia' is a listed company name, but a geography word of the idea is not a company name
        text = t[("user_not_supplier", "insufficient")]
        self.assertEqual(calib.validate_rule_text(text, names=["Asia", "Grabbit Holdings Inc."]), [])
        self.assertEqual(calib.validate_sieve({**calib.new_sieve("x"), "facets": {**SEA, "type": "planet"}}),
                         ["facets.type must be one of technology, geography, customer"])

    def test_the_legend_says_what_g_means_on_this_deck(self):
        # review v1.1: on a geography / customer deck g means 'not for the target', not 'only the broad category'
        def md(sieve):
            deck = self.deck(sieve)
            return calib.render_cards_md({**deck, "idea": "x", "cards": [
                {**c, "why_zh": "w", "what": {}, "quote": {"source": "公司简介"}, "verdict": {}}
                for c in deck["cards"]]})
        sea = md({"facets": SEA, "facets_zh": SEA_ZH})
        self.assertIn("g不面向东南亚", sea)
        self.assertNotIn("g只有大类", sea)
        smb = md({"facets": SMB, "facets_zh": {"category": "薪资软件", "target": "小企业"}})
        self.assertIn("g不面向小企业", smb)
        long_target = md({"facets": {**SEA, "target": "consumers and merchants in Indonesia and Southeast Asia"},
                          "facets_zh": {**SEA_ZH, "target": "印度尼西亚和东南亚各国的消费者和商户"}})
        self.assertIn("g不面向印度尼西亚和东南亚各…", long_target)
        self.assertIn("g只有大类", md(None))
        self.assertIn("g只有大类", md({"facets": {"category": "precision reducers", "target": "humanoid robot joints"},
                                     "facets_zh": {"category": "精密减速机", "target": "人形机器人关节"}}))

    def test_a_demonym_or_a_place_inside_a_target_is_not_a_geography(self):
        # review v1.1: geography only when the target is a place (or a customer segment in a place)
        cases = {
            "traditional Chinese medicine makers": "customer",
            "Chinese EV makers": "customer",
            "Korean humanoid robot makers": "customer",
            "Japanese consumers": "customer",
            "data centers in the United States": "technology",
            "Korean players": "customer",
            "Indian": "technology",
            "AI agents": "technology",
            "Southeast Asia": "geography",
            "the United States": "geography",
            "Indonesia and Vietnam": "geography",
            "in Japan": "geography",
            "rural India": "geography",
            "Southeast Asian markets": "geography",
            "Latin America, Africa and the Middle East": "geography",
            "small businesses in Brazil": "geography",
            "hospitals": "customer",
        }
        for target, want in cases.items():
            with self.subTest(target=target):
                self.assertEqual(calib.facet_type({"category": "x", "target": target}), want)
        tcm = {(r, lab): x for r, lab, x in calib.render_rules(
            ["scope_narrow", "buyer_not_supplier"],
            {"category": "herbal extraction equipment", "target": "traditional Chinese medicine makers"})}
        self.assertEqual(tcm[("scope_narrow", "explicit")],
                         "Explicit means herbal extraction equipment offered to traditional Chinese medicine makers.")
        self.assertEqual(tcm[("scope_narrow", "partial")],
                         "Partial means the company offers herbal extraction equipment to traditional Chinese medicine "
                         "makers, but as a small or early part of its business.")
        self.assertTrue(tcm[("buyer_not_supplier", "insufficient")].startswith(
            "A company that is itself one of the traditional Chinese medicine makers"))
        # no rule text of any type tells Jev that selling cards, chips or terminals is insufficient
        for facets in (SEA, SMB, {"category": "LLM inference chips", "target": "data centers in the United States"},
                       {"category": "LLM inference chips", "target": "the United States", "type": "geography"}, None):
            rules = [r if r != "homonym" else {"id": r, "terms": ["x"]} for r in calib.RULES]
            for _, _, text in calib.render_rules(rules, facets):
                with self.subTest(facets=facets, text=text):
                    own = text.replace((facets or {}).get("category") or "\0", "")
                    self.assertNotRegex(own, r"\b(?:cards|chips|terminals|readers)\b")
                    self.assertNotRegex(text, r"\bone of (?!the )")
                    self.assertNotIn("{", text)


if __name__ == "__main__":
    unittest.main()
