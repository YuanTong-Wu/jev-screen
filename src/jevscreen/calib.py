"""Calibration (sieve) for jev-screen: calibration cards, answers, pins, rule trials and keyword learning.

The pieces, all free except trial_rules (a few cents of L2 reads):
- the sieve file (load_sieve / validate_sieve / save_sieve: atomic, version-checked);
- inclusion_probability (NOISE_SD_BANDS) and select_cards: a deterministic deck of about 8 yes/no cards, rendered as
  cards.json (jevscreen.cards/1) and cards.md (Chinese);
- parse_answers ("1要a 2不要c 3?"), record_answers / undo_example (sieve.examples, history);
- apply_pins (screen's ranking step) and rerank_result (the same code on a finished run: the free stage A diff),
  render_diff_zh;
- RULES / render_rules / validate_rule_text / candidate_rules / trial_rules / adopt_rules;
- background_df (cached per source), noisy_terms, mine_terms, peer_preview, merge_keywords.

Rules (see docs/DATA_RULES.md "Calibration (sieve)"):
- The sieve file data/sieves/<keywords.idea_key(idea)>.json is the single source of truth; the user's AI may edit it.
- Pins are judgments, not evidence: a pinned yes is listed and scored with the user's label but keeps its evidence
  label, with verdict_source 'evidence+user' (the evidence agrees) or 'user' (it does not: 用户判断（年报未写明）).
  A pinned no is removed from the ranking and shown under 你排除的, never silently dropped.
- Examples with source 'sieve' (should_pass / should_fail written by the user's AI) are checks, never pins.
- Only library rule texts (RULES, rendered with the sieve's facets) ever enter a Jev question; the user's free text
  never does. validate_rule_text keeps company names, tickers and card quotes out of them.
- Nothing numeric is fitted: rules are adopted only through the trial gate, keywords only through the precision
  gates and the peer preview.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import heapq
import json
import math
import os
import random
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple

from . import screen, zhvariants

SIEVE_FORMAT = "jevscreen.sieve/1"
CARDS_FORMAT = "jevscreen.cards/1"
WANTS = ("explicit", "partial", "no", "unsure")
PIN_WANTS = ("explicit", "partial", "no")
USER_ONLY_NOTE = "用户判断（年报未写明）"
LANGS = screen.LANGS

# ---------------------------------------------------------------------------------------------------------------
# Rule library: id -> chip, the criteria its sentences attach to, templates with facet slots ({category}, {target},
# {mechanism}; {terms} for homonym) and the generic fallback used when the sieve has no facets (None: the rule is off
# without facets). A template dict maps an L2 label to its sentence.
RULES: dict[str, dict[str, Any]] = {
    "user_not_supplier": {
        "version": 1, "chip": "c",
        "template": {"insufficient": "A company that only builds, sells or uses {target} (including inside its own "
                                     "operations or security services) without offering {category} for them is "
                                     "insufficient."},
        "generic": {"insufficient": "A company that only builds, sells or uses the technology the idea is about, "
                                    "without offering what the idea describes, is insufficient."}},
    "homonym": {
        "version": 1, "chip": "d",
        "template": {"insufficient": "Terms such as {terms} count only when they mean what the idea means; the same "
                                     "words used for something else (e.g. agreement management, consumer identity "
                                     "verification, credit data, ISO/ISMS certification) are not evidence."},
        "generic": {"insufficient": "Words of the idea count only when they mean what the idea means; the same words "
                                    "used for something else (e.g. agreement management, consumer identity "
                                    "verification, credit data, ISO/ISMS certification) are not evidence."}},
    "laundry_list": {
        "version": 1, "chip": "e",
        "template": {"insufficient": "A service provider, reseller or distributor that lists the idea among many "
                                     "offerings without describing its own product for it is insufficient."},
        "generic": None},
    "mention_only": {
        "version": 1, "chip": "f",
        "template": {"insufficient": "A single mention in a strategy, outlook, market-trend or risk sentence, or an "
                                     "activity that is only planned, is insufficient."},
        "generic": None},
    "scope_narrow": {
        "version": 1, "chip": "g",
        "template": {"explicit": "Explicit means {category} applied to {target}.",
                     "partial": "Partial means such a link between {category} and {target} exists but is small or "
                                "early.",
                     "insufficient": "{category} without {target} is insufficient."},
        "by_type": {
            "geography": {"explicit": "Explicit means {category} offered {target_in}.",
                          "partial": "Partial means the company offers {category} {target_in}, but as a small or "
                                     "early part of its business.",
                          "insufficient": "Offering {category} only {target_out} is insufficient."},
            "customer": {"explicit": "Explicit means {category} offered to {target}.",
                         "partial": "Partial means the company offers {category} to {target}, but as a small or "
                                    "early part of its business.",
                         "insufficient": "Offering {category} only to customers other than {target} is "
                                         "insufficient."}},
        "generic": {"explicit": "Explicit means the company offers the specific thing the idea describes, not only "
                                "the broad category it belongs to.",
                    "insufficient": "A company that offers only the broad category, without the specific thing the "
                                    "idea describes, is insufficient."}},
    "scope_broad": {
        "version": 1, "chip": None, "facets_only": True,
        "template": {"partial": "Partial includes the company's own {category} products even when {target} are not "
                                "mentioned."},
        "by_type": {
            "geography": {"partial": "Partial includes the company's own {category} business even when {target} is "
                                     "not mentioned."}},
        "generic": None},
    # role chips (v1.1): who the company is to the idea
    "buyer_not_supplier": {
        "version": 1, "chip": "h",
        "template": {"insufficient": "A company that buys or uses {category} for {target} from others (a customer, "
                                     "end user or operator of it) rather than supplying it is insufficient."},
        "by_type": {
            "geography": {"insufficient": "A company that only buys or uses {category} from others (a customer or "
                                          "end user of it) rather than offering it {target_in} is insufficient."},
            "customer": {"insufficient": "A company that is itself one of {the_target} and only buys or uses "
                                         "{category} rather than offering it is insufficient."}},
        "generic": {"insufficient": "A company that buys or uses what the idea describes from others (a customer, "
                                    "end user or operator of it) rather than supplying it is insufficient."}},
    "holding_only": {
        "version": 1, "chip": "i",
        "template": {"insufficient": "A company that only holds a stake in, invests in or lends to a company that "
                                     "does what the idea describes, without doing it itself, is insufficient."},
        "generic": None},
    "upstream_parts": {
        "version": 1, "chip": "j",
        "template": {"insufficient": "A supplier of generic parts, materials, components or production equipment "
                                     "used to make {category} for {target}, whose own products are not {category}, is "
                                     "insufficient."},
        "by_type": {
            "geography": {"insufficient": "A supplier of generic parts, materials, components or equipment used by "
                                          "providers of {category}, whose own products are not {category}, is "
                                          "insufficient."},
            "customer": {"insufficient": "A supplier of generic parts, materials or components used to make "
                                         "{category} for {target}, whose own products are not {category}, is "
                                         "insufficient."}},
        "generic": {"insufficient": "A supplier of generic parts, materials, components or production equipment used "
                                    "to make what the idea describes, whose own products are not that thing, is "
                                    "insufficient."}},
    "hardware_to_operators": {
        "version": 1, "chip": "k",
        "template": {"insufficient": "A maker of hardware or devices sold to the companies that provide {category} "
                                     "for {target}, which does not provide {category} itself, is insufficient."},
        "by_type": {
            "geography": {"insufficient": "A maker of hardware or devices sold to the companies that offer "
                                          "{category}, which does not itself offer {category} {target_in}, is "
                                          "insufficient."},
            "customer": {"insufficient": "A maker of hardware or devices sold to the companies that offer {category} "
                                         "to {target}, which does not itself offer {category} to {target}, is "
                                         "insufficient."}},
        "generic": {"insufficient": "A maker of hardware or devices sold to the companies that provide what the "
                                    "idea describes, which does not provide it itself, is insufficient."}},
}
RULES["user_not_supplier"]["by_type"] = {
    "geography": {"insufficient": "A company that only supplies technology, equipment or components to the "
                                  "providers of {category}, or only uses {category} in its own operations, without "
                                  "itself offering {category} {target_in}, is insufficient."},
    "customer": {"insufficient": "A company that only builds technology for {category} or only uses it in its own "
                                 "operations, without offering {category} to {target}, is insufficient."}}
RULES["laundry_list"]["generic"] = RULES["laundry_list"]["template"]    # no facet slots: the same sentence
RULES["mention_only"]["generic"] = RULES["mention_only"]["template"]
RULES["holding_only"]["generic"] = RULES["holding_only"]["template"]
EXCLUSIVE_RULES = frozenset({"scope_narrow", "scope_broad"})           # the newest one wins
CHIP_RULES = {spec["chip"]: rid for rid, spec in RULES.items() if spec.get("chip")}   # c d e f g h i j k -> rule id
FACET_TYPES = ("technology", "geography", "customer")
_GEO_PHRASES = tuple(x.strip() for x in """asia, europe, africa, americas, north america,
south america, latin america, latam, asia pacific, apac, emea, mena, asean, nordics, scandinavia, middle east,
southeast asia, south asia, east asia, central asia, eastern europe, western europe, china, japan,
korea, south korea, india, indonesia, vietnam, thailand, malaysia,
philippines, singapore, taiwan, hong kong, australia, new zealand, brazil, mexico, argentina, chile, colombia, peru,
canada, germany, france, united kingdom, uk, britain, italy, spain, poland, turkey, israel, saudi arabia, egypt, nigeria,
kenya, south africa, united states, us, usa, gulf states, gcc""".replace("\n", " ").split(","))
# demonyms: a place only in front of 'markets' / 'countries' / 'region' ('Southeast Asian markets'), never as the
# adjective of another noun ('Chinese EV makers' is a customer segment, 'traditional Chinese medicine makers' too)
_GEO_DEMONYMS = tuple(x.strip() for x in """asian, southeast asian, east asian, south asian, european, african,
latin american, north american, middle eastern, nordic, chinese, japanese, korean, indian, indonesian, vietnamese,
thai, malaysian, filipino, australian, brazilian, mexican, canadian, german, french, british, american,
gulf""".replace("\n", " ").split(","))
_GEO_WORDS = frozenset(_GEO_PHRASES + _GEO_DEMONYMS)   # also: a company named like a region ('Asia') is not a leak


def _alt(phrases: Iterable[str]) -> str:
    return "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in sorted(phrases, key=len, reverse=True))


_PLACE = (r"(?:the\s+)?(?:(?:rural|urban|mainland|greater|northern|southern|eastern|western|central|coastal)\s+)?"
          rf"(?:{_alt(_GEO_PHRASES)}|(?:{_alt(_GEO_DEMONYMS)})\s+(?:markets?|countries|region|economies))")
_PLACES = rf"{_PLACE}(?:\s*(?:,\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+|\s*[&/+]\s*){_PLACE})*"
_GEO_TARGET = re.compile(rf"^\s*(?:(?:in|across|throughout|within)\s+)?{_PLACES}(?:\s+(?:markets?|region))?\s*$",
                         re.IGNORECASE)
_IN_PLACES = re.compile(rf"^(?P<head>.+?)\s+(?:in|across|throughout|within)\s+{_PLACES}\s*$", re.IGNORECASE)
_CUSTOMER_TARGET = re.compile(r"\b(?:customers?|clients?|businesses|companies|firms|smes?|smbs?|msmes?|merchants?|"
                              r"consumers?|households?|enterprises|hospitals?|clinics?|pharmacies|banks?|insurers|"
                              r"lenders|brokers|governments?|municipalities|schools?|universities|farmers?|"
                              r"retailers?|restaurants?|patients?|employers?|landlords?|homeowners|freelancers?|"
                              r"developers|users|gamers|players|investors|shoppers|travell?ers|drivers|students|"
                              r"makers|manufacturers|producers|automakers|oems|operators|carriers|utilities)\b",
                              re.IGNORECASE)


def facet_type(facets: dict[str, str] | None) -> str | None:
    """What kind of target the facets name: the sieve's own facets.type when set (the AI drafting the sieve should
    set it), else detected: 'geography' when the target is a place ('Southeast Asia', 'Indonesia and Vietnam', 'in
    Japan', 'Southeast Asian markets') or a customer segment in a place ('consumers and merchants in Indonesia');
    'customer' when it names a customer segment ('small businesses', 'Chinese EV makers': a demonym is never a
    place by itself); else 'technology' ('data centers in the United States' too). None without facets. It picks
    the rule template (RULES by_type)."""
    if not (isinstance(facets, dict) and facets.get("category") and facets.get("target")):
        return None
    if facets.get("type") in FACET_TYPES:
        return facets["type"]
    target = facets.get("target") or ""
    if _GEO_TARGET.match(target):
        return "geography"
    m = _IN_PLACES.match(target)
    if m and _CUSTOMER_TARGET.search(m.group("head")):
        return "geography"
    if _CUSTOMER_TARGET.search(target):
        return "customer"
    return "technology"

# ---------------------------------------------------------------------------------------------------------------
# Cards

# L2 read noise: (upper bound of p̄, sd of one read's p_pos), measured on 75 paired re-reads. sd(p̄) = sd / sqrt(n).
NOISE_SD_BANDS: tuple[tuple[float, float], ...] = ((0.1, 0.023), (0.3, 0.069), (0.7, 0.133), (0.9, 0.105),
                                                   (1.01, 0.034))
PI_DRAWS = 2000              # draws of inclusion_probability
PI_CUT = 0.5                 # a draw counts as verified when p̃ >= this
CARDS_MAX = 8
# slot type -> the most cards of that type; filled in this order (a company is never picked twice)
CARD_SLOTS: tuple[tuple[str, int], ...] = (("recheck", 2), ("top_check", 1), ("scope", 1), ("boundary_in", 4),
                                           ("boundary_out", 1), ("gap", 1))
TOP_CHECK_RANK = 5           # top_check: rank <= this, label explicit and p̄_explicit - p̄_partial < TOP_CHECK_MARGIN
TOP_CHECK_MARGIN = 0.20
SCOPE_MIN_P = 0.75           # scope: a row in the output with p̄ >= this whose L2 text matches no target term
BOUNDARY_MIN_V = 0.3         # boundary_in: v = 4π(1-π) x BOUNDARY_LOW_BOOST (when p̄ < BOUNDARY_LOW_P) > this
BOUNDARY_LOW_P, BOUNDARY_LOW_BOOST = 0.6, 1.5
STRATUM_MAX = 2              # boundary_in cards per evidence stratum (each official source, or profile)
GAP_L1_MIN = 0.70            # gap: L1 P(core) + P(adjacent) >= this
QUOTE_MAX_CHARS = 200
WHAT_MAX_CHARS = 160
EMPTY_DECK_ZH = "这次没有需要你判断的卡"
YES_CHIPS, NO_CHIPS = ("a", "b"), ("c", "d", "e", "f", "g", "h", "i", "j", "k")
NO_CHIP_TEXT = {"c": "只是做/用这项技术，不卖想法里的东西", "d": "词对上了，意思不一样", "e": "泛泛的服务/转售，罗列很多",
                "f": "只是一句展望/计划", "h": "是买方/客户/用户，不是供应方", "i": "只是持股/投资，自己不做",
                "j": "上游通用零件/材料/设备，自己的产品不是这个", "k": "卖硬件/设备给运营方，自己不运营"}
LEGEND_NO = {"c": "c只是做/用", "d": "d词对意思不对", "e": "e泛泛服务/转售", "f": "f一句展望", "g": "g只有大类",
             "h": "h买方/客户", "i": "i只持股", "j": "j上游零件设备", "k": "k卖硬件给运营方"}
G_CHIP_GENERIC_ZH = "只有大类，没提想法里的具体对象"
WHY_ZH = {
    "top_check": "排第{rank}，「直接」「相关」五五开",
    "scope": "只有大类、没提{target}：这一类算不算？",
    "boundary_in": "在名单里，但多读几次可能掉出去（入选 {pi:.0%}）",
    "boundary_out": "{src}判为相关/明确（合计 {p:.0%}），但排在 {max_out} 名外",
    "gap": "简介相关，年报摘录没写（缺口）。你知道它做的话可以直接定",
    "recheck": "你上次答过，但年报换了新版本",
    "recheck_excerpt": "你上次答过，但系统读的年报摘录变了",   # an answer recorded without its filing identity
    "recheck_profile": "你上次答时只有公司简介，现在有了年报原文",   # e.g. fetched on demand since (jevscreen.ondemand)
}
# English twins of the card texts (the result page in English; the keys match WHY_ZH / NO_CHIP_TEXT exactly)
WHY_EN = {
    "top_check": "Ranked {rank}; 'direct' and 'related' are about even",
    "scope": "Only the broad category, no mention of {target}: does this kind count?",
    "boundary_in": "On the list, but re-reading could drop it (in {pi:.0%} of draws)",
    "boundary_out": "The {src} reads related/direct ({p:.0%} together), but it ranks below {max_out}",
    "gap": "The profile looks related; the annual-report excerpt does not say so (a gap). Decide if you know",
    "recheck": "You answered this before, but there is a newer annual report",
    "recheck_excerpt": "You answered this before, but the excerpt the system read has changed",
    "recheck_profile": "You answered when only the company profile was there; now the annual report is",
}
NO_CHIP_TEXT_EN = {"c": "Only makes/uses the technology; does not sell what the idea is about",
                   "d": "The words match, the meaning does not", "e": "Generic service/resale in a long list",
                   "f": "Only an outlook or plan sentence", "h": "A buyer/customer/user, not a supplier",
                   "i": "Only holds shares/invests; does not do it itself",
                   "j": "Upstream generic parts/materials/equipment; its own product is not this",
                   "k": "Sells hardware/equipment to operators; does not operate itself"}
YES_CHIP_TEXT_EN = {"a": "Does it directly ({mechanism})", "b": "Related; counts as the broad category"}
NO_CHIP_G_EN = "Only the broad category ({category}), no mention of {target}"
NO_CHIP_G_EN_PLACE = "Does {category}, but not for {target}"          # geography / customer facets (deck_chips)
G_CHIP_GENERIC_EN = "Only the broad category, with no mention of what the idea is about"


def g_chip_en(facets: dict[str, Any] | None) -> str:
    """The English twin of deck_chips' g text for the (English) facets."""
    kind = facet_type(facets)
    if kind is None:
        return G_CHIP_GENERIC_EN
    tpl = NO_CHIP_G_EN_PLACE if kind in ("geography", "customer") else NO_CHIP_G_EN
    return tpl.format(category=facets.get("category"), target=facets.get("target"))

# how a filing's form is named on a card ('（CNINFO 年报摘要，2026 年发布）'); other forms are shown as they are
FORM_ZH = {"annual_report_summary": "年报摘要", "annual_report": "年报", "10-K": "10-K 年报",
           "10-KT": "10-K 年报（过渡期）", "20-F": "20-F 年报", "40-F": "40-F 年报",
           "有価証券報告書": "有价证券报告书（年报）", "사업보고서": "事业报告（年报）", "股東會年報": "股东会年报"}
NO_REPORT_ZH = "（没有年报文本，系统只读了公司简介）"
LABEL_ZH = {"explicit": "明确符合", "partial": "相关", "insufficient": "证据不足", "contradicted": "年报否认"}
LICENCE_GRAY_ZH = ("仅供个人使用：部分「做什么」来自 TradingView / FinanceDatabase 公司简介（gray-private），"
                   "本文件不得公开、分享或转发。")
LICENCE_OFFICIAL_ZH = "年报摘录（SEC、CNINFO、EDINET、DART、MOPS、BSE；official-private）是发行人原文：原文摘录只留在本地。"

# ---------------------------------------------------------------------------------------------------------------
# Rule trials and keywords

TRIAL_READS = (0, 1)         # read indices each trial item gets under q'
TRIAL_MAX = 5
TRIAL_ANCHORS = 10           # anchors: top verified companies by p̄ (>= TRIAL_ANCHOR_MIN_P, with a keyword hit)
TRIAL_ANCHOR_MIN_P = 0.85
TRIAL_ANCHOR_MAX_DROPS = 2
TRIAL_EDGE = (0.40, 0.60)    # a would-be casualty with its base or trial p̄ here is re-read before it may veto
TRIAL_HIGH_SD = 0.15         # ... or with a base read sd / a spread of its trial reads at least this
TRIAL_REREADS = (2, 3)       # the two extra read indices of a boundary casualty (under the trial's question)
CONTROL_READS = (900, 901)   # the control read's indices: never a base run's (0, band 1..K-1, should_pass reads), so
#                              the control is a fresh read of the question without the candidates, not the base
#                              run's own answers replayed from the item cache (the key does not depend on the packet)
IN_SAMPLE_NOTE_ZH = "一致度是用你的回答算的（样本内），不是独立验证"

NOISY_DF_SHARE = 0.05        # a seed in >= 5% of a source's filings becomes weak (down-weighted, not deleted)
MINE_WINDOW = 150            # candidates are mined within +-150 chars of a current-term hit
MINE_MIN_YES = 3             # (a) in the filings of at least min(3, number of yes companies) distinct yes companies
MINE_MAX_DF_SHARE, MINE_MAX_DF_MIN = 0.01, 3   # (b) df <= max(3, 1% of N)
MINE_MIN_LIFT = 3.0          # (c) lift >= 3
MINE_YES_P = 0.8             # yes_docs: companies answered yes, plus companies verified with p̄ >= this
# the background corpus of a language's keywords: the FIRST official source of that language (zh = CNINFO: MOPS is
# also zh, but a smaller Traditional-script corpus, and the zh keywords are Simplified)
LANG_SOURCE: dict[str, str] = {}
for _src, (_label, _lang) in screen.OFFICIAL_DOC_SOURCES.items():
    LANG_SOURCE.setdefault(_lang, _src)
del _src, _label, _lang
LANG_ZH = {"en": "英文", "zh": "中文", "ja": "日文", "ko": "韩文"}
CORPUS_ZH = {"en": "美股年报", "zh": "A股年报", "ja": "日本年报", "ko": "韩国年报"}
_MINE_SUFFIX = {"ja": ("認証", "認可", "管理", "制御", "権限"), "zh": ("认证", "授权", "管理", "管控", "权限"),
                "ko": ("인증", "권한", "관리", "제어")}
_KATAKANA_RUN = re.compile(r"[ァ-ヺー]{4,}")
# Generic acronyms (IT plumbing, business, regulators, filing types): never mined unless the run seeded them. Mined
# acronyms also need >= MINE_ACRONYM_MIN letters unless seeded (SQL, WEB, SDK, VPN, POS, APT, SOC, MSS, WAF ...).
MINE_ACRONYM_MIN = 4
MINE_GATE_VERSION = 2        # the gates a mined / replacement add passed (log 'gate'); older adds are re-gated
LEARNED_SOURCES = ("mined", "replacement")
GENERIC_ACRONYMS = frozenset("""
HTTP HTTPS HTML JSON XML SOAP REST SAAS PAAS IAAS CRM ERP SCM MES PLM OEM ODM EMS IDC CDN LAN WAN WLAN WIFI LTE SDK
API APIS APP APPS WEB SQL VPN POS APT SOC MSS WAF IOT AIOT ICT CPU GPU SSD DRAM NAND USB LED LCD OLED ESG IPO CEO CFO
CTO COO KPI ROE ROI EPS GDP CAGR EBIT EBITDA IFRS GAAP ISO ISMS GMP GLP GSP FDA EMA MHRA AIFA PMDA NMPA CFDA MFDS KFDA
TGA ANVISA WHO NIH CDE HSA BPOM CEP DMF VMF CMC ANDA NDA BLA IND CRO CMO SME SMES MSME ETF REIT EPC NFC RFID PDF
PROTAC ADC AIDD CGT PDC XDC AOC RNAI SIRNA MRNA
""".split())
# katakana words of company names (subsidiaries named in a filing), never a keyword
_CORPORATE_KATAKANA = frozenset("""インコーポレイテッド インク エルエルシー リミテッド カンパニー コーポレーション
ホールディングス ホールディング グループ ゲーエムベーハー ピーエルシー エスエー エービー エルピー""".split())
# Han characters that end words (suffixes, particles): a mined term never starts with one ('化运维管理')
_BOUND_START = frozenset("化性式型率度者们等的了着过地得之其所")
_ZH_INTERIOR_STOP = frozenset("的和")       # a Chinese term does not run across 的 / 和 ('经销商的管理', '配置和管理')
_ACRONYM_RUN = re.compile(r"(?<![A-Za-z])[A-Z]{2,6}(?![A-Za-z])")
_PRODUCT_NAME = re.compile(r"[「『]([^」』\n]{1,40})[」』]")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------------------------
# Sieve file

class SieveStale(ValueError):
    """The sieve on disk changed since it was read (its version moved on): re-read it and apply again."""


def sieve_path(cfg, idea: str) -> Path:
    """data/sieves/<idea_key>.json: the same key as the keyword cache (sha256(idea.strip())[:16])."""
    from . import keywords
    return Path(cfg.home) / "sieves" / f"{keywords.idea_key(idea)}.json"


def new_sieve(idea: str, *, now: str | None = None) -> dict[str, Any]:
    """An empty sieve (version 0: not written yet) for an idea."""
    from . import keywords
    now = now or now_iso()
    return {"format": SIEVE_FORMAT, "version": 0, "idea": idea.strip(), "idea_key": keywords.idea_key(idea),
            "created_at": now, "updated_at": now, "facets": None, "facets_zh": None, "target_terms": None,
            "keywords": {}, "rules": [], "rejected_rules": [], "examples": [], "history": [], "decks": []}


def _str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def validate_sieve(data: Any) -> list[str]:
    """The problems of a sieve document (empty when valid): the jevscreen.sieve/1 schema, known rule ids, facets
    (category, target, an optional type of FACET_TYPES), examples with a company, a known want / source and a chip
    that fits the want (a-b yes, c-k no)."""
    if not isinstance(data, dict):
        return ["the sieve must be a JSON object"]
    errs: list[str] = []
    if data.get("format") != SIEVE_FORMAT:
        errs.append(f"format must be {SIEVE_FORMAT!r}")
    v = data.get("version", 0)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        errs.append("version must be an integer >= 0")
    for k in ("idea", "idea_key", "created_at", "updated_at", "idea_en"):
        if data.get(k) is not None and not isinstance(data[k], str):
            errs.append(f"{k} must be a string")
    facets = data.get("facets")
    if facets is not None:
        if not isinstance(facets, dict) or not all(isinstance(x, str) for x in facets.values()):
            errs.append("facets must be null or an object of strings")
        elif not (facets.get("category", "").strip() and facets.get("target", "").strip()):
            errs.append("facets needs non-empty 'category' and 'target'")
        elif facets.get("type") is not None and facets["type"] not in FACET_TYPES:
            errs.append(f"facets.type must be one of {', '.join(FACET_TYPES)}")
    fz = data.get("facets_zh")
    if fz is not None and (not isinstance(fz, dict) or not all(isinstance(x, str) for x in fz.values())):
        errs.append("facets_zh must be null or an object of strings")
    tt = data.get("target_terms")
    if tt is not None:
        if not isinstance(tt, dict):
            errs.append("target_terms must be null or an object {lang: [terms]}")
        else:
            for lang, terms in tt.items():
                if lang not in LANGS or not _str_list(terms):
                    errs.append(f"target_terms.{lang}: the language must be one of {', '.join(LANGS)} and the "
                                "value a list of strings")
    kw = data.get("keywords")
    if kw is not None:
        if not isinstance(kw, dict):
            errs.append("keywords must be an object {lang: {add, weak, log}}")
        else:
            for lang, x in kw.items():
                if lang not in LANGS or not isinstance(x, dict):
                    errs.append(f"keywords.{lang}: the language must be one of {', '.join(LANGS)} and the value an "
                                "object")
                    continue
                for part in ("add", "weak"):
                    if x.get(part) is not None and not _str_list(x[part]):
                        errs.append(f"keywords.{lang}.{part} must be a list of strings")
                if x.get("log") is not None and not isinstance(x["log"], list):
                    errs.append(f"keywords.{lang}.log must be a list")
    for k in ("examples", "rules", "rejected_rules", "history", "decks"):
        if data.get(k) is not None and not isinstance(data[k], list):
            errs.append(f"'{k}' must be a list")
    for i, r in enumerate(data.get("rules") or [] if isinstance(data.get("rules"), list) else []):
        rid = r if isinstance(r, str) else r.get("id") if isinstance(r, dict) else None
        if rid not in RULES:
            errs.append(f"rules[{i}]: unknown rule {rid!r} (known: {', '.join(sorted(RULES))})")
        elif isinstance(r, dict) and r.get("terms") is not None and not _str_list(r["terms"]):
            errs.append(f"rules[{i}].terms must be a list of strings")
    for i, ex in enumerate(data.get("examples") or [] if isinstance(data.get("examples"), list) else []):
        if not isinstance(ex, dict):
            errs.append(f"examples[{i}] must be an object")
            continue
        if not (ex.get("security_id") or ex.get("company_key")):
            errs.append(f"examples[{i}] needs security_id or company_key")
        want, chip = ex.get("want"), ex.get("chip")
        if want not in WANTS:
            errs.append(f"examples[{i}].want must be one of {', '.join(WANTS)}")
        if ex.get("source", "card") not in ("card", "sieve"):
            errs.append(f"examples[{i}].source must be 'card' or 'sieve'")
        if chip is not None:
            if chip not in YES_CHIPS + NO_CHIPS:
                errs.append(f"examples[{i}].chip must be one of a-k")
            elif (chip in YES_CHIPS) != (want in ("explicit", "partial")) or want == "unsure":
                errs.append(f"examples[{i}].chip {chip!r} does not fit want {want!r}")
    from . import sieve_author
    errs += sieve_author.field_problems(data)       # author fields (should_pass / should_fail / idea_en / ...)
    return errs


def load_sieve(path: str | Path) -> dict[str, Any] | None:
    """The sieve at `path`, or None when the file does not exist. Raises ValueError for unreadable JSON or a
    document that fails validate_sieve (missing lists are filled with [])."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"sieve {p}: unreadable ({type(e).__name__})") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"sieve {p}: invalid JSON ({str(e)[:120]})") from None
    if not isinstance(data, dict) or data.get("format") != SIEVE_FORMAT:
        raise ValueError(f"sieve {p}: not a {SIEVE_FORMAT} document")
    errs = validate_sieve(data)
    if errs:
        raise ValueError(f"sieve {p}: " + "; ".join(errs[:8]))
    for k in ("examples", "rules", "rejected_rules", "history", "decks"):
        if data.get(k) is None:
            data[k] = []
    data.setdefault("version", 0)
    from . import sieve_author
    data["examples"] = data["examples"] + sieve_author.author_examples(data)    # checks as examples (_author)
    return data


@contextlib.contextmanager
def _file_lock(path: Path):
    """An exclusive lock on <path>.lock (POSIX flock; a no-op where fcntl is missing) around check-and-replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:          # pragma: no cover - non-POSIX
        yield
        return
    with open(path.with_name(path.name + ".lock"), "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def save_sieve(path: str | Path, sieve: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
    """Write `sieve` atomically (tmp file + os.replace, as keywords._write_cache does) and return the written copy
    with version + 1 and updated_at set.

    Version check: sieve['version'] is the version it was read at; when the file on disk has another version (some
    other writer saved in between) nothing is written and SieveStale (请重新读取) is raised. ValueError when the new
    document fails validate_sieve."""
    from . import sieve_author
    p = Path(path)
    now = now or now_iso()
    base = sieve.get("version") or 0
    out = {**sieve, "format": SIEVE_FORMAT, "version": base + 1, "updated_at": now,
           "examples": sieve_author.strip_author(sieve.get("examples") or [])}
    out.setdefault("created_at", now)
    errs = validate_sieve(out)
    if errs:
        raise ValueError(f"sieve {p}: " + "; ".join(errs[:8]))
    with _file_lock(p):
        try:
            disk = load_sieve(p)
        except ValueError:
            disk = {"version": None}
        if disk is not None and (disk.get("version") or 0) != base:
            raise SieveStale(f"校准文件 {p} 已被别处改动（磁盘版本 {disk.get('version')}，你读到的是 {base}）："
                             "请重新读取后再保存")
        tmp = p.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.replace(tmp, p)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
    return {**out, "examples": out["examples"] + sieve_author.author_examples(out)}


def sieve_sha256(sieve: dict[str, Any] | None, path: str | Path | None = None) -> str | None:
    """sha256 of the sieve file's bytes (or of the canonical JSON of an in-memory sieve)."""
    if sieve is None:
        return None
    if path is not None:
        try:
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            pass
    body = json.dumps(sieve, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "").lower())
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def idea_similarity(a: str, b: str) -> float:
    """Character-bigram Jaccard similarity of two ideas (NFKC, lower case, whitespace removed)."""
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if (x or y) else 0.0


def closest_sieve(cfg, idea: str, *, min_similarity: float = 0.6) -> dict[str, Any] | None:
    """The existing sieve of the most similar OTHER idea (idea_similarity >= min_similarity) - a reworded idea gets a
    new key, so screen prints this hint and the AI can pass --sieve PATH. {'path', 'idea', 'similarity'} or None."""
    own = sieve_path(cfg, idea)
    best = None
    for p in sorted(own.parent.glob("*.json")) if own.parent.is_dir() else []:
        if p.name == own.name:
            continue
        try:
            sv = load_sieve(p)
        except ValueError:
            continue
        if not sv or not sv.get("idea"):
            continue
        sim = idea_similarity(idea, sv["idea"])
        if sim >= min_similarity and (best is None or sim > best["similarity"]):
            best = {"path": str(p), "idea": sv["idea"], "similarity": round(sim, 3)}
    return best


def _match_key(ex: dict[str, Any]) -> str | None:
    return ex.get("company_key") or ex.get("security_id")


def pins(sieve: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """{company_key or security_id: example} of the pinning examples (source 'card', pin true, want explicit /
    partial / no). A later example for the same company wins."""
    out: dict[str, dict[str, Any]] = {}
    for ex in (sieve or {}).get("examples") or []:
        if not isinstance(ex, dict) or ex.get("source", "card") != "card" or not ex.get("pin"):
            continue
        if ex.get("want") in PIN_WANTS and _match_key(ex):
            for k in {ex.get("company_key"), ex.get("security_id")} - {None}:
                out[k] = ex
    return out


def forced_examples(sieve: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The examples whose companies layer 2 always reads (l2_forced): pins and source-'sieve' checks."""
    out = []
    for ex in (sieve or {}).get("examples") or []:
        if not isinstance(ex, dict) or not _match_key(ex):
            continue
        if ex.get("source") == "sieve" or (ex.get("source", "card") == "card" and ex.get("pin")
                                           and ex.get("want") in PIN_WANTS):
            out.append(ex)
    return out


def named_keys(sieve: dict[str, Any] | None) -> set[str]:
    """company_key and security_id of every company the sieve names (card answers and pins, should_pass /
    should_fail checks, `sieve pin`): the shells filter never drops them."""
    out: set[str] = set()
    for ex in (sieve or {}).get("examples") or []:
        if isinstance(ex, dict):
            out |= {ex.get("company_key"), ex.get("security_id")} - {None}
    return out


def keywords_of(sieve: dict[str, Any] | None) -> dict[str, dict[str, list[str]]] | None:
    """sieve.keywords ({lang: {add, weak, log}}) for screen.resolve_keywords(sieve_kw=...), or None."""
    kw = (sieve or {}).get("keywords")
    return kw if isinstance(kw, dict) and kw else None


def _facets(sieve: dict[str, Any] | None) -> dict[str, str] | None:
    f = (sieve or {}).get("facets")
    return f if isinstance(f, dict) and f.get("category") and f.get("target") else None


def _facets_zh(sieve: dict[str, Any] | None) -> dict[str, str]:
    """facets_zh, falling back per slot to the English facets."""
    fz = (sieve or {}).get("facets_zh") if isinstance((sieve or {}).get("facets_zh"), dict) else {}
    f = _facets(sieve) or {}
    return {k: (fz.get(k) or f.get(k) or "") for k in ("category", "target", "mechanism")}


# ---------------------------------------------------------------------------------------------------------------
# Rules

def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def render_rules(rules: Iterable[Any], facets: dict[str, str] | None = None) -> list[tuple[str, str, str]]:
    """[(rule_id, label, sentence)] for the active rules, sorted by rule id and label, deduplicated.

    A rule is a library id or a sieve rule entry ({'id', 'terms'?, 'adopted_at'?}). Facet templates are used when
    the sieve has facets (category / target; the template of the facets' type, facet_type: technology / geography
    / customer), else the generic fallback; a facets-only rule without facets, and a homonym rule without terms
    under facets, falls back to its generic text or is dropped. scope_narrow and
    scope_broad are mutually exclusive: the newest (latest adopted_at, else later in the list) wins. Unknown ids
    raise ValueError."""
    entries: list[tuple[int, dict[str, Any]]] = []
    for i, r in enumerate(rules or ()):
        e = {"id": r} if isinstance(r, str) else dict(r) if isinstance(r, dict) else None
        if e is None or e.get("id") not in RULES:
            raise ValueError(f"unknown calibration rule: {r!r}")
        entries.append((i, e))
    excl = [(str(e.get("adopted_at") or ""), i, e["id"]) for i, e in entries if e["id"] in EXCLUSIVE_RULES]
    if excl:
        keep = max(excl)[2]
        entries = [(i, e) for i, e in entries if e["id"] not in EXCLUSIVE_RULES or e["id"] == keep]
    facets = facets if isinstance(facets, dict) and facets.get("category") and facets.get("target") else None
    out: set[tuple[str, str, str]] = set()
    for _, e in entries:
        spec = RULES[e["id"]]
        terms = [str(t) for t in (e.get("terms") or []) if str(t).strip()]
        use = (spec.get("by_type") or {}).get(facet_type(facets) or "") or spec["template"]
        if spec.get("facets_only") and not facets:
            continue
        if not facets and spec.get("generic") is not None:
            use = spec["generic"]
        if e["id"] == "homonym":
            use = spec["template"] if terms else spec["generic"]
        target = (facets or {}).get("target", "")
        to = bool(_CUSTOMER_TARGET.search(target))      # 'consumers and merchants in X': offered TO them
        slots = {"category": (facets or {}).get("category", ""), "target": target,
                 "the_target": target if target[:4].lower() == "the " else f"the {target}",
                 "mechanism": (facets or {}).get("mechanism", ""),
                 "target_in": f"{'to' if to else 'in'} {target}",
                 "target_out": f"to customers other than {target}" if to else f"outside {target}",
                 "terms": ", ".join(f"'{t}'" for t in terms)}
        for label, tmpl in use.items():
            out.add((e["id"], label, _cap(tmpl.format(**slots))))
    return sorted(out)


_WORD = re.compile(r"[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*")      # 'ISO', 'k.k' - a sentence's full stop is not part


def _library_tokens() -> frozenset[str]:
    words: set[str] = set()
    for spec in RULES.values():
        for part in (spec.get("template") or {}, spec.get("generic") or {}):
            for text in part.values():
                words.update(_WORD.findall(text))
    return frozenset(words)


_LIBRARY_TOKENS = _library_tokens()
_NAME_SUFFIX = re.compile(r"(?:[\s,]+(?:inc|incorporated|corp|corporation|co|company|ltd|limited|plc|ag|sa|nv|se|"
                          r"llc|lp|holdings?|group|k\.?k|s\.?a|n\.?v)\.?)+$|^株式会社|株式会社$|[（(]株[)）]",
                          re.IGNORECASE)


def _short_name(name: str) -> str:
    n = unicodedata.normalize("NFKC", name or "").strip()
    prev = None
    while prev != n:
        prev, n = n, _NAME_SUFFIX.sub("", n).strip(" ,.")
    return n


def validate_rule_text(text: str, *, names: Iterable[str] = (), tickers: Iterable[str] = (),
                       quotes: Iterable[str] = (), terms: Iterable[str] = ()) -> list[str]:
    """Why a rendered rule sentence may not enter a Jev question (empty when it may):
    - a company name of the universe (corporate suffix removed, >= 4 characters) appears in it as a whole word, case
      sensitive (so the facet word 'access' does not collide with ACCESS Co., Ltd.);
    - a ticker of >= 4 characters appears as a whole token (the library's own words, e.g. ISMS, and the words of
      `terms` - the run's keyword terms, which homonym puts in {terms} by design, e.g. SAML = OTC:SAML - are exempt);
    - it shares a 30-character substring with a card quote."""
    probs: list[str] = []
    norm = unicodedata.normalize("NFKC", text or "")
    toks = set(_WORD.findall(norm))
    own = set(_LIBRARY_TOKENS) | {w for t in terms for w in _WORD.findall(unicodedata.normalize("NFKC", t or ""))}
    for t in sorted({t.strip() for t in tickers if t and len(t.strip()) >= 4}):
        if t in toks and t not in own:
            probs.append(f"含股票代码 {t}")
    seen = set()
    for n in names:
        s = _short_name(n)
        if len(s) < 4 or s in seen or s.islower() or s.lower() in _GEO_WORDS:
            continue                  # a region / country word of the facets ('Asia') is not a company name
        seen.add(s)
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])", norm):
            probs.append(f"含公司名 {s}")
    for q in quotes:
        q = unicodedata.normalize("NFKC", q or "")
        if any(q[i:i + 30] in norm for i in range(0, max(0, len(q) - 29))):
            probs.append(f"含卡片原文（{q[:20]}…）")
    return probs


def rule_text_problems(rules: Iterable[Any], facets: dict[str, str] | None, *, names: Iterable[str] = (),
                       tickers: Iterable[str] = (), quotes: Iterable[str] = (), terms: Iterable[str] = ()
                       ) -> list[tuple[str, str, list[str]]]:
    """[(rule_id, label, problems)] of the rendered sentences of `rules` (render_rules with `facets`) that
    validate_rule_text refuses: what candidate_rules, screen (before anything is sent) and `sieve check` enforce."""
    names, tickers, quotes, terms = list(names), list(tickers), list(quotes), list(terms)
    out = []
    for rid, label, text in render_rules(rules, facets):
        probs = validate_rule_text(text, names=names, tickers=tickers, quotes=quotes, terms=terms)
        if probs:
            out.append((rid, label, list(dict.fromkeys(probs))))
    return out


def keyword_terms(terms_by_lang: dict[str, list[str]] | None, sieve: dict[str, Any] | None = None) -> list[str]:
    """Every keyword term of a run ({lang: [terms]}) and the sieve's added terms (exempt from the ticker check)."""
    out = [t for ts in (terms_by_lang or {}).values() for t in ts or []]
    out += [t for k in (keywords_of(sieve) or {}).values() if isinstance(k, dict) for t in k.get("add") or []]
    return list(dict.fromkeys(out))


def load_names(con) -> tuple[list[str], list[str]]:
    """(names, tickers) of all active securities, for validate_rule_text."""
    rows = con.execute("SELECT DISTINCT name, symbol FROM securities WHERE active").fetchall()
    return sorted({r[0] for r in rows if r[0]}), sorted({r[1] for r in rows if r[1]})


# ---------------------------------------------------------------------------------------------------------------
# Pins (free, applied in screen's ranking step and by rerank_result)

def apply_pins(verified: list[dict[str, Any]], unverified: list[dict[str, Any]], sieve: dict[str, Any] | None
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """(verified', excluded_by_user, notes).

    verified / unverified: candidate dicts with at least company_key, security_id, name, market_cap_usd,
    l1_p_core, l2_label, l2_evidence and score (screen's ranking entries or result rows). Returns copies:
    - want explicit / partial: listed (moved from unverified if needed) and scored with the user's label via
      screen.score_of; user_verdict = want, verdict_source 'evidence+user' when the evidence label is verified,
      else 'user' with user_note 用户判断（年报未写明）;
    - want no: removed from both lists into excluded_by_user (user_verdict 'no', verdict_source 'user');
    - unsure / no pin: unchanged (verdict_source 'evidence').
    verified' is not sorted. The caller drops from its unverified list whatever is in verified' or excluded."""
    pin = pins(sieve)
    seen: set[int] = set()

    def lookup(e: dict[str, Any]) -> dict[str, Any] | None:
        return pin.get(e.get("company_key")) or pin.get(e.get("security_id"))

    out_v: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for e, in_verified in [(e, True) for e in verified] + [(e, False) for e in unverified]:
        ex = lookup(e)
        is_verified = e.get("l2_label") in screen.L2_VERIFIED
        if ex is None:
            if in_verified:
                out_v.append({**e, "user_verdict": None, "verdict_source": "evidence"})
            continue
        seen.add(id(ex))
        want = ex["want"]
        if want == "no":
            excluded.append({**e, "user_verdict": "no", "verdict_source": "user", "user_chip": ex.get("chip"),
                             **({"user_pin_via": "pin", "user_pin_at": ex.get("at")} if ex.get("via") == "pin"
                                else {})})
            continue
        row = {**e, "user_verdict": want, "user_chip": ex.get("chip"),
               "score": screen.score_of(want, e.get("l1_p_core"), e.get("market_cap_usd"), e.get("l2_evidence"))}
        if ex.get("via") == "pin":          # `jevscreen sieve pin` (the human told their AI), not a card answer
            row.update(user_pin_via="pin", user_pin_at=ex.get("at"))
        if is_verified:
            row["verdict_source"] = "evidence+user"
        else:
            row.update(verdict_source="user", user_note=USER_ONLY_NOTE)
        out_v.append(row)
    notes = []
    for ex in {id(x): x for x in pin.values()}.values():
        if id(ex) not in seen:
            notes.append(f"校准：{ex.get('name') or ex.get('security_id') or ex.get('company_key')} 不在本次结果中，"
                         f"钉选（{ex['want']}）未生效")
    return out_v, excluded, notes


_USER_KEYS = ("user_verdict", "verdict_source", "backfill", "user_note", "user_chip", "below_cut", "user_pin_via",
              "user_pin_at")


def _merge_candidates(result: dict[str, Any], pool: Iterable[dict[str, Any]] | None = None
                      ) -> list[dict[str, Any]]:
    """Every company of a finished run as a row-shaped dict: result rows (output), unverified, excluded_by_user,
    then `pool` (load_pool: all L2-read companies from screen_results) for companies not listed and to fill the
    fields a row lacks. '_in_out' marks the output rows."""
    by: dict[str, dict[str, Any]] = {}
    for where in ("rows", "unverified", "excluded_by_user"):
        for r in result.get(where) or []:
            if r.get("company_key") and r["company_key"] not in by:
                by[r["company_key"]] = {**r, "_in_out": where == "rows" and r.get("rank") is not None}
    for p in pool or []:
        k = p.get("company_key")
        if not k:
            continue
        cur = by.get(k)
        if cur is None:
            by[k] = {**p, "_in_out": False}
        else:
            for f, v in p.items():
                if cur.get(f) is None:
                    cur[f] = v
    return list(by.values())


def _l1_pass(c: dict[str, Any]) -> bool:
    if c.get("l1_pass") is not None:
        return bool(c["l1_pass"])
    return not c.get("l2_forced")


def _evidence_score(c: dict[str, Any], rank: str) -> float:
    if rank == "ev":
        return screen.score_ev(c.get("l2_p_explicit"), c.get("l2_p_partial"), c.get("l1_p_core"),
                               c.get("market_cap_usd"), c.get("l2_evidence"))
    return screen.score_of(c.get("l2_label"), c.get("l1_p_core"), c.get("market_cap_usd"), c.get("l2_evidence"))


def ranking_entries(cands: list[dict[str, Any]], sieve: dict[str, Any] | None, rank: str = "label"
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(verified, unverified) ranking entries as screen's step 4 builds them: L1 passes (and pinned companies),
    contradicted dropped unless pinned, evidence score by `rank`, earlier user fields removed."""
    pin = pins(sieve)
    verified, unverified = [], []
    for c in cands:
        pinned = pin.get(c.get("company_key")) or pin.get(c.get("security_id"))
        if not _l1_pass(c) and not pinned:
            continue
        label = c.get("l2_label")
        if label == "contradicted" and not pinned:
            continue
        e = {k: v for k, v in c.items() if k not in _USER_KEYS and k != "rank"}
        e["score"] = _evidence_score(c, rank)
        (verified if label in screen.L2_VERIFIED else unverified).append(e)
    return verified, unverified


def rerank_result(result: dict[str, Any], sieve: dict[str, Any] | None, *,
                  pool: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The run's ranking redone with `sieve`'s pins, free (screen.pin_and_rank: the same code as screen's step 4, so
    this equals what a rerun gives when no rule or text changes). `pool` (load_pool) adds the verified companies
    ranked below max_out, which results.json does not list, so backfill works as in screen.
    Returns {'rows' (ranked: the top max_out plus user yes pins below it, below_cut), 'unverified', 'excluded_by_user', 'notes', 'max_out', 'rank'}."""
    params = result.get("params") or {}
    max_out = int(params.get("max_out") or screen.SCREEN_DEFAULTS["max_out"])
    rank = params.get("rank") or "label"
    verified, unverified = ranking_entries(_merge_candidates(result, pool), sieve, rank)
    v2, u2, excluded, notes = screen.pin_and_rank(verified, unverified, sieve, max_out)

    def out(e: dict[str, Any], i: int | None) -> dict[str, Any]:
        row = {k: v for k, v in e.items() if not k.startswith("_")}
        row.update(rank=i, score=round(e["score"], 4))
        return row

    return {"rows": [out(e, i) for i, e in screen.output_entries(v2, max_out)],
            "unverified": [out(e, None) for e in u2[:max_out]],
            "excluded_by_user": [out(e, None) for e in excluded], "notes": notes, "max_out": max_out, "rank": rank}


US_EXCHANGES = frozenset({"NASDAQ", "NYSE", "AMEX", "NYSEARCA", "NYSEAMERICAN", "NYSEMKT", "OTC", "BATS", "CBOE"})
_SHARE_CLASS = re.compile(r"\s+(?:Class|Cl\.?)\s+[A-Z]\.?$")


def _display(r: dict[str, Any]) -> str:
    """How a company is named in Chinese output: a US listing by its name ('Hivebay, Inc.'); any other listing (or
    a CJK name) with its exchange code and without a share-class suffix ('Beijing Beichen Xinan Software Co., Ltd.
    309352', '恒远世通 309161'), so an A-share / JP / KR company is recognisable by its ticker."""
    name = r.get("name") or r.get("security_id") or r.get("company_key") or "?"
    sid = r.get("security_id") or ""
    exch, _, sym = sid.rpartition(":")
    if not sym or not (screen.has_non_ascii(name) or (exch and exch.upper() not in US_EXCHANGES)):
        return name
    return f"{_SHARE_CLASS.sub('', name).strip() or name} {sym}"


def want_zh(want: str | None, chip: str | None = None) -> str:
    """An answer in the answer grammar: 要a / 要b / 不要e / ?."""
    if want == "no":
        return f"不要{chip or ''}"
    if want in ("explicit", "partial"):
        return f"要{chip or ('a' if want == 'explicit' else 'b')}"
    return "?" if want == "unsure" else str(want or "?")


def _answer_zh(r: dict[str, Any]) -> str:
    return want_zh(r.get("user_verdict"), r.get("user_chip"))


YES_WORDS_ZH = {"a": "直接做", "b": "相关，算大类"}


def answer_words_zh(want: str | None, chip: str | None = None, *, user_only: bool = False) -> str:
    """An answer in plain words for the answer printout: 要（直接做）/ 要（年报没写，按你的判断）/ 不要（是买方…）/
    不确定. `user_only`: the row is listed only because of the answer (the filing does not say it)."""
    if want == "no":
        why = NO_CHIP_TEXT.get(chip or "") or (G_CHIP_GENERIC_ZH if chip == "g" else None)
        return f"不要（{why}）" if why else "不要"
    if want in ("explicit", "partial"):
        if user_only:
            return "要（年报没写，按你的判断）"
        return f"要（{YES_WORDS_ZH[chip or ('a' if want == 'explicit' else 'b')]}）" \
            if (chip or "a") in YES_WORDS_ZH else "要"
    return "不确定"


CAUSE_WORDS_ZH = {"关键词（摘录变了）": "年报摘录换了一段", "规则": "按新规则重判",
                  "重读": "AI 重读后结论变了", "被挤出": "被排名更高的公司挤出"}


def render_diff_zh(before: dict[str, Any], after: dict[str, Any], *, title: str = "立即生效（免费）") -> str:
    """The row diff between two rankings (a result and rerank_result / a newer result), Chinese, each change
    tagged with its cause in plain words: 你：不要 / 你：要（年报没写，按你的判断）/ 递补，未经你确认 / 年报摘录换了一段 /
    按新规则重判 / AI 重读后结论变了 / 被排名更高的公司挤出; then the rank moves of the companies the user answered."""
    b_rows = {r["company_key"]: r for r in before.get("rows") or []}
    a_rows = {r["company_key"]: r for r in after.get("rows") or []}
    b_all = {r["company_key"]: r for w in ("rows", "unverified", "excluded_by_user") for r in before.get(w) or []}
    a_all = {r["company_key"]: r for w in ("rows", "unverified", "excluded_by_user") for r in after.get(w) or []}
    a_excl = {r["company_key"] for r in after.get("excluded_by_user") or []}
    q_changed = bool(before.get("questions") and after.get("questions")
                     and (before["questions"] or {}).get("l2") != (after["questions"] or {}).get("l2"))

    def evidence_cause(k: str) -> str | None:
        b, a = b_all.get(k) or {}, a_all.get(k) or {}
        if b.get("l2_evidence") == "profile" and a.get("l2_evidence") == "annual_report":
            return "新抓年报"          # read from a profile before, from an annual report now (jevscreen.ondemand)
        if b.get("evidence_sha") and a.get("evidence_sha") and b["evidence_sha"] != a["evidence_sha"]:
            return "关键词（摘录变了）"
        if b and a and b.get("l2_label") != a.get("l2_label"):
            return "规则" if q_changed else "重读"
        return None

    removed = []
    for k, r in b_rows.items():
        if k in a_rows:
            continue
        cause = "你：不要" if k in a_excl else evidence_cause(k) or "被挤出"
        removed.append(f"{_display(r)} #{r.get('rank')}（{CAUSE_WORDS_ZH.get(cause, cause)}）")
    added, backfill = [], []
    max_out = after.get("max_out") or (after.get("params") or {}).get("max_out")
    for k, r in a_rows.items():
        if k in b_rows:
            continue
        if r.get("user_verdict"):
            tag = "你：" + answer_words_zh(r.get("user_verdict"), r.get("user_chip"),
                                          user_only=r.get("verdict_source") == "user")
            if r.get("below_cut"):
                tag += f" · 排第{r.get('rank')}，在前{max_out or '?'}名外，照样列出"
            added.append(f"{_display(r)}（{tag}）")
        elif r.get("backfill"):
            backfill.append(_display(r))
        else:
            cause = evidence_cause(k)
            added.append(f"{_display(r)}（{CAUSE_WORDS_ZH.get(cause, cause)}）" if cause else _display(r))
    moves = []
    for k, r in a_rows.items():
        b = b_rows.get(k)
        if b and r.get("user_verdict") and b.get("rank") != r.get("rank"):
            moves.append(f"{_display(r)} #{b.get('rank')}→#{r.get('rank')}")
    lines = [title]
    if not (removed or added or backfill or moves):
        return "\n".join(lines + ["  名单没有变化"])
    if removed:
        lines.append(f"  移出 {len(removed)}：" + " · ".join(removed))
    if added or backfill:
        parts = added + (["、".join(backfill) + "（递补，未经你确认）"] if backfill else [])
        lines.append(f"  新进 {len(added) + len(backfill)}：" + " · ".join(parts))
    if moves:
        lines.append("  名次：" + " · ".join(moves))
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# Run data for cards / rerank (read-only store access)

def _columns(con, table: str) -> set[str]:
    return {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                                      [table]).fetchall()}


def load_pool(con, run_id: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every company layer 2 read in a run, as row-shaped dicts (company_key, security_id, name, country,
    market_cap_usd, l1_label, l1_p_core, l1_p_adjacent, l1_pass, l2_label (status ok only), l2_status, l2_evidence,
    l2_p_pos, l2_p_explicit, l2_p_partial, l2_p_pos_sd, l2_reads, l2_edge, l2_read_detail, evidence_url,
    l1_description, l1_input_tier), from screen_results and the universe. results.json lists only the top max_out
    verified rows; the cards and rerank_result need the rest. Works on a store without the repeated-read columns
    (a run made before them counts as one read)."""
    params = params or {}
    have = _columns(con, "screen_results")
    extra = ", ".join(c if c in have else f"NULL AS {c}" for c in ("reads_json", "p_pos", "p_pos_sd"))
    res = con.execute(f"SELECT company_key, security_id, layer, label, probs_json, status, input_source, "
                      f"input_tier, evidence_url, {extra} FROM screen_results WHERE run_id = ? "
                      "ORDER BY company_key, layer", [run_id]).fetchall()
    l1: dict[str, dict] = {}
    l2: dict[str, dict] = {}
    for ck, sid, layer, label, pj, status, src, tier, url, rj, pp, psd in res:
        d = {"security_id": sid, "label": label, "probs": json.loads(pj) if pj else {}, "status": status,
             "input_source": src, "input_tier": tier, "evidence_url": url,
             "reads": json.loads(rj) if rj else None, "p_pos": pp, "p_pos_sd": psd}
        (l1 if layer == "l1" else l2)[ck] = d
    keys = sorted(l2)
    if not keys:
        return []
    uni = {r[0]: r[1:] for r in con.execute(
        "SELECT company_key, security_id, name, country, exchange, market_cap_usd FROM universe "
        "WHERE company_key IN (SELECT unnest(?))", [keys]).fetchall()}
    descs: dict[str, list[tuple[str, str, bool]]] = {}
    for ck, src, text, own in con.execute("""
            WITH u AS (SELECT security_id, company_key FROM universe WHERE company_key IN (SELECT unnest(?)))
            SELECT u.company_key, d.source_id, d.text, d.security_id = u.security_id
            FROM u JOIN descriptions d ON d.security_id = u.security_id
            WHERE d.text IS NOT NULL AND trim(d.text) <> ''
            UNION
            SELECT u.company_key, d.source_id, d.text, d.security_id = u.security_id
            FROM u JOIN descriptions d ON d.company_key = u.company_key
            WHERE d.text IS NOT NULL AND trim(d.text) <> ''""", [keys]).fetchall():
        descs.setdefault(ck, []).append((src, text, bool(own)))
    adj_min = params.get("l1_adjacent_min", screen.L1_ADJACENT_MIN)
    core_min = params.get("l1_core_min", screen.L1_CORE_MIN)
    out = []
    for ck in keys:
        r2, r1 = l2[ck], l1.get(ck)
        u = uni.get(ck)
        ok = r2["status"] == "ok"
        probs = r2["probs"] or {}
        reads = [d for d in (r2["reads"] or []) if d.get("p_pos") is not None]
        p_pos = r2["p_pos"] if r2["p_pos"] is not None else (screen.p_pos_of(probs) if ok else None)
        desc = screen.select_description(descs.get(ck, []))
        l1r = {"status": r1["status"], "label": r1["label"], "probs": r1["probs"]} if r1 else None
        out.append({
            "company_key": ck, "security_id": (u[0] if u else None) or r2["security_id"],
            "name": u[1] if u else None, "country": u[2] if u else None, "market_cap_usd": u[4] if u else None,
            "rank": None, "l1_label": (r1 or {}).get("label"), "l1_p_core": screen.p_core_of(l1r),
            "l1_p_adjacent": ((r1 or {}).get("probs") or {}).get("adjacent"),
            "l1_pass": screen.l1_passes(l1r, adjacent_min=adj_min, core_min=core_min),
            "l2_label": r2["label"] if ok else None, "l2_status": r2["label"] if ok else r2["status"],
            "l2_evidence": ("profile" if r2["input_source"] == "profile" else "annual_report") if ok else None,
            "l2_p_pos": p_pos, "l2_p_explicit": probs.get("explicit") if ok else None,
            "l2_p_partial": probs.get("partial") if ok else None, "l2_p_pos_sd": r2["p_pos_sd"] if ok else None,
            "l2_reads": (len(reads) or 1) if ok else None,
            "l2_edge": (screen.L2_EDGE[0] <= p_pos < screen.L2_EDGE[1]) if (ok and p_pos is not None) else None,
            "l2_read_detail": r2["reads"], "evidence_url": r2["evidence_url"], "l2_input_tier": r2["input_tier"],
            "l1_description": (desc or {}).get("plain"), "l1_input_tier": (desc or {}).get("tier")})
    return out


def load_inputs(path: str | Path) -> dict[str, dict[str, Any]]:
    """l2_inputs.jsonl of a run (a file or the run's output directory) as {company_key: line}; {} when missing."""
    p = Path(path)
    if p.is_dir():
        p = p / "l2_inputs.jsonl"
    out: dict[str, dict[str, Any]] = {}
    with contextlib.suppress(FileNotFoundError):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                x = json.loads(line)
                out[x["company_key"]] = x
    return out


def run_companies(con, keys: Iterable[str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """({company_key: {security_id, company_key, name, country, exchange, market_cap_usd, desc}}, {company_key:
    document}) of the given companies as screen builds them (select_description, load_documents): what
    screen._l2_input needs to rebuild a run's L2 inputs. Every market cap counts (min 0), so a company whose market
    cap moved since the run is still found; one without a description is left out."""
    want = set(keys)
    if not want:
        return {}, {}
    uni = {r["company_key"]: r for r in screen.load_universe(con, min_mcap_usd=0.0, min_avg_volume=None)
           if r["company_key"] in want}
    descs = screen.load_descriptions(con, min_mcap_usd=0.0)
    docs = screen.load_documents(con, min_mcap_usd=0.0)
    cs: dict[str, dict[str, Any]] = {}
    for ck, u in uni.items():
        d = screen.select_description(descs.get(ck, []))
        if d is not None:
            cs[ck] = {**u, "desc": d}
    return cs, {k: docs[k] for k in cs if k in docs}


def _run_date(result: dict[str, Any]) -> dt.date | None:
    with contextlib.suppress(TypeError, ValueError):
        return dt.date.fromisoformat(str(result.get("started_at") or "")[:10])
    return None


def rebuild_inputs(con, result: dict[str, Any], keys: Iterable[str], *,
                   companies: tuple[dict, dict] | None = None) -> dict[str, dict[str, Any]]:
    """l2_inputs.jsonl lines ({company_key: line}) of a run made before l2_inputs.jsonl existed, rebuilt with
    screen._l2_input from the CURRENT documents, the run's terms_by_lang and its sieve's weak terms. They show what
    layer 2 would read today, which can differ from what it read then (a newer filing, the CJK fragment widening)."""
    cs, docs = companies if companies is not None else run_companies(con, keys)
    terms = result.get("terms_by_lang") or {"en": result.get("terms") or []}
    weak = (result.get("calibration") or {}).get("keywords_weak") or {}
    today = _run_date(result)
    return {k: screen._l2_input_line(c, screen._l2_input(c, docs.get(k), terms, today, weak))
            for k, c in sorted(cs.items()) if k in set(keys)}


def l1_relevant(con, run_id: str) -> set[str]:
    """The companies L1 judged core or adjacent in a run (the lift base of mine_terms / sieve check)."""
    return {r[0] for r in con.execute(
        "SELECT company_key FROM screen_results WHERE run_id = ? AND layer = 'l1' AND status = 'ok' "
        "AND label IN ('core', 'adjacent')", [run_id]).fetchall()}


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def propose_keywords(cfg, con, result: dict[str, Any], sieve: dict[str, Any] | None, inputs: Any,
                     pool: Iterable[dict[str, Any]] | None = None, *,
                     companies: tuple[dict, dict] | None = None) -> list[dict[str, Any]]:
    """The free keyword learning of one run, per CJK language with L2 inputs in it: noisy seeds (noisy_terms: the
    run's terms in >= 5% of that language's filings become weak), mined local vocabulary (mine_terms over the
    filings of the yes companies: answered yes / should_pass, or verified with p̄ >= MINE_YES_P; no_docs = the
    keyword excerpts of the companies answered no / should_fail; acronyms seeded by any language of the run pass the
    acronym gate), replacements for the seeds absent from every filing of the source (mine_replacements over the L2
    texts of the verified companies of that language), minus the terms that worsen a yes company's excerpt alone
    (drop_worsening_terms), and the peer
    preview (peer_preview over every L2 input in that language; a change that costs a yes company its keyword hit or
    an old term of its excerpt is not adopted).
    Learned adds are never seeds, and learned adds from before the current gates that fail them are proposed for
    removal ('remove' [{term, why}], 'remove_log'; regate_learned), also in languages the run has no text in (static
    checks only); the caller removes them whether or not the rest is adopted.
    Returns [{'lang', 'add', 'weak', 'log', 'noisy', 'mined', 'preview', 'adopted', 'why_zh', 'remove',
    'remove_log'}]; the caller merges an adopted proposal with merge_keywords."""
    ins = _inputs_map(inputs)
    langs = sorted({x.get("lang") for x in ins.values() if x.get("lang") in screen.CJK_LANGS})
    if not langs and not learned_any(keywords_of(sieve)):
        return []
    cands = _merge_candidates(result, pool)
    by_key = {c["company_key"]: c for c in cands}
    sid_key = {c.get("security_id"): c["company_key"] for c in cands if c.get("security_id")}
    yes: set[str] = set()
    no: set[str] = set()
    for ex in (sieve or {}).get("examples") or []:
        if not isinstance(ex, dict) or (ex.get("source", "card") == "card" and not ex.get("pin")):
            continue
        k = ex.get("company_key") if ex.get("company_key") in by_key else sid_key.get(ex.get("security_id"))
        if k is None:
            continue
        if _is_yes(ex.get("want")):
            yes.add(k)
        elif ex.get("want") == "no":
            no.add(k)
    strong = {k for k, c in by_key.items() if c.get("l2_label") in screen.L2_VERIFIED
              and (c.get("l2_p_pos") or 0.0) >= MINE_YES_P}
    cs, docs = companies if companies is not None else run_companies(con, ins)
    rel = l1_relevant(con, result.get("run_id") or "")
    kw = keywords_of(sieve) or {}
    tt = (sieve or {}).get("target_terms") if isinstance((sieve or {}).get("target_terms"), dict) else {}
    # seeds: the run's terms minus what an earlier answer learned (mined / replacement adds, which resolve_keywords
    # appended to them), plus target_terms; a learned add is never a seed (v1 noise such as zh SQL must not pass
    # the acronym gate of another language)
    learned = {lg: {_fold(t) for t in learned_adds(kw, lg)} for lg in kw}
    generated = (result.get("keywords") or {}).get("generated") or {}
    seed_terms = {lg: [t for t in ts or [] if _fold(t) not in learned.get(lg, set())
                       or _fold(t) in {_fold(g) for g in generated.get(lg) or []}]
                  for lg, ts in (result.get("terms_by_lang") or {}).items()}
    seeded = [t for ts in list(seed_terms.values()) + list(tt.values()) for t in ts or []]
    seed_up = {unicodedata.normalize("NFKC", t).strip().upper() for t in seeded if t}
    run_texts = {lg: [_body(x.get("text")) for x in ins.values() if x.get("lang") == lg] for lg in LANGS}
    out = []
    for lang in sorted(set(kw) - set(langs)):          # languages this run has no text in: the static gates only
        rm = regate_learned(kw, lang, seed_up, [])
        if rm:
            out.append({"lang": lang, "add": [], "weak": [], "log": [], "noisy": {}, "mined": {}, "preview": None,
                        "adopted": False, "why_zh": None, "remove": rm, "remove_log": _remove_log(rm)})
    for lang in langs:
        cur = list((result.get("terms_by_lang") or {}).get(lang) or [])       # incl. the learned adds so far
        old_weak = list((kw.get(lang) or {}).get("weak") or [])
        noisy = noisy_terms(cfg, con, cur, lang)
        new_weak = [t for t in noisy["weak"] if t not in old_weak]
        in_lang = [k for k, x in ins.items() if x.get("lang") == lang]
        yes_keys = sorted(k for k in (yes | (strong - no)) if k in in_lang and k in docs)
        yes_docs = [_read_text(docs[k].get("text_path")) for k in yes_keys]
        no_docs = [next((e["text"] for e in ins[k].get("excerpts") or [] if e.get("kind") == "keywords"), "")
                   for k in sorted(no) if k in in_lang]
        names = [by_key[k].get("name") or "" for k in yes_keys]
        src = LANG_SOURCE.get(lang)
        have = [(k, d) for k, d in zip(yes_keys, yes_docs) if d]
        remove = regate_learned(kw, lang, seed_up, run_texts.get(lang, []) + [d for _, d in have])
        gone = {_fold(x["term"]) for x in remove}
        cur = [t for t in cur if _fold(t) not in gone]
        mined = mine_terms(lang, [d for _, d in have], cur, [d for d in no_docs if d], names,
                           lambda ts, src=src: background_df(cfg, con, src, ts), rel, seeded=seeded,
                           yes_keys=[k for k, _ in have]) if src else {"add": [], "log": [], "rejected": []}
        if noisy.get("absent") and src:
            # seeds the model wrote that no filing of this source contains (df 0): replaced by words of the texts
            # of this language's yes companies (answered yes / should_pass, or verified with p̄ >= MINE_YES_P)
            ver = [k for k in sorted(in_lang) if k not in no and (ins[k].get("evidence") == "annual_report")
                   and (k in yes or k in strong)]
            rep = mine_replacements(lang, [_body(ins[k].get("text")) for k in ver], ver, noisy["absent"],
                                    [d for d in no_docs if d], [by_key[k].get("name") or "" for k in ver],
                                    lambda ts, src=src: background_df(cfg, con, src, ts), rel, current_terms=cur)
            fresh = [t for t in rep["add"] if _fold(t) not in {_fold(x) for x in mined["add"]}]
            mined = {**mined, "add": list(mined["add"]) + fresh,
                     "log": list(mined["log"]) + [e for e in rep["log"] if e["term"] in fresh],
                     "rejected": list(mined["rejected"]) + rep["rejected"], "replacements": rep}
        add = list(mined["add"])
        entries = [(cs[k], docs.get(k)) for k in sorted(in_lang) if k in cs]
        protected = (set(yes) | {by_key[k].get("security_id") for k in yes if by_key.get(k)}) - {None}
        if add:
            # a new term never worsens the excerpt of a yes / should_pass company (tried one term at a time)
            add, worsening = drop_worsening_terms(entries, lang, {"terms": cur, "weak": old_weak}, add,
                                                  protected=protected, today=_run_date(result))
            if worsening:
                mined = {**mined, "add": add,
                         "log": [e for e in mined["log"] if e["term"] in add],
                         "rejected": list(mined["rejected"]) + [{"term": x["term"], "why": x["why"]}
                                                                for x in worsening]}
        prop: dict[str, Any] = {"lang": lang, "add": add, "weak": new_weak, "noisy": noisy, "mined": mined,
                                "preview": None, "adopted": False, "why_zh": None, "remove": remove,
                                "remove_log": _remove_log(remove),
                                "log": list(mined["log"]) + [
                                    {"term": t, "action": "weak", "source": "noisy",
                                     "df": noisy["stats"][t]["df"], "share": noisy["stats"][t]["share"]}
                                    for t in new_weak]}
        if add or new_weak:
            wf = {_fold(t) for t in new_weak}
            old = {"terms": cur, "weak": old_weak}
            new = {"terms": [t for t in dict.fromkeys(cur + add) if _fold(t) not in wf], "weak": old_weak + new_weak}
            prop["preview"] = peer_preview(entries, lang, old, new, protected=protected, today=_run_date(result))
            prop["adopted"] = not prop["preview"]["rejected"]
            prop["why_zh"] = prop["preview"]["why_zh"]
        out.append(prop)
    return out


def learned_adds(kw: dict[str, Any] | None, lang: str, *, legacy_only: bool = False) -> list[str]:
    """The terms of keywords[lang].add that an answer learned (latest add log entry with source mined /
    replacement), in add order; legacy_only: only those whose entry predates MINE_GATE_VERSION. An add without such
    a log entry is the user's (or their AI's) own and is never re-gated."""
    k = (kw or {}).get(lang) if isinstance((kw or {}).get(lang), dict) else {}
    last: dict[str, dict[str, Any]] = {}
    for e in k.get("log") or []:
        if isinstance(e, dict) and e.get("action") == "add" and isinstance(e.get("term"), str):
            last[_fold(e["term"])] = e
    out = []
    for t in k.get("add") or []:
        e = last.get(_fold(t))
        if e and e.get("source") in LEARNED_SOURCES and not (
                legacy_only and (e.get("gate") or 0) >= MINE_GATE_VERSION):
            out.append(t)
    return out


def learned_any(kw: dict[str, Any] | None) -> bool:
    return any(learned_adds(kw, lg, legacy_only=True) for lg in (kw or {}))


def regate_learned(kw: dict[str, Any] | None, lang: str, seed_up: set[str], docs: list[str]
                   ) -> list[dict[str, str]]:
    """[{term, why}] of the learned adds of `lang` from before the current gates (learned_adds legacy_only) that fail
    them now: an acronym that was not seeded and is short or generic ('acronym'; SQL, VPN), or a piece of a word
    (fragment_of_word over `docs`, the run's texts of that language; without texts only the static checks: a
    word-final Han start, a Chinese term across 的 / 和) ('fragment'; 化运维管理, クセス制御), or found at fewer than
    MINE_MIN_YES yes companies when it was mined ('yes_docs', from its log)."""
    k = (kw or {}).get(lang) if isinstance((kw or {}).get(lang), dict) else {}
    log = {_fold(e["term"]): e for e in k.get("log") or [] if isinstance(e, dict) and e.get("action") == "add"
           and isinstance(e.get("term"), str)}
    norm = [unicodedata.normalize("NFKC", d or "") for d in docs]
    out = []
    for t in learned_adds(kw, lang, legacy_only=True):
        if _ACRONYM_RUN.fullmatch(t):
            if not _acronym_ok(t, seed_up):
                out.append({"term": t, "why": "acronym"})
        elif fragment_of_word(t, lang, norm):
            out.append({"term": t, "why": "fragment"})
        elif (log.get(_fold(t)) or {}).get("yes_docs") is not None and log[_fold(t)]["yes_docs"] < MINE_MIN_YES:
            out.append({"term": t, "why": "yes_docs"})
    return out


def _remove_log(remove: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [{"term": x["term"], "action": "remove", "source": "regate", "why": x["why"], "gate": MINE_GATE_VERSION}
            for x in remove]


def _inputs_map(inputs: Any) -> dict[str, dict[str, Any]]:
    if inputs is None:
        return {}
    if isinstance(inputs, dict):
        return inputs
    return {x["company_key"]: x for x in inputs}


# ---------------------------------------------------------------------------------------------------------------
# Inclusion probability and card selection

def noise_sd(p: float) -> float:
    """The sd of one L2 read's p_pos near p (NOISE_SD_BANDS)."""
    for hi, sd in NOISE_SD_BANDS:
        if p < hi:
            return sd
    return NOISE_SD_BANDS[-1][1]


def deck_seed(result: dict[str, Any] | str, inputs: Any = None) -> int:
    """The seed of a run's deck (the π draws): sha256 of the idea_key and the sorted (company_key, evidence_sha) of
    its L2 inputs, so identical inputs give identical decks (a $0 cached rerun has a new run_id but the same deck).
    A bare string (the v1 form, a run id) is hashed as it is."""
    if isinstance(result, str):
        body = result
    else:
        from . import keywords
        ins = _inputs_map(inputs)
        body = keywords.idea_key(result.get("idea") or "") + "\n" + "\n".join(
            f"{k}|{(v or {}).get('evidence_sha') or ''}" for k, v in sorted(ins.items()))
    return int(hashlib.sha256(body.encode("utf-8")).hexdigest()[:8], 16)


def inclusion_probability(cands: list[dict[str, Any]], *, max_out: int, sieve: dict[str, Any] | None = None,
                          rank: str = "label", draws: int = PI_DRAWS, seed: int = 0) -> dict[str, float]:
    """π per company: the share of `draws` noisy re-reads in which it lands in the top max_out.

    cands: ranking candidates (ranking_entries: row-shaped, with l2_p_pos, l2_p_explicit, l2_p_partial, l2_reads).
    Per draw p̃ ~ N(p̄, noise_sd(p̄)/sqrt(n)) clipped to [0, 1]; verified when p̃ >= 0.5, labelled explicit when
    p̄_explicit >= p̄_partial else partial, scored with the run's rank function (ev: p̃ split in the p̄ ratio), then
    the pins apply as in apply_pins (a yes is always listed with the user's label, a no never). Candidates without
    a mean (no ok L2 read) are fixed by their label. Deterministic for a seed; items more than 6 sd from the cut are
    not drawn (their outcome is certain)."""
    pin = pins(sieve)
    fixed: list[tuple] = []
    stoch: list[tuple] = []
    keys = []
    for c in sorted(cands, key=lambda x: x["company_key"]):
        ck = c["company_key"]
        keys.append(ck)
        pc, mcap, ev = c.get("l1_p_core"), c.get("market_cap_usd"), c.get("l2_evidence")
        tail = (-(mcap or 0), c.get("security_id") or "", ck)
        ex = pin.get(ck) or pin.get(c.get("security_id"))
        if ex is not None:
            if ex["want"] != "no":
                fixed.append((-screen.score_of(ex["want"], pc, mcap, ev),) + tail)
            continue
        label, p = c.get("l2_label"), c.get("l2_p_pos")
        if label is None:
            continue
        if p is None:
            if label in screen.L2_VERIFIED:
                fixed.append((-_evidence_score(c, rank),) + tail)
            continue
        pe, pp = float(c.get("l2_p_explicit") or 0.0), float(c.get("l2_p_partial") or 0.0)
        if ev == "profile":                   # a profile supports at most partial (screen.PROFILE_CAP_ZH)
            pe, pp = 0.0, pe + pp
        lab = "explicit" if pe >= pp and not (ev == "profile") else "partial"
        fe = pe / (pe + pp) if pe + pp > 0 else 1.0
        s = noise_sd(p) / math.sqrt(max(1, int(c.get("l2_reads") or 1)))
        if rank == "ev":
            def mk(pt, fe=fe, pc=pc, mcap=mcap, ev=ev, tail=tail):
                return (-screen.score_ev(pt * fe, pt * (1 - fe), pc, mcap, ev),) + tail
        else:
            key = (-screen.score_of(lab, pc, mcap, ev),) + tail

            def mk(pt, key=key):
                return key
        if s <= 0 or abs(p - PI_CUT) > 6 * s:
            if p >= PI_CUT:
                fixed.append(mk(p))
            continue
        stoch.append((p, s, mk))
    rng = random.Random(seed)
    counts: dict[str, int] = {}
    n_draws = max(1, int(draws))
    for _ in range(n_draws):
        cur = list(fixed)
        for p, s, mk in stoch:
            pt = min(1.0, max(0.0, rng.gauss(p, s)))
            if pt >= PI_CUT:
                cur.append(mk(pt))
        for k in heapq.nsmallest(max_out, cur):
            counts[k[-1]] = counts.get(k[-1], 0) + 1
    return {k: counts.get(k, 0) / n_draws for k in keys}


def _stratum(c: dict[str, Any], inp: dict[str, Any]) -> str:
    if (inp.get("evidence") or c.get("l2_evidence")) == "profile":
        return "profile"
    return screen.source_label(inp.get("source_id") or c.get("filing_source"))


def _body(text: str | None) -> str:
    """An L2 text without its '[...]' tag line."""
    if not text:
        return ""
    first, _, rest = text.partition("\n")
    return rest.strip() if screen._TAG_LINE.fullmatch(first.strip()) else text


def _lang_of(c: dict[str, Any], inp: dict[str, Any]) -> str:
    return inp.get("lang") or c.get("doc_lang") or screen.detect_language(_body(inp.get("text")), default="en") \
        or "en"


def select_cards(result: dict[str, Any], inputs: Any, sieve: dict[str, Any] | None, *, max_cards: int = CARDS_MAX,
                 seed: int | None = None, pool: Iterable[dict[str, Any]] | None = None,
                 draws: int = PI_DRAWS) -> list[dict[str, Any]]:
    """The calibration deck of a finished run: a pure, deterministic list of card dicts (cards.json 'cards').

    result: results.json (rows = the output O, unverified, excluded_by_user, params max_out / rank, run_id);
    inputs: l2_inputs.jsonl ({company_key: line} or the lines); pool: load_pool (the verified companies below
    max_out and L1 p_adjacent / descriptions; without it boundary_out has no candidates). seed defaults to
    deck_seed(result, inputs): the idea and the inputs, not the run id, so a cached rerun gives the same deck.
    Excluded everywhere: companies without an ok L2 read, companies answered on a card with the same evidence_sha,
    the user's AI's checks (source 'sieve'), and pinned companies outside the recheck slot. Slots in order, a
    company never twice, at most max_cards: recheck (2: a pin whose evidence_sha changed), top_check (1: rank <= 5,
    explicit, p̄_explicit - p̄_partial < 0.20, highest rank), scope (1: only with sieve.target_terms for the text's
    language; p̄ >= 0.75 and no target term in the L2 text, largest market cap), boundary_in (4, or 3 after a scope
    card: rows in O by v = 4π(1-π) x 1.5 when p̄ < 0.6, v > 0.3, at most 2 per evidence stratum), boundary_out (1:
    verified, not in O, max π + 0.5 p̄), gap (1: not in O, annual-report evidence, L2 insufficient, L1 P(core +
    adjacent) >= 0.70; keyword hit first, then largest market cap). Ties: market cap desc, then security_id."""
    ins = _inputs_map(inputs)
    params = result.get("params") or {}
    max_out = int(params.get("max_out") or screen.SCREEN_DEFAULTS["max_out"])
    rank = params.get("rank") or "label"
    seed = deck_seed(result, ins) if seed is None else seed
    cands = _merge_candidates(result, pool)
    by_key = {c["company_key"]: c for c in cands}
    verified, unverified = ranking_entries(cands, sieve, rank)
    ranked = {e["company_key"] for e in verified + unverified}
    pi = inclusion_probability(verified + unverified, max_out=max_out, sieve=sieve, rank=rank, draws=draws,
                               seed=seed)
    pin = pins(sieve)
    answered: dict[str, dict] = {}
    checks: set[str] = set()
    for ex in (sieve or {}).get("examples") or []:
        if not isinstance(ex, dict):
            continue
        for k in {ex.get("company_key"), ex.get("security_id")} - {None}:
            if ex.get("source") == "sieve":
                checks.add(k)
            else:
                answered[k] = ex

    def inp(c):
        return ins.get(c["company_key"]) or {}

    def sha(c):
        return inp(c).get("evidence_sha") or c.get("evidence_sha")

    def pinned(c):
        return pin.get(c["company_key"]) or pin.get(c.get("security_id"))

    def blocked(c) -> bool:
        if c.get("l2_label") is None or c["company_key"] in checks or c.get("security_id") in checks:
            return True
        if pinned(c):
            return True
        ex = answered.get(c["company_key"]) or answered.get(c.get("security_id"))
        return ex is not None and (not ex.get("evidence_sha") or ex.get("evidence_sha") == sha(c))

    def tie(c):
        return (-(c.get("market_cap_usd") or 0), c.get("security_id") or "")

    def p_of(c):
        return c.get("l2_p_pos")

    in_out = [c for c in cands if c.get("_in_out")]
    picked: list[tuple[str, dict]] = []
    used: set[str] = set()

    def take(slot: str, pool_: list[dict], cap: int, key: Callable | None = None,
             accept: Callable[[dict], bool] | None = None) -> int:
        n = 0
        for c in sorted(pool_, key=key or tie):
            if n >= cap or len(picked) >= max_cards:
                break
            if c["company_key"] in used or (accept and not accept(c)):
                continue
            picked.append((slot, c))
            used.add(c["company_key"])
            n += 1
        return n

    caps = dict(CARD_SLOTS)
    # 1) recheck: a pinned company whose filing changed (evidence_filing). The excerpt alone changing (a keyword
    # change, the CJK widening) is not a new filing: no card. An answer without its filing identity (recorded
    # before evidence_filing existed) is rechecked when the excerpt changed, saying only that.
    why_key: dict[str, str] = {}

    def recheck_due(c) -> bool:
        ex = pinned(c)
        if not (ex and ex.get("evidence_sha") and sha(c) and ex["evidence_sha"] != sha(c)):
            return False
        if not ex.get("evidence_filing"):
            why_key[c["company_key"]] = "recheck_excerpt"
            return True
        now_key = filing_key(*_filing_of(inp(c), c))
        if ex["evidence_filing"] != now_key and ex["evidence_filing"].startswith(PROFILE_FILING) \
                and not now_key.startswith(PROFILE_FILING):
            why_key[c["company_key"]] = "recheck_profile"
        return ex["evidence_filing"] != now_key
    take("recheck", [c for c in cands if c.get("l2_label") is not None and recheck_due(c)], caps["recheck"])
    free = [c for c in cands if not blocked(c)]
    free_out = [c for c in free if c.get("_in_out")]
    # 2) top_check
    take("top_check", [c for c in free_out if (c.get("rank") or 10 ** 9) <= TOP_CHECK_RANK
                       and c.get("l2_label") == "explicit" and c.get("l2_p_explicit") is not None
                       and c["l2_p_explicit"] - (c.get("l2_p_partial") or 0.0) < TOP_CHECK_MARGIN],
         caps["top_check"], key=lambda c: (c.get("rank"),) + tie(c))
    # 3) scope
    tt = (sieve or {}).get("target_terms") if isinstance((sieve or {}).get("target_terms"), dict) else {}
    n_scope = 0
    if tt:
        def no_target(c):
            terms = tt.get(_lang_of(c, inp(c))) or []
            return bool(terms) and not screen.matched_terms(_body(inp(c).get("text")), terms)
        n_scope = take("scope", [c for c in free_out if (p_of(c) or 0.0) >= SCOPE_MIN_P and inp(c).get("text")],
                       caps["scope"], accept=no_target)
    # 4) boundary_in
    v_of = {}
    for c in free_out:
        p = pi.get(c["company_key"], 0.0)
        v_of[c["company_key"]] = 4 * p * (1 - p) * (BOUNDARY_LOW_BOOST if (p_of(c) is not None
                                                                         and p_of(c) < BOUNDARY_LOW_P) else 1.0)
    strata: dict[str, int] = {}

    def stratum_ok(c):
        s = _stratum(c, inp(c))
        if strata.get(s, 0) >= STRATUM_MAX:
            return False
        strata[s] = strata.get(s, 0) + 1
        return True
    take("boundary_in", [c for c in free_out if v_of[c["company_key"]] > BOUNDARY_MIN_V],
         caps["boundary_in"] - (1 if n_scope else 0), key=lambda c: (-v_of[c["company_key"]],) + tie(c),
         accept=stratum_ok)
    # 5) boundary_out
    take("boundary_out", [c for c in free if not c.get("_in_out") and c["company_key"] in ranked
                          and c.get("l2_label") in screen.L2_VERIFIED],
         caps["boundary_out"], key=lambda c: (-(pi.get(c["company_key"], 0.0) + 0.5 * (p_of(c) or 0.0)),) + tie(c))

    # 6) gap
    def l1_rel(c):
        return (c.get("l1_p_core") or 0.0) + (c.get("l1_p_adjacent") or 0.0)
    take("gap", [c for c in free if not c.get("_in_out") and c.get("l2_label") == "insufficient"
                 and (inp(c).get("evidence") or c.get("l2_evidence")) == "annual_report" and l1_rel(c) >= GAP_L1_MIN],
         caps["gap"], key=lambda c: (not (inp(c).get("keyword_hit") or c.get("l2_keyword_hit")),) + tie(c))
    return [make_card(i, slot, by_key[c["company_key"]], inp(c), sieve, pi=pi.get(c["company_key"]),
                      max_out=max_out, why_key=why_key.get(c["company_key"]) if slot == "recheck" else None)
            for i, (slot, c) in enumerate(picked, 1)]


# a full stop that ends an abbreviation, not a sentence: 'Rykon Hosting, Inc. operates ...'
_ABBREV_END = re.compile(r"(?:^|[\s,(])(?:inc|co|corp|ltd|plc|llc|l\.p|s\.a|n\.v|a\.g|k\.k|no|nos|st|mr|ms|dr|jr|sr|"
                         r"u\.s|u\.k|e\.g|i\.e|etc|vs|approx)\.$", re.IGNORECASE)


def _first_sentence(text: str | None, max_chars: int) -> str | None:
    if not text or not text.strip():
        return None
    t = re.sub(r"^\[[^\]]*\]\s*", "", text.strip())            # a '[source]' tag of the L1 description
    t = re.sub(r"\s+", " ", t)
    lang = screen.detect_language(t, default="en")
    ends = [e for e in screen._sent_ends(t) if not _ABBREV_END.search(t[max(0, e - 8):e])]
    if ends and ends[0] <= max_chars:
        return t[:ends[0]].strip()
    return screen.truncate(t, max_chars, lang if lang in screen.CJK_LANGS else None)


_TAG_PARTS = re.compile(r"^\[annual report excerpts: (?P<label>\S+) (?P<form>.+?)(?: \+ summary)? filed "
                        r"(?P<date>[^;]+); language (?P<lang>\w+)\]")


def _filing_of(inp: dict[str, Any], row: dict[str, Any] | None = None) -> tuple[str, str | None, str | None]:
    """(source label, form, filing date) of the text L2 read: '公司简介' for a profile; else from the row, falling
    back to the L2 tag line."""
    row = row or {}
    if (inp.get("evidence") or row.get("l2_evidence")) == "profile":
        return "公司简介", None, None
    tag = _TAG_PARTS.match((inp.get("text") or "").split("\n", 1)[0].strip())
    return (screen.source_label(inp.get("source_id") or row.get("filing_source")),
            row.get("filing_form") or (tag.group("form") if tag else None),
            row.get("filing_date") or (tag.group("date") if tag else None))


PROFILE_FILING = "公司简介|"     # filing_key of a profile-only answer (_filing_of)


def filing_key(source: str | None, form: str | None, filing_date: str | None) -> str:
    """The identity of a filing ('SEC|10-K|2026-03-01'), stored with an answer (evidence_filing): a recheck card is
    due only when the filing changed, not when only the excerpt did (keyword changes, the CJK widening)."""
    return "|".join(str(x or "") for x in (source, form, filing_date))


def card_quote(inp: dict[str, Any], row: dict[str, Any] | None = None) -> dict[str, Any]:
    """The card's quote: at most QUOTE_MAX_CHARS verbatim around the densest term hits of the keyword excerpt
    (screen._keyword_window over the matched terms), else the overview's first sentence (a profile: its first
    sentence). Never evidence_excerpt. Source / form / filing date / URL from the row, else the L2 tag line."""
    row = row or {}
    lang = inp.get("lang")
    tlang = lang if lang in screen.CJK_LANGS else None
    excerpts = inp.get("excerpts") or []
    kw = next((e for e in excerpts if e.get("kind") == "keywords"), None)
    mt = list(inp.get("matched_terms") or [])
    if kw and mt:
        text = screen._keyword_window(kw["text"], [screen._term_pattern(t) for t in mt], QUOTE_MAX_CHARS, tlang)
    elif kw:
        text = screen.truncate(kw["text"], QUOTE_MAX_CHARS, tlang)
    elif excerpts:
        text = _first_sentence(excerpts[0].get("text"), QUOTE_MAX_CHARS)
    else:
        text = _first_sentence(_body(inp.get("text")), QUOTE_MAX_CHARS)
    source, form, date = _filing_of(inp, row)
    return {"text": text, "source": source, "form": form, "filing_date": date,
            "url": row.get("evidence_url"), "matched_terms": mt if kw else []}


def make_card(n: int, slot: str, c: dict[str, Any], inp: dict[str, Any], sieve: dict[str, Any] | None, *,
              pi: float | None, max_out: int, why_key: str | None = None) -> dict[str, Any]:
    """One cards.json card (see card_format in the spec); why_key picks another WHY_ZH text than the slot's."""
    fz = _facets_zh(sieve)
    target = fz.get("target") or "想法里的东西"
    what_text = _first_sentence(c.get("l1_description"), WHAT_MAX_CHARS)
    tier = c.get("l1_input_tier")
    if what_text is None and (inp.get("evidence") or c.get("l2_evidence")) == "profile":
        what_text, tier = _first_sentence(_body(inp.get("text")), WHAT_MAX_CHARS), tier or c.get("l2_input_tier")
    p, pe, pp = c.get("l2_p_pos"), c.get("l2_p_explicit"), c.get("l2_p_partial")
    profile = (inp.get("evidence") or c.get("l2_evidence")) == "profile"
    why = WHY_ZH[why_key or slot].format(rank=c.get("rank"), target=target, pi=pi or 0.0, p=p or 0.0,
                                         max_out=max_out, src="简介" if profile else "年报")
    reads = [d.get("p_pos") for d in c.get("l2_read_detail") or [] if isinstance(d, dict)
             and d.get("p_pos") is not None]
    return {"n": n, "type": slot, "company_key": c["company_key"], "security_id": c.get("security_id"),
            "name": c.get("name"), "country": c.get("country"), "rank": c.get("rank"),
            "pi": None if pi is None else round(pi, 2),
            "what": {"text": what_text, "source": "公司简介", "tier": tier},
            "quote": card_quote(inp, c),
            "verdict": {"label": c.get("l2_label"), "p_pos": _r2(p), "p_explicit": _r2(pe), "p_partial": _r2(pp),
                        "reads": [_r2(x) for x in reads], "sd": _r2(c.get("l2_p_pos_sd")),
                        "edge": bool(c.get("l2_edge"))},
            "gap": f"年报摘录没写到{target}" if slot in ("gap", "scope") else None,
            "why_zh": why, "evidence_sha": inp.get("evidence_sha") or c.get("evidence_sha"),
            "chips": list(YES_CHIPS + NO_CHIPS)}


def _r2(v: float | None) -> float | None:
    return None if v is None else round(float(v), 2)


def deck_chips(sieve: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """The chip texts of a deck: g in the words of the facets' type (technology: 只有大类（X），没提 Y; geography /
    customer: 做 X，但不面向 Y), or generic without facets; h-k the role chips."""
    fz = _facets_zh(sieve)
    yes = {"a": f"直接做（{fz.get('mechanism') or '想法本身'}）", "b": "相关，算大类"}
    no = {k: NO_CHIP_TEXT[k] for k in ("c", "d", "e", "f")}
    kind = facet_type(_facets(sieve))
    if kind in ("geography", "customer"):
        no["g"] = f"做{fz.get('category')}，但不面向{fz.get('target')}"
    elif kind:
        no["g"] = f"只有大类（{fz.get('category')}），没提{fz.get('target')}"
    else:
        no["g"] = G_CHIP_GENERIC_ZH
    no.update({k: NO_CHIP_TEXT[k] for k in ("h", "i", "j", "k")})
    return {"yes": yes, "no": no}


def build_deck(result: dict[str, Any], inputs: Any, sieve: dict[str, Any] | None, *, max_cards: int = CARDS_MAX,
               pool: Iterable[dict[str, Any]] | None = None, deck_n: int | None = None, now: str | None = None,
               seed: int | None = None, draws: int = PI_DRAWS) -> dict[str, Any]:
    """cards.json (jevscreen.cards/1) of a run: select_cards plus the deck header (deck_id 'deck-<run_id>-<n>', n =
    the run's decks in the sieve + 1; stabilize = the run's band reads; chips)."""
    from . import keywords
    run_id = result.get("run_id") or "?"
    if deck_n is None:
        deck_n = 1 + sum(1 for d in (sieve or {}).get("decks") or [] if isinstance(d, dict)
                         and d.get("run_id") == run_id)
    band = ((result.get("layers") or {}).get("l2") or {}).get("band")
    cards = select_cards(result, inputs, sieve, max_cards=max_cards, seed=seed, pool=pool, draws=draws)
    return {"format": CARDS_FORMAT, "deck_id": f"deck-{run_id}-{deck_n}", "run_id": run_id,
            "idea": result.get("idea"), "idea_key": keywords.idea_key(result.get("idea") or ""),
            "created_at": now or now_iso(), "facets_en": _facets(sieve),
            "stabilize": ({"items": band.get("items"), "reads_added": band.get("ok"),
                           "cost_usd": band.get("cost_usd")} if band else None),
            "chips": deck_chips(sieve), "cards": cards}


def _pct(v: float | None) -> str:
    return "?" if v is None else f"{v:.0%}"


def _legend_no(chip: str, text: str | None) -> str:
    """The legend word of a 'no' chip: g in the words of this deck's chip (geography / customer decks: g不面向<target>,
    deck_chips' '做 X，但不面向 Y'), else LEGEND_NO."""
    if chip == "g" and text and "但不面向" in text:
        target = text.split("但不面向", 1)[1].strip()
        return "g不面向" + (target if len(target) <= 10 else target[:10] + "…")
    return LEGEND_NO[chip]


def render_cards_md(deck: dict[str, Any]) -> str:
    """cards.md (Chinese): a header and the answer legend once, then 4 lines per card (做什么 / 年报 / 系统, plus 缺口
    when set), then the licence note. An empty deck is the single line 这次没有需要你判断的卡."""
    cards = deck.get("cards") or []
    if not cards:
        return EMPTY_DECK_ZH + "\n"
    idea = (deck.get("idea") or "").strip()
    head = idea[:30] + ("…" if len(idea) > 30 else "")
    have = (deck.get("chips") or {}).get("no") or {}
    legend_no = " ".join(_legend_no(k, have.get(k)) for k in NO_CHIPS if k in have or (k in "cdef" and not have))
    lines = [f"校准卡 · {head}（{len(cards)} 张，约 1 分钟）",
             f"回答：1要a 2不要c 3? …  要a=直接做 要b=相关算大类 | 不要 {legend_no} | ?=不确定；不答=跳过", ""]
    gray = official = False
    for c in cards:
        pos = f"现排第{c['rank']}" if c.get("rank") else "未入选"
        lines.append(f"[{c['n']}] {_display(c)} · {pos} — {c.get('why_zh')}")
        w = c.get("what") or {}
        if w.get("text"):
            priv = "，仅供个人使用" if w.get("tier") == "gray-private" else ""
            gray = gray or w.get("tier") == "gray-private"
            lines.append(f"  做什么：{w['text']}（{w.get('source') or '公司简介'}{priv}）")
        else:
            lines.append("  做什么：（没有简介）")
        q = c.get("quote") or {}
        if q.get("source") == "公司简介":
            lines.append(f"  年报：{NO_REPORT_ZH}")
            if q.get("text") and q["text"] != w.get("text"):
                lines.append(f"  简介：「{q['text']}」")
        else:
            official = True
            year = str(q.get("filing_date") or "")[:4]
            form = FORM_ZH.get(q.get("form") or "", q.get("form"))
            where = " ".join(x for x in (q.get("source"), form) if x) + (f"，{year} 年发布" if year else "")
            lines.append(f"  年报：「{q.get('text') or ''}」（{where}）")
        v = c.get("verdict") or {}
        label = v.get("label")
        n_reads = len(v.get("reads") or []) or 1
        if label == "partial":
            sysl = f"相关 {_pct(v.get('p_partial'))}（明确 {_pct(v.get('p_explicit'))}，读 {n_reads} 次）"
        elif label == "explicit":
            sysl = f"明确符合 {_pct(v.get('p_explicit'))}（相关 {_pct(v.get('p_partial'))}，读 {n_reads} 次）"
        else:
            sysl = (f"{LABEL_ZH.get(label, label or '?')}（明确 {_pct(v.get('p_explicit'))}，相关 "
                    f"{_pct(v.get('p_partial'))}，读 {n_reads} 次）")
        cap = f" · {screen.PROFILE_CAP_ZH}（最多算相关）" if (q.get("source") == "公司简介"
                                                         and label in screen.L2_VERIFIED) else ""
        lines.append(f"  系统：{sysl}" + (" · 边缘" if v.get("edge") else "") + cap)
        if c.get("gap"):
            lines.append(f"  缺口：{c['gap']}")
    lines.append("")
    if gray:
        lines.append(LICENCE_GRAY_ZH)
    if official:
        lines.append(LICENCE_OFFICIAL_ZH)
    return "\n".join(lines).rstrip() + "\n"


def write_deck(deck: dict[str, Any], out_dir: str | Path) -> tuple[Path, Path]:
    """cards.json and cards.md in the run's output directory."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pj, pm = out / "cards.json", out / "cards.md"
    pj.write_text(json.dumps(deck, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    pm.write_text(render_cards_md(deck), encoding="utf-8")
    return pj, pm


def load_deck(path: str | Path) -> dict[str, Any]:
    """cards.json (a file or a run's output directory). ValueError when missing or not jevscreen.cards/1."""
    p = Path(path)
    if p.is_dir():
        p = p / "cards.json"
    try:
        deck = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"卡组 {p} 读不了（{type(e).__name__}）") from None
    if not isinstance(deck, dict) or deck.get("format") != CARDS_FORMAT:
        raise ValueError(f"{p} 不是 {CARDS_FORMAT} 卡组")
    return deck


# ---------------------------------------------------------------------------------------------------------------
# Answers

class Answer(NamedTuple):
    n: int
    verdict: str            # yes | no | unsure
    level: str | None       # explicit | partial (yes only)
    chip: str | None


class AnswerError(ValueError):
    """A bad answer text or file (the message is the Chinese hint, or English with lang='en'; the CLI exits 1)."""


# parse_answers errors per language (same keys in both; answer --lang en picks the English ones)
ANSWER_ERRORS: dict[str, dict[str, str]] = {
    "zh": {
        "empty": "没有读到任何回答：请写成 1要a 2不要c 3? 这样的格式",
        "where_card": "第 {n} 张：", "where_rest": "其余：",
        "unsure_chip": "{where}「?」不能带理由字母 {chip}",
        "no_with_yes_chip": "{where}{chip} 是「要」的理由，不能配「不要」（不要 用 c–k）",
        "yes_with_no_chip": "{where}{chip} 是「不要」的理由，不能配「要」（要 用 a 或 b）",
        "chip_not_in_deck": "{where}这副卡没有 {chip}（旧版卡组，没有这个理由字母）",
        "unreadable": "看不懂「{tok}」：请写成 1要a、2不要c、3? 这样的格式",
        "no_card": "没有第 {n} 张卡（这副卡的编号是 {valid}）",
        "two_verdicts": "第 {n} 张：「{tok}」同时写了要/不要/?，只能选一个",
        "no_verdict": "第 {n} 张：「{tok}」没写要还是不要（例如 {n}要a 或 {n}不要c）",
        "twice": "第 {n} 张答了两次，用最后一次（{tok}）",
        "no_cards": "（没有卡）",
    },
    "en": {
        "empty": "No answers found: write them like 1a 2no 3c 4?",
        "where_card": "Card {n}: ", "where_rest": "The rest: ",
        "unsure_chip": "{where}'?' cannot take a reason letter ({chip})",
        "no_with_yes_chip": "{where}{chip} is a reason for yes, not for no (no takes c-k)",
        "yes_with_no_chip": "{where}{chip} is a reason for no, not for yes (yes takes a or b)",
        "chip_not_in_deck": "{where}this deck has no {chip} (an older deck without this reason letter)",
        "unreadable": "Cannot read '{tok}': write answers like 1a, 2no, 3c, 4?",
        "no_card": "There is no card {n} (this deck has cards {valid})",
        "two_verdicts": "Card {n}: '{tok}' says more than one of yes/no/?; pick one",
        "no_verdict": "Card {n}: '{tok}' does not say yes or no (e.g. {n}a or {n}no)",
        "twice": "Card {n} was answered twice; the last one counts ({tok})",
        "no_cards": "(no cards)",
    },
}


_ANSWER_TOKEN = re.compile(r"^(\d{1,2})[.:：号)]?\s*(要|是|y|yes|留|对)?(不要|否|n|no|删)?(\?|不确定|跳过|skip)?\s*"
                           r"([a-k])?$")
_REST_TOKEN = re.compile(r"^(?:其余|其他|剩下|剩余)(?:的)?(都)?(不要|要)([a-k])?$")
_SPLIT = re.compile(r"[\s,，、；;。.!！]+")                 # also full stops ("1要b。2不要e", "3? …" -> "3?")
_NUM_ONLY = re.compile(r"^\d{1,2}[:：号)]?$")
_CHIP_ONLY = re.compile(r"^[a-k]$")
_VERDICT_ONLY = re.compile(r"^(要|是|y|yes|留|对|不要|否|n|no|删|\?|不确定|跳过|skip)$")


def _tokens(text: str) -> list[str]:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"(?<=[^\d\s.:：号),，、；;。!！])(?=\d)", " ", t)      # "1要a2不要c" -> "1要a 2不要c"
    toks = [x for x in _SPLIT.split(t) if x]
    out: list[str] = []
    for x in toks:
        prev = out[-1] if out else ""
        if prev and _NUM_ONLY.match(prev) and (_CHIP_ONLY.match(x) or _VERDICT_ONLY.match(x)
                                              or _ANSWER_TOKEN.match(prev + x)):
            out[-1] += x                                            # "1. 要a" -> "1要a", "1 要" -> "1要"
        elif prev and _CHIP_ONLY.match(x) and (m := _ANSWER_TOKEN.match(prev)) and m.group(5) is None \
                and not _NUM_ONLY.match(prev):
            out[-1] += x                                            # "1不要 c" / "1 要 b" -> "1不要c" / "1要b"
        else:
            out.append(x)
    return out


def parse_answers(text: str, deck: dict[str, Any], warnings: list[str] | None = None, *, lang: str = "zh"
                  ) -> list[Answer]:
    """The answers in `text` for `deck`, sorted by card number (cards not mentioned are skipped).

    NFKC first; tokens are split on whitespace , ， 、 ； ; and newlines (and before a card number glued to the
    previous answer). Forms: 1要a (explicit) · 1要b (partial) · 1要 (the evidence label when it is verified, else
    partial) · 1不要 / 1不要c (no, chip c-k: c-g the evidence, h-k the company's role) · 1? (unsure) · 1跳过 / 1skip (skipped, like a card not
    mentioned) · 1a / 1c (the chip alone says yes / no) · 其余都要 / 其余不要 (every card not answered yet). Raises AnswerError with a Chinese hint for an unknown card number, a
    yes-chip on a no (or the reverse), a letter the deck does not offer (an older deck: g without facets, h-k), or a
    token it cannot read. A card answered twice keeps its last answer and a warning is appended to `warnings`.
    lang='en' gives the English messages (ANSWER_ERRORS)."""
    E = ANSWER_ERRORS["en" if lang == "en" else "zh"]
    cards = {int(c["n"]): c for c in deck.get("cards") or []}
    valid = ("、" if lang != "en" else ", ").join(str(n) for n in sorted(cards)) or E["no_cards"]
    deck_no = set(((deck.get("chips") or {}).get("no") or {})) | {"c", "d", "e", "f"}
    toks = _tokens(text)
    if not toks:
        raise AnswerError(E["empty"])
    got: dict[int, Answer | None] = {}
    rest: tuple[str, str | None] | None = None

    def level_for(n: int, chip: str | None) -> str:
        if chip == "a":
            return "explicit"
        if chip == "b":
            return "partial"
        label = ((cards[n].get("verdict") or {}).get("label"))
        return label if label in screen.L2_VERIFIED else "partial"

    def check_chip(n: int | None, verdict: str, chip: str | None) -> None:
        where = E["where_card"].format(n=n) if n is not None else E["where_rest"]
        if chip is None:
            return
        if verdict == "unsure":
            raise AnswerError(E["unsure_chip"].format(where=where, chip=chip))
        if verdict == "no" and chip in YES_CHIPS:
            raise AnswerError(E["no_with_yes_chip"].format(where=where, chip=chip))
        if verdict == "yes" and chip in NO_CHIPS:
            raise AnswerError(E["yes_with_no_chip"].format(where=where, chip=chip))
        if chip in NO_CHIPS and chip not in deck_no:
            raise AnswerError(E["chip_not_in_deck"].format(where=where, chip=chip))

    for tok in toks:
        m = _REST_TOKEN.match(tok)
        if m:
            verdict = "no" if m.group(2) == "不要" else "yes"
            check_chip(None, verdict, m.group(3))
            rest = (verdict, m.group(3))
            continue
        m = _ANSWER_TOKEN.match(tok)
        if not m:
            raise AnswerError(E["unreadable"].format(tok=tok))
        n, yes, no, unsure, chip = int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5)
        if n not in cards:
            raise AnswerError(E["no_card"].format(n=n, valid=valid))
        if sum(bool(x) for x in (yes, no, unsure)) > 1:
            raise AnswerError(E["two_verdicts"].format(n=n, tok=tok))
        if unsure in ("跳过", "skip") and not (yes or no or chip):
            if n in got and warnings is not None:
                warnings.append(E["twice"].format(n=n, tok=tok))
            got[n] = None                      # skipped: not recorded, and 其余 does not cover it
            continue
        if yes:
            verdict = "yes"
        elif no:
            verdict = "no"
        elif unsure:
            verdict = "unsure"
        elif chip:
            verdict = "yes" if chip in YES_CHIPS else "no"
        else:
            raise AnswerError(E["no_verdict"].format(n=n, tok=tok))
        check_chip(n, verdict, chip)
        ans = Answer(n, verdict, level_for(n, chip) if verdict == "yes" else None, chip)
        if n in got and warnings is not None:
            warnings.append(E["twice"].format(n=n, tok=tok))
        got[n] = ans
    if rest is not None:
        verdict, chip = rest
        for n in cards:
            if n not in got:
                got[n] = Answer(n, verdict, level_for(n, chip) if verdict == "yes" else None, chip)
    return [got[n] for n in sorted(got) if got[n] is not None]


def answer_tokens(deck: dict[str, Any]) -> dict[int, dict[str, str]]:
    """{card n: {choice: token}} for the result page's buttons: 'yes' -> '1yes', 'a' -> '1a', ..., 'no' -> '1no',
    'c' -> '1c', ..., '?' -> '1?'. Only the chips the card allows (and g only when the deck has it). The tokens are
    language-neutral and parse_answers reads them (the page only joins them with spaces, so there is no second
    answer grammar in JavaScript)."""
    has_g = "g" in ((deck.get("chips") or {}).get("no") or {})
    out: dict[int, dict[str, str]] = {}
    for c in deck.get("cards") or []:
        n = int(c["n"])
        allowed = [x for x in (c.get("chips") or list(YES_CHIPS + NO_CHIPS)) if x != "g" or has_g]
        t = {"yes": f"{n}yes", "no": f"{n}no", "?": f"{n}?"}
        for chip in YES_CHIPS + NO_CHIPS:
            if chip in allowed:
                t[chip] = f"{n}{chip}"
        out[n] = t
    return out


IDEA_EN_MAX_CHARS = 400

# Ordinary English words that are also (short) company names in the universe ('Core', 'Main', 'Global', 'Energy'
# ...). In an English idea sentence a single such word is never taken for a company name, capitalised or not (a
# sentence starts with a capital: 'Core suppliers of ...'). Multi-word names ('Applied Materials') still count.
IDEA_EN_COMMON_WORDS = frozenset("""
about above access across active advanced advantage aero aerospace after agile agri air alliance alpha alternative
american analog analytics anchor apex applied apps architecture arena art asset assets atlas audio auto automated
automation automotive avenue axis balance bank base basic battery batteries beacon best beta better beyond big bio
blue board bold bond bright brands bridge broad build builders building business cable capital carbon care cargo
catalyst cell cells central century chain champion change charge chemical chemicals choice circle city civil clean
clear climate cloud coast code cold commerce commercial common communications community compact companies company
compass component components computer computing concept connect connected consumer contact control controls cool
cooling core corner cosmos craft create creative critical cross crown crystal cube current custom cyber data dawn
defense delivery design designs develop development device devices digital direct discovery diversified domain
dragon drive dynamic dynamics eagle early earth east eastern eco edge education electric electrical electronic
electronics element elite emerging energy engine engineering enterprise enterprises entertainment environmental
equipment equity essential ever evolution excel exchange expert express fabric factory farm fast fidelity field
finance financial first fiber fibre flex flow fluid focus food foods force forest forward foundation frame
freedom fresh frontier fuel fusion future galaxy garden gas gate gateway gear general generation genesis global
gold golden good great green grid ground group growth guard harbor harbour harmony harvest health healthcare heat
heavy helix heritage high highway home homes horizon hub human hydro hydrogen ideal image impact industrial
industries industry infinity information infrastructure innovation innovative insight instruments insurance
integrated intelligence intelligent interactive international internet invest investment investments iron island
key kinetic labs land laser leader leading legacy level liberty life light lightning line link liquid logic
logistics lotus machine machinery machines main major marine market markets master material materials matrix
maximum media medical mega mercury meta metal metals micro mind mining mobile mobility modern module modular
momentum motion motor motors mountain national native natural nature navigator network networks new next noble
north northern nova nuclear ocean office omega one online open optical optics orbit origin pacific packaging paper
paragon park partner partners path peak performance pharma pioneer pipe pipeline pixel planet plant plastic
plastics platform plus point polar power precision premier premium prime pro process product products progress
project property protect pulse pure quality quantum quest radiant rapid real reliance renewable renewables
research resource resources retail ridge river road robot robotics rock royal safe safety science sciences
scientific sea secure security select semiconductor semiconductors sense sensor sensors service services shield
signal silicon silver simple sky smart social software solar solid solution solutions sonic sound source south
southern space spark spectrum sphere spirit spring square standard star stars station stations steel stellar
storage strategic stream strong structure summit sun sunrise super supply supplier suppliers support supreme
sustainable swift synergy system systems target team tech techno technical technologies technology terra textile
thermal thermo titan tools top total tower trade trans transport travel tree trend trust ultra union united
unity universal urban utility utilities valley value vantage vector velocity venture ventures vertex view vision
vista vital volt water wave way west western wind wireless wood works world zenith zero
""".split())
# Acronyms an idea sentence may use that also happen to be 4-letter tickers (never taken for a ticker).
IDEA_EN_ACRONYMS = frozenset("BESS HVAC EVSE LIDAR PCBA PEMFC SOFC HBM CCUS DRAM NAND ASIC FPGA MEMS".split())
_NAME_HIT = re.compile(r"^(?:含公司名|含股票代码) (.+)$")
# The words a fix of a refused sentence may swap for each other and still mean the same ('Core' -> 'Main').
IDEA_EN_SYNONYMS = frozenset("core main key leading major primary".split())
# Well-known companies an idea in Chinese / Japanese / Korean names in its own script: a faithful English sentence
# may name them in English (特斯拉 -> Tesla). Keys are matched as substrings of the idea; values are English names or
# tickers, matched case-insensitively against a flagged name (whole name, or its first words).
IDEA_EN_ALIASES: dict[str, tuple[str, ...]] = {
    "特斯拉": ("Tesla",), "テスラ": ("Tesla",), "테슬라": ("Tesla",),
    "英伟达": ("NVIDIA",), "輝達": ("NVIDIA",), "辉达": ("NVIDIA",), "英偉達": ("NVIDIA",), "エヌビディア": ("NVIDIA",),
    "엔비디아": ("NVIDIA",),
    "苹果": ("Apple",), "蘋果": ("Apple",), "アップル": ("Apple",), "애플": ("Apple",),
    "宁德时代": ("CATL", "Contemporary Amperex"), "寧德時代": ("CATL", "Contemporary Amperex"),
    "比亚迪": ("BYD",), "比亞迪": ("BYD",), "华为": ("Huawei",), "華為": ("Huawei",), "小米": ("Xiaomi",),
    "台积电": ("TSMC", "Taiwan Semiconductor"), "台積電": ("TSMC", "Taiwan Semiconductor"),
    "中芯国际": ("SMIC", "Semiconductor Manufacturing International"),
    "三星": ("Samsung",), "삼성": ("Samsung",), "现代": ("Hyundai",), "現代": ("Hyundai",), "현대": ("Hyundai",),
    "海力士": ("SK Hynix", "Hynix"), "하이닉스": ("SK Hynix", "Hynix"),
    "微软": ("Microsoft",), "微軟": ("Microsoft",), "谷歌": ("Google", "Alphabet"), "亚马逊": ("Amazon",),
    "亞馬遜": ("Amazon",), "英特尔": ("Intel",), "英特爾": ("Intel",), "高通": ("Qualcomm",), "博通": ("Broadcom",),
    "超威": ("AMD", "Advanced Micro Devices"), "阿斯麦": ("ASML",), "艾司摩尔": ("ASML",), "腾讯": ("Tencent",),
    "騰訊": ("Tencent",), "阿里巴巴": ("Alibaba",), "阿里": ("Alibaba",), "京东": ("JD.com", "JD"),
    "美团": ("Meituan",), "拼多多": ("PDD", "Pinduoduo"), "百度": ("Baidu",), "字节跳动": ("ByteDance",),
    "丰田": ("Toyota",), "豐田": ("Toyota",), "トヨタ": ("Toyota",), "本田": ("Honda",), "ホンダ": ("Honda",),
    "索尼": ("Sony",), "ソニー": ("Sony",), "任天堂": ("Nintendo",), "发那科": ("FANUC",), "ファナック": ("FANUC",),
    "基恩士": ("Keyence",), "キーエンス": ("Keyence",), "松下": ("Panasonic",), "パナソニック": ("Panasonic",),
    "西门子": ("Siemens",), "博世": ("Bosch",), "宝马": ("BMW",), "奔驰": ("Mercedes-Benz", "Mercedes"),
    "大众": ("Volkswagen",), "波音": ("Boeing",), "空客": ("Airbus",), "英飞凌": ("Infineon",),
    "美的": ("Midea",), "格力": ("Gree",), "海尔": ("Haier",), "京东方": ("BOE",), "隆基": ("LONGi",),
    "阳光电源": ("Sungrow",), "立讯": ("Luxshare",), "蔚来": ("NIO",), "小鹏": ("XPeng",), "理想汽车": ("Li Auto",),
    "海康威视": ("Hikvision",), "大疆": ("DJI",), "中际旭创": ("Innolight",), "新易盛": ("Eoptolink",),
}


def _load_words() -> frozenset[str]:
    try:
        text = (Path(__file__).with_name("words_en.txt")).read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    return frozenset(w.strip() for w in text.splitlines() if w.strip() and not w.startswith("#"))


_ORDINARY_WORDS = _load_words() | IDEA_EN_COMMON_WORDS


def ordinary_word(word: str) -> bool:
    """Is `word` an ordinary English word (any case; plurals too): the bundled list words_en.txt (public-domain
    dictionary words that occur in lower case in business text) or IDEA_EN_COMMON_WORDS."""
    w = (word or "").strip().lower()
    if not w.isascii() or not w.isalpha():
        return False
    words = _ORDINARY_WORDS
    if w in words:
        return True
    if w.endswith("ies") and w[:-3] + "y" in words:
        return True
    if w.endswith("es") and w[:-2] in words:
        return True
    return w.endswith("s") and w[:-1] in words


def _common_word_name(short: str) -> bool:
    """A universe short name that is one ordinary English word ('Core', 'MAIN' excluded: case matters later)."""
    words = _WORD.findall(short)
    return len(words) == 1 and words[0] == short.strip() and short.strip().lower() in IDEA_EN_COMMON_WORDS


def _capitalised_word_name(short: str) -> bool:
    """A single-word name written like a capitalised ordinary word ('Immersion', 'Harmonic'): its capital letter is
    evidence of a company name only where a sentence would not have it anyway."""
    s = short.strip()
    return bool(re.fullmatch(r"[A-Z][a-z]+", s)) and ordinary_word(s)


_SENTENCE_START = re.compile(r"(?:^|[.!?;:]\s+)[\"'“‘(\[]*$")


def _title_case(text: str) -> bool:
    """Most longer words after the first are capitalised ('Suppliers Of Harmonic Reducers'): capitals are style."""
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", text)[1:]
    long = [w for w in words if len(w) >= 4]
    return len(long) >= 2 and sum(1 for w in long if w[0].isupper()) / len(long) >= 0.6


def _capital_is_evidence(text: str, short: str) -> bool:
    """Does `short` (a capitalised ordinary word) appear capitalised somewhere a sentence would not capitalise it?"""
    if _title_case(text):
        return False
    for m in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(short)}(?![A-Za-z0-9])", text):
        if not _SENTENCE_START.search(text[:m.start()]):
            return True
    return False


def _idea_aliases(idea: str) -> list[str]:
    """English names / tickers the idea names in Chinese, Japanese or Korean (IDEA_EN_ALIASES)."""
    i = unicodedata.normalize("NFKC", idea or "")
    return [a.lower() for k, v in IDEA_EN_ALIASES.items() if k in i for a in v]


def _alias_allows(hit: str, aliases: list[str]) -> bool:
    h = hit.strip().lower()
    return any(h == a or h.startswith(a + " ") or a.startswith(h + " ") for a in aliases)


def idea_en_name_hits(text: str | None, idea: str = "", universe_names: Iterable[str] = (),
                      tickers: Iterable[str] = ()) -> list[str]:
    """The company names / tickers of the universe that `text` names and the idea does not (case-sensitive
    proper-noun matching as in validate_rule_text). Never counted: a single ordinary English word such as 'Core', a
    capitalised ordinary word where a sentence would capitalise it anyway ('Immersion cooling ...', after '. ' or in
    Title Case), a technical acronym such as BESS, and a company the idea names in its own script (特斯拉 -> Tesla)."""
    t = unicodedata.normalize("NFKC", (text or "").strip())
    idea_l = unicodedata.normalize("NFKC", idea or "").lower()
    names = [n for n in universe_names if n and _short_name(n).lower() not in idea_l
             and not _common_word_name(_short_name(n))]
    ticks = [x for x in tickers if x and x.lower() not in idea_l and x.strip().upper() not in IDEA_EN_ACRONYMS
             and x.strip().upper() not in GENERIC_ACRONYMS]
    if not t or not (names or ticks):
        return []
    aliases = _idea_aliases(idea)
    hits = []
    for p in validate_rule_text(t, names=names, tickers=ticks):
        m = _NAME_HIT.match(p)
        if not m:
            continue
        h = m.group(1)
        if aliases and _alias_allows(h, aliases):
            continue
        if p.startswith("含公司名") and _capitalised_word_name(h) and not _capital_is_evidence(t, h):
            continue
        hits.append(h)
    return list(dict.fromkeys(hits))


def idea_en_problems(text: str | None, idea: str = "", universe_names: Iterable[str] = (),
                     tickers: Iterable[str] = ()) -> list[str]:
    """Why an agent-written English idea (quickstart --idea-en) cannot be used, as short English reasons (empty:
    usable): one line, at most IDEA_EN_MAX_CHARS characters, mostly Latin letters, and no company name or ticker
    from the universe unless the original idea names it too (idea_en_name_hits: case-sensitive, a capital letter a
    sentence would have anyway never counts, a company the idea names in Chinese may be named in English)."""
    t = (text or "").strip()
    if not t:
        return ["empty"]
    out: list[str] = []
    if "\n" in t or "\r" in t:
        out.append("must be one line")
    if len(t) > IDEA_EN_MAX_CHARS:
        out.append(f"longer than {IDEA_EN_MAX_CHARS} characters")
    letters = [ch for ch in t if ch.isalpha()]
    latin = sum(1 for ch in letters if ch.isascii())
    if letters and latin / len(letters) < 0.8:
        out.append("not mostly English (Latin letters)")
    hits = idea_en_name_hits(t, idea, universe_names, tickers)
    if hits:
        out.append("names a company or ticker that the idea does not: " + "; ".join(hits))
    return out


_LEAD_IN = r"(?:such\s+as|like|including|incl\.|e\.g\.,?|for\s+example|for\s+instance|notably|especially)"


def suggest_idea_en(text: str, hits: Iterable[str]) -> str | None:
    """A best-effort rewrite of `text` without the names in `hits`, only where they are examples ('..., such as X
    and Y' / '(e.g. X)' / '(X)'), for the agent to check and reuse. None when a name is part of the phrase itself
    ('X supply chain', 'suppliers to X', "X's robot": dropping it would change the meaning or break the sentence),
    when too little is left, or when nothing changed."""
    t = " ".join((text or "").split())
    names = sorted({h for h in hits if h}, key=len, reverse=True)
    if not t or not names:
        return None
    alt = "|".join(re.escape(n) for n in names)
    one = rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])"
    if re.search(rf"(?<![A-Za-z0-9])(?:{alt})['’]", t):
        return None                  # a possessive: cutting the name leaves "'s robot"
    t = re.sub(rf"\s*\(\s*(?:{_LEAD_IN}\s+)?{one}(?:\s*(?:,|and|or|&)\s*{one})*\s*\)", "", t)
    t = re.sub(rf",?\s*{_LEAD_IN}\s+{one}(?:\s*(?:,|and|or|&)\s*{one})*(?:\s*,)?", "", t, flags=re.IGNORECASE)
    if re.search(one, t):
        return None                  # the name carries the phrase: the agent writes a new sentence
    t = re.sub(r"\s+([,.;:])", r"\1", " ".join(t.split())).strip(" ,;:")
    t = re.sub(r",\s*,", ",", t)
    if len(re.findall(r"[A-Za-z]{2,}", t)) < 3:
        return None                  # too little left to carry the idea: the agent writes a new sentence
    t = t[0].upper() + t[1:]
    return None if t == " ".join((text or "").split()) else t


def idea_en_minor_fix(old: str | None, new: str | None, flagged: Iterable[str]) -> bool:
    """Is `new` the refused `old` sentence with only a word that was mistaken for a company name changed, so the
    meaning the human approved is kept? Only when each flagged word that changed is an ordinary English word and it
    was recased ('Harmonic' -> 'harmonic') or swapped within IDEA_EN_SYNONYMS ('Core' -> 'Main'), nothing else
    touched. Dropping or swapping a real company name, or any other edit, is a new English sentence (the reprice
    rule)."""
    a = _WORD.findall(unicodedata.normalize("NFKC", old or ""))
    b = _WORD.findall(unicodedata.normalize("NFKC", new or ""))
    bad = {f.strip().lower() for f in flagged if f and len(_WORD.findall(f)) == 1 and ordinary_word(f.strip())}
    if not a or not b or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        xl, yl = x.lower(), y.lower()
        if xl not in bad:
            return False
        if xl == yl or (xl in IDEA_EN_SYNONYMS and yl in IDEA_EN_SYNONYMS):
            continue
        return False
    return True


def answers_from_file(data: Any, deck: dict[str, Any], warnings: list[str] | None = None) -> list[Answer]:
    """--file answers.json: {"deck_id", "answers": {"1": {"v": "yes|no|unsure", "chip": "b"}}}, checked like
    parse_answers (the same Chinese errors)."""
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise AnswerError('answers.json 需要 {"deck_id": …, "answers": {"1": {"v": "yes", "chip": "b"}}}')
    if data.get("deck_id") and data["deck_id"] != deck.get("deck_id"):
        raise AnswerError(f"answers.json 的 deck_id {data['deck_id']} 不是当前卡组 {deck.get('deck_id')}")
    parts = []
    for n, a in sorted(data["answers"].items(), key=lambda kv: str(kv[0])):
        if not isinstance(a, dict) or a.get("v") not in ("yes", "no", "unsure"):
            raise AnswerError(f"answers.json 第 {n} 张：v 必须是 yes / no / unsure")
        word = {"yes": "要", "no": "不要", "unsure": "?"}[a["v"]]
        parts.append(f"{n}{word}{a.get('chip') or ''}")
    if not parts:
        raise AnswerError("answers.json 里没有回答")
    return parse_answers(" ".join(parts), deck, warnings)


def answers_json(deck: dict[str, Any], answers: list[Answer]) -> dict[str, Any]:
    """The audit copy (answers.json in the run's output directory), in the --file format."""
    return {"deck_id": deck.get("deck_id"),
            "answers": {str(a.n): {"v": a.verdict, "chip": a.chip, "level": a.level} for a in answers}}


def record_answers(sieve: dict[str, Any], deck: dict[str, Any], answers: list[Answer], *,
                   now: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(sieve', new examples): each answer appended to sieve.examples as {security_id, company_key, name, want
    (explicit | partial | no | unsure), chip, source 'card', pin (want != unsure), run_id, deck_id, card_n,
    evidence_sha, evidence_filing (filing_key of the card's quote), answered_at}. An earlier card answer for the same company moves to sieve.history (the user's AI's
    source-'sieve' checks stay). The deck is recorded in sieve.decks. Not saved: call save_sieve."""
    now = now or now_iso()
    sv = json.loads(json.dumps(sieve))
    cards = {int(c["n"]): c for c in deck.get("cards") or []}
    new = []
    for a in answers:
        c = cards[a.n]
        want = a.level if a.verdict == "yes" else a.verdict
        ex = {"security_id": c.get("security_id"), "company_key": c.get("company_key"), "name": c.get("name"),
              "want": want, "chip": a.chip, "source": "card", "pin": want != "unsure", "run_id": deck.get("run_id"),
              "deck_id": deck.get("deck_id"), "card_n": a.n, "card_type": c.get("type"),
              "evidence_sha": c.get("evidence_sha"), "answered_at": now}
        q = c.get("quote") or {}
        if q.get("source"):
            ex["evidence_filing"] = filing_key(q.get("source"), q.get("form"), q.get("filing_date"))
        keep = []
        for old in sv.get("examples") or []:
            same = isinstance(old, dict) and old.get("source", "card") == "card" and (
                (old.get("company_key") and old.get("company_key") == ex["company_key"])
                or (old.get("security_id") and old.get("security_id") == ex["security_id"]))
            if same:
                sv.setdefault("history", []).append({**old, "superseded_at": now, "superseded_by": deck.get("deck_id")})
            else:
                keep.append(old)
        sv["examples"] = keep + [ex]
        new.append(ex)
    decks = [d for d in sv.get("decks") or [] if not (isinstance(d, dict) and d.get("deck_id") == deck.get("deck_id"))]
    decks.append({"deck_id": deck.get("deck_id"), "run_id": deck.get("run_id"), "cards": len(cards),
                  "answered": len(answers), "cost_usd": (deck.get("stabilize") or {}).get("cost_usd"),
                  "answered_at": now})
    sv["decks"] = decks
    sv.setdefault("idea", deck.get("idea"))
    sv.setdefault("idea_key", deck.get("idea_key"))
    return sv, new


def undo_example(sieve: dict[str, Any], n: int, *, now: str | None = None) -> tuple[dict[str, Any], dict]:
    """(sieve', removed): answer n (1-based, as `sieve show` numbers them: the stored examples, never the in-memory
    should_pass / should_fail checks, which `sieve remove` takes out) removed into sieve.history."""
    sv = json.loads(json.dumps(sieve))
    exs = sv.get("examples") or []
    real = [i for i, ex in enumerate(exs) if not (isinstance(ex, dict) and ex.get("_author"))]
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= len(real):
        raise AnswerError(f"没有第 {n} 条回答（现有 {len(real)} 条；应该有/不该有的检查用 jevscreen sieve remove 撤掉）")
    removed = exs.pop(real[n - 1])
    sv["examples"] = exs
    sv.setdefault("history", []).append({**removed, "undone_at": now or now_iso()})
    return sv, removed


# ---------------------------------------------------------------------------------------------------------------
# Rule candidates, trials and adoption

def _rule_id(r: Any) -> str | None:
    return r if isinstance(r, str) else r.get("id") if isinstance(r, dict) else None


def candidate_rules(sieve: dict[str, Any], deck: dict[str, Any], answers: list[Answer], *,
                    names: Iterable[str] = (), tickers: Iterable[str] = (), terms: Iterable[str] = ()
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(candidates, dropped): the library rules the answers' chips point to (c user_not_supplier, d homonym with the
    matched terms of the rejected cards' quotes, e laundry_list, f mention_only, g scope_narrow (generic text without
    facets), h buyer_not_supplier, i holding_only, j upstream_parts, k hardware_to_operators; 要b on a scope card
    scope_broad, only with facets), minus rules already adopted. scope_narrow and scope_broad are
    exclusive: the later answer wins. Each rendered sentence must pass validate_rule_text (names / tickers of the
    universe and of the deck, the deck's quotes; the run's keyword `terms` and the cards' matched terms are exempt
    from the ticker check); a failing one is dropped with its Chinese reason."""
    cards = {int(c["n"]): c for c in deck.get("cards") or []}
    facets = _facets(sieve)
    adopted = {_rule_id(r): r for r in sieve.get("rules") or []}
    found: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for a in answers:
        c = cards.get(a.n) or {}
        rid = None
        if a.verdict == "no" and a.chip in CHIP_RULES:
            rid = CHIP_RULES[a.chip]
        elif a.verdict == "yes" and a.chip == "b" and c.get("type") == "scope":
            rid = "scope_broad"
        if rid is None or (RULES[rid].get("facets_only") and not facets):
            continue
        e = found.get(rid) or {"id": rid, "version": RULES[rid]["version"], "from": []}
        e["from"].append(f"card {a.n} @{deck.get('deck_id')}")
        if rid == "homonym":
            terms = e.setdefault("terms", [])
            for t in (c.get("quote") or {}).get("matched_terms") or []:
                if t not in terms:
                    terms.append(t)
        found[rid] = e
        if rid in order:
            order.remove(rid)
        order.append(rid)
    excl = [r for r in order if r in EXCLUSIVE_RULES]
    dropped: list[dict[str, Any]] = []
    if len(excl) > 1:
        for r in excl[:-1]:
            dropped.append({"id": r, "why_zh": f"和后面的回答（{excl[-1]}）冲突，用后面的"})
            order.remove(r)
    quotes = [(c.get("quote") or {}).get("text") or "" for c in cards.values()]
    all_names = list(names) + [c.get("name") or "" for c in cards.values()]
    all_tickers = list(tickers) + [(c.get("security_id") or "").split(":")[-1] for c in cards.values()]
    own_terms = list(terms) + [t for c in cards.values() for t in (c.get("quote") or {}).get("matched_terms") or []]
    out = []
    for rid in order:
        e = found[rid]
        e["from"] = "; ".join(e["from"])
        old = adopted.get(rid)
        if old is not None and (rid != "homonym" or set(e.get("terms") or []) <= set(
                (old.get("terms") if isinstance(old, dict) else None) or [])):
            dropped.append({"id": rid, "why_zh": "已经采用过"})
            continue
        if rid == "homonym" and isinstance(old, dict):
            e["terms"] = list(dict.fromkeys(list(old.get("terms") or []) + list(e.get("terms") or [])))
        probs = [p for _, _, ps in rule_text_problems([e], facets, names=all_names, tickers=all_tickers,
                                                      quotes=quotes, terms=own_terms) for p in ps]
        if probs:
            dropped.append({"id": rid, "why_zh": "规则文字" + "、".join(dict.fromkeys(probs)) + "，不能进入问题"})
            continue
        out.append(e)
    return out, dropped


def base_reads_of(result: dict[str, Any], pool: Iterable[dict[str, Any]] | None = None,
                  inputs: Any = None) -> dict[str, dict[str, Any]]:
    """{company_key: {security_id, name, p_pos, l2_label, keyword_hit}} of every L2-read company of a run (the base
    p̄ trial_rules compares against)."""
    ins = _inputs_map(inputs)
    out = {}
    for c in _merge_candidates(result, pool):
        if c.get("l2_label") is None:
            continue
        inp = ins.get(c["company_key"]) or {}
        out[c["company_key"]] = {"security_id": c.get("security_id"), "name": c.get("name"),
                                 "p_pos": c.get("l2_p_pos"), "p_pos_sd": c.get("l2_p_pos_sd"),
                                 "l2_label": c.get("l2_label"),
                                 "keyword_hit": inp.get("keyword_hit", c.get("l2_keyword_hit")),
                                 "market_cap_usd": c.get("market_cap_usd")}
    return out


def _is_yes(want: str | None) -> bool:
    return want in ("explicit", "partial")


def _trial_pool(sieve: dict[str, Any], base_reads: dict[str, dict[str, Any]], ins: dict[str, dict[str, Any]]
                ) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """(E, anchors) of a rule trial: E = {company_key: {want, source}} of the pinned card answers and the source-
    'sieve' checks with an L2 text and a base p̄; anchors = the top TRIAL_ANCHORS verified companies outside E by base
    p̄ (>= TRIAL_ANCHOR_MIN_P, with a keyword hit and an L2 text)."""
    sid_key = {v.get("security_id"): k for k, v in base_reads.items() if v.get("security_id")}

    def key_of(ex):
        k = ex.get("company_key")
        return k if k in base_reads else sid_key.get(ex.get("security_id"))

    evals: dict[str, dict[str, Any]] = {}
    for ex in sieve.get("examples") or []:
        if not isinstance(ex, dict) or ex.get("want") not in PIN_WANTS:
            continue
        if ex.get("source", "card") == "card" and not ex.get("pin"):
            continue
        k = key_of(ex)
        if k and (ins.get(k) or {}).get("text") and base_reads[k].get("p_pos") is not None:
            evals[k] = {"want": ex["want"], "source": ex.get("source", "card")}
    anchors = [k for k, v in sorted(base_reads.items(), key=lambda kv: (-(kv[1].get("p_pos") or 0.0),
                                                                          -(kv[1].get("market_cap_usd") or 0), kv[0]))
               if k not in evals and (v.get("p_pos") or 0.0) >= TRIAL_ANCHOR_MIN_P and v.get("keyword_hit")
               and v.get("l2_label") in screen.L2_VERIFIED and (ins.get(k) or {}).get("text")][:TRIAL_ANCHORS]
    return evals, anchors


def _trial_items(pool: list[str], base_reads: dict[str, dict[str, Any]], ins: dict[str, dict[str, Any]]) -> list:
    _, Item = screen._jev_types()
    return [Item(item_id=k, issuer=base_reads[k].get("name") or base_reads[k].get("security_id") or k,
                 text=ins[k]["text"], meta={"security_id": base_reads[k].get("security_id")}) for k in pool]


def _kept_rules(sieve: dict[str, Any], candidates: list[dict[str, Any]]) -> list[Any]:
    """The sieve's adopted rules a trial keeps: minus the candidates' ids and, when a candidate is scope_narrow /
    scope_broad, minus the other one."""
    new_ids = {c["id"] for c in candidates}
    return [r for r in sieve.get("rules") or []
            if not (_rule_id(r) in EXCLUSIVE_RULES and new_ids & EXCLUSIVE_RULES) and _rule_id(r) not in new_ids]


def _trial_plan_size(n_candidates: int, max_trials: int) -> int:
    """The most trials trial_rules may run: all together (when more than one), each alone, and the union of the
    singles that passed (when it differs from all together, i.e. more than two candidates)."""
    n = n_candidates + (1 if n_candidates > 1 else 0) + (1 if n_candidates > 2 else 0)
    return min(max_trials, n)


def estimate_trials(candidates: list[dict[str, Any]], sieve: dict[str, Any], base_reads: dict[str, dict[str, Any]],
                    client, *, inputs: Any, idea: str, idea_en: str | None = None, max_trials: int = TRIAL_MAX
                    ) -> dict[str, Any]:
    """What trial_rules may spend (no cache): {'items' (per read), 'trials', 'est_cost_usd' (the planned trials,
    both reads), 'extra_max_usd' (the most the v1.1 extra reads can add: the control read of the pool once, plus
    TRIAL_REREADS of every company that can be a casualty - a yes / should_pass company with base p̄ >= 0.5, or an
    anchor - in every planned trial)}. $0 when there is nothing to try. The extras are made only while trial_rules'
    own budget allows them; a casualty that cannot be confirmed leaves its rule untried."""
    ins = _inputs_map(inputs)
    evals, anchors = _trial_pool(sieve, base_reads, ins)
    if not candidates or not evals:
        return {"items": 0, "trials": 0, "est_cost_usd": 0.0, "extra_max_usd": 0.0}
    items = _trial_items(list(evals) + anchors, base_reads, ins)
    n = _trial_plan_size(len(candidates), max_trials)
    kept = _kept_rules(sieve, candidates)
    q = screen.build_l2_question(idea, idea_en, rules=kept + list(candidates), facets=_facets(sieve))
    est = client.estimate(items, q) or {}
    q0 = screen.build_l2_question(idea, idea_en, rules=kept, facets=_facets(sieve))
    ctrl = float((client.estimate(items, q0) or {}).get("est_cost_usd") or 0.0) * len(CONTROL_READS)
    can_drop = [k for k, e in evals.items() if _is_yes(e["want"]) and (base_reads[k].get("p_pos") or 0.0) >= PI_CUT]
    elig = _trial_items(can_drop + anchors, base_reads, ins)
    rer = (float((client.estimate(elig, q) or {}).get("est_cost_usd") or 0.0) * len(TRIAL_REREADS) * n
           if elig else 0.0)
    return {"items": len(items), "trials": n,
            "est_cost_usd": round(float(est.get("est_cost_usd") or 0.0) * len(TRIAL_READS) * n, 8),
            "extra_max_usd": round(ctrl + rer, 8)}


def _read_order(items: list, r: int) -> list:
    """The trial items of read r in the band-read order (screen._l2_band_reads): sha256(f"{r}:{item_id}"), so the
    two reads of a trial are different packets, not a paid duplicate of one payload."""
    return sorted(items, key=lambda it: hashlib.sha256(f"{r}:{it.item_id}".encode("utf-8")).hexdigest())


def control_explains(trial_p: float, control_p: float) -> bool:
    """A casualty's drop is read noise, not the rule's, when the company is below PI_CUT without the new rules too
    AND the rule did not push it clearly lower than that: trial p̄ >= control p̄ - max(TRIAL_HIGH_SD,
    2 x noise_sd(control p̄)). A rule that takes a yes company from 0.55 to 0.02 vetoes whatever the control says."""
    return control_p < PI_CUT and trial_p >= control_p - max(TRIAL_HIGH_SD, 2 * noise_sd(control_p))


def _trial_stop(err: str | None) -> str:
    """The trial status of a read that could not confirm a casualty: jev_unavailable / jev_busy as they came, a
    budget stop as 'budget', anything else 'incomplete'."""
    if err in (screen.STATUS_UNAVAILABLE, screen.STATUS_BUSY):
        return err
    return "budget" if err in ("budget", screen.STATUS_BUDGET) else "incomplete"


def trial_rules(candidates: list[dict[str, Any]], sieve: dict[str, Any], base_reads: dict[str, dict[str, Any]],
                client, *, inputs: Any, idea: str, idea_en: str | None = None, budget_usd: float = 0.05,
                max_trials: int = TRIAL_MAX, now: str | None = None) -> dict[str, Any]:
    """The adoption test of candidate rules (a few cents of L2 reads).

    E = the sieve's card answers (yes / no, not unsure) and its source-'sieve' checks; anchors = the top
    TRIAL_ANCHORS companies by base p̄ (>= 0.85, verified, with a keyword hit) outside E. Every E and anchor company
    with an L2 text is read with read indices 0 and 1 under q' = the L2 question with the sieve's rules plus the
    trial's (each read in its own packet order, _read_order). agree(q) = items of E where [p̄ >= 0.5] matches
    want != no (base p̄ for q, mean of the trial reads for q'). Adopted only when agree(q') >= agree(q) + 1, no yes /
    should_pass company with base p̄ >= 0.5 drops below 0.5, and at most TRIAL_ANCHOR_MAX_DROPS anchors drop below
    0.5. A trial in which some pool company got no ok read (budget, provider errors, uncertain sends) is incomplete
    and adopts nothing (status 'budget' or 'incomplete'; its rules stay untried).
    Order: all candidates together first (when more than one; adopted as a set when it passes), then each alone.
    Only a rule set that passed together is ever adopted: when more than one single passes after the combination
    failed, their union is tried once more (unless it is the combination already rejected); when the union fails
    or cannot be tried, only the best single (agreement, then fewer anchor drops, then rule id) is adopted and the
    other passing singles are recorded as not adopted with the reason. At most max_trials, stopping before a trial
    whose estimate would pass budget_usd. Agreement is in-sample (IN_SAMPLE_NOTE_ZH).
    Read noise never vetoes (v1.1): a would-be casualty (a yes / should_pass company or an anchor dropping below 0.5)
    whose base or trial p̄ lies in TRIAL_EDGE, or whose reads spread by >= TRIAL_HIGH_SD, is re-read twice more
    (TRIAL_REREADS, it alone) and counts only when the mean stays below 0.5. A casualty that remains is read noise
    only when the question without the candidates reads it low as well and the rule did not push it clearly lower
    (control_explains); the control is a fresh read of the whole pool at CONTROL_READS (read indices no base run
    uses, so not the base run's cached answers), made once and shared by the trials. Noise companies are neutral in
    the agreement and listed in 'noise'. A casualty that could not be confirmed (no budget or no answer for its
    re-reads or the control) makes the trial incomplete: nothing adopted, its rules stay untried.
    Returns {'trials', 'adopted' (sieve rule entries with 'trial'), 'rejected' ([{id, why_zh, at}]), 'untried',
    'cost_usd', 'status' (ok | budget | incomplete | jev_unavailable | jev_busy | no_items), 'note_zh'}."""
    now = now or now_iso()
    ins = _inputs_map(inputs)
    evals, anchors = _trial_pool(sieve, base_reads, ins)
    pool = list(evals) + anchors
    out: dict[str, Any] = {"trials": [], "adopted": [], "rejected": [], "untried": [], "cost_usd": 0.0,
                           "status": "ok", "note_zh": IN_SAMPLE_NOTE_ZH, "evaluation": len(evals),
                           "anchors": len(anchors)}
    if not candidates:
        return out
    if not evals:
        out["status"] = "no_items"
        out["untried"] = [c["id"] for c in candidates]
        return out
    items = _trial_items(pool, base_reads, ins)
    kept = _kept_rules(sieve, candidates)

    def agree(p: dict[str, float | None]) -> int:
        return sum(1 for k, e in evals.items() if p.get(k) is not None
                   and (p[k] >= PI_CUT) == (e["want"] != "no"))

    base_p = {k: base_reads[k].get("p_pos") for k in pool}
    before = agree(base_p)
    spent0 = float(getattr(client, "spent_usd", 0.0) or 0.0)

    control: dict[str, Any] = {}             # the reads of the pool without any new rule (lazy, shared by trials)

    def spent_now() -> float:
        return float(getattr(client, "spent_usd", 0.0) or 0.0) - spent0

    def affordable(its: list, q, n_reads: int) -> bool:
        est = None
        with contextlib.suppress(Exception):
            est = client.estimate(its, q)
        return spent_now() + float((est or {}).get("est_cost_usd") or 0.0) * n_reads <= budget_usd + 1e-12

    def read_many(its: list, q, indices) -> tuple[dict[str, list[float]], str | None]:
        got_p: dict[str, list[float]] = {it.item_id: [] for it in its}
        for r in indices:
            order = _read_order(its, r)
            got, err_status, _err = screen._call_classify(client, order, dataclasses.replace(q, read=r))
            for it, x in zip(order, got):
                if x.get("status") == "ok":
                    got_p[it.item_id].append(screen.p_pos_of(x.get("probs")) or 0.0)
            if err_status:
                return got_p, err_status
        return got_p, None

    def control_p() -> dict[str, float] | None:
        """Mean p of every pool company under the question WITHOUT the candidates (kept rules only), read in the
        trial's packets (same read indices and order): the baseline a casualty is attributed against. None when
        it cannot be afforded or read."""
        if "p" not in control:
            q0 = screen.build_l2_question(idea, idea_en, rules=kept, facets=_facets(sieve))
            if not affordable(items, q0, len(CONTROL_READS)):
                control["p"], control["error"] = None, "budget"
            else:
                got_p, err = read_many(items, q0, CONTROL_READS)
                control["p"] = None if err else {k: sum(v) / len(v) for k, v in got_p.items() if v}
                control["error"] = err
        return control["p"]

    def run(trial: list[dict[str, Any]]) -> dict[str, Any] | None:
        """One trial: its record (appended to out['trials']), or None when the budget stops it before sending."""
        q = screen.build_l2_question(idea, idea_en, rules=kept + trial, facets=_facets(sieve))
        est = None
        with contextlib.suppress(Exception):
            est = client.estimate(items, q)
        est_cost = float((est or {}).get("est_cost_usd") or 0.0) * len(TRIAL_READS)
        spent = spent_now()
        if spent + est_cost > budget_usd + 1e-12:
            out["status"] = "budget"
            return None
        reads: dict[str, list[float]] = {k: [] for k in pool}
        stop = None
        short_budget = False
        for r in TRIAL_READS:
            order = _read_order(items, r)
            got, err_status, _err = screen._call_classify(client, order, dataclasses.replace(q, read=r))
            for it, x in zip(order, got):
                if x.get("status") == "ok":
                    reads[it.item_id].append(screen.p_pos_of(x.get("probs")) or 0.0)
                elif x.get("status") == "skipped_budget":
                    short_budget = True
            if err_status:
                stop = err_status
                break
        unread = [k for k in pool if not reads[k]]
        new_p = {k: (sum(v) / len(v) if v else base_p[k]) for k, v in reads.items()}
        noise: list[dict[str, Any]] = []
        unconfirmed: list[str] = []

        def name(k):
            return base_reads[k].get("name") or k

        def casualties() -> list[str]:
            yes_flips = [k for k, e in evals.items() if _is_yes(e["want"]) and (base_p[k] or 0.0) >= PI_CUT
                         and (new_p[k] or 0.0) < PI_CUT]
            return yes_flips + [k for k in anchors if (new_p[k] or 0.0) < PI_CUT]

        if not stop and not unread:
            # 1) boundary casualties are re-read twice more under the trial's question before they count
            def on_edge(k):
                v = reads[k]
                sd = base_reads[k].get("p_pos_sd")
                return (TRIAL_EDGE[0] <= (base_p[k] or 0.0) <= TRIAL_EDGE[1]
                        or TRIAL_EDGE[0] <= new_p[k] <= TRIAL_EDGE[1]
                        or (sd is not None and sd >= TRIAL_HIGH_SD)
                        or (len(v) > 1 and max(v) - min(v) >= TRIAL_HIGH_SD))
            edge = [k for k in casualties() if on_edge(k)]
            if edge:
                its = [it for it in items if it.item_id in set(edge)]
                more, err = ({}, "budget")
                if affordable(its, q, len(TRIAL_REREADS)):
                    more, err = read_many(its, q, TRIAL_REREADS)
                short = [k for k in edge if len((more or {}).get(k) or []) < len(TRIAL_REREADS)]
                if short:                                    # unconfirmed: a gap, never noise and never adopted
                    stop = _trial_stop(err)
                    unconfirmed = short
                elif err in (screen.STATUS_UNAVAILABLE, screen.STATUS_BUSY):
                    stop = err
                for k in edge:
                    if k in short:
                        continue
                    reads[k] += more[k]
                    new_p[k] = sum(reads[k]) / len(reads[k])
                    if new_p[k] >= PI_CUT:
                        noise.append({"company_key": k, "name": name(k), "base": _r2(base_p[k]),
                                      "trial": _r2(new_p[k]), "why": "reread"})
            # 2) attribution: a casualty that drops about as far without the new rules (a fresh control read) is
            #    not the rule's; one the rule pushes clearly lower than the control is
            left = casualties()
            if left and not stop:
                ctrl = control_p()
                if ctrl is None:                             # the control could not be read: unconfirmed
                    stop = _trial_stop(control.get("error"))
                    unconfirmed = left
                for k in left if ctrl is not None else ():
                    if ctrl.get(k) is not None and control_explains(new_p[k], ctrl[k]):
                        noise.append({"company_key": k, "name": name(k), "base": _r2(base_p[k]),
                                      "trial": _r2(new_p[k]), "control": _r2(ctrl[k]), "why": "control"})
                        new_p[k] = base_p[k]                 # not the rule: neutral
        after = agree(new_p)
        flips = [name(k) for k, e in evals.items()
                 if _is_yes(e["want"]) and (base_p[k] or 0.0) >= PI_CUT and (new_p[k] or 0.0) < PI_CUT]
        drops = [name(k) for k in anchors if (new_p[k] or 0.0) < PI_CUT]
        cost = spent_now() - spent
        if flips:
            why = f"这条规则会误伤 {'、'.join(flips)}，未采用"
        elif len(drops) > TRIAL_ANCHOR_MAX_DROPS:
            why = f"会让 {len(drops)} 家高分公司掉出（{'、'.join(drops[:4])}），未采用"
        elif after < before + 1:
            why = f"和你的回答一致度没有提高（{before}→{after}），未采用"
        else:
            why = None
        if unconfirmed:
            why = (f"{'、'.join(name(k) for k in unconfirmed)} 的下降没能确认（"
                   + ("预算不够多读" if stop == "budget" else "Jev 没读完") + "），结果不完整，未采用")
        elif stop:
            why = "Jev 中途停了，结果不完整，未采用"
        elif unread:
            why = f"Jev 没读完（{len(unread)} 家没有结果），结果不完整，未采用"
            stop = "budget" if short_budget else "incomplete"
        rec = {"rules": [c["id"] for c in trial], "items": len(items) * len(TRIAL_READS), "agree_before": before,
               "agree_after": after, "flips": flips, "anchor_drops": drops, "cost_usd": round(cost, 6),
               "adopted": why is None, "why_zh": why, "complete": not stop, "noise": noise}
        out["trials"].append(rec)
        if stop:
            out["status"] = stop
        return rec

    def can_run() -> bool:
        return len(out["trials"]) < max_trials and out["status"] == "ok"

    def adopt(trial: list[dict[str, Any]], rec: dict[str, Any]) -> None:
        info = {"items": rec["items"], "agree_before": rec["agree_before"], "agree_after": rec["agree_after"],
                "cost_usd": rec["cost_usd"]}
        out["adopted"] += [{**c, "trial": info, "adopted_at": now} for c in trial]

    decided: dict[str, str | None] = {}            # rule id -> None (passed alone) / why it was rejected alone
    singles: dict[str, dict[str, Any]] = {}        # rule id -> the record of its passing single trial
    combined = None
    if len(candidates) > 1 and can_run():
        combined = run(list(candidates))
        if combined is not None and combined["adopted"]:
            adopt(list(candidates), combined)
    if not out["adopted"]:
        for c in candidates:
            if not can_run():
                break
            rec = run([c])
            if rec is None or out["status"] != "ok":
                break
            decided[c["id"]] = rec["why_zh"]
            if rec["adopted"]:
                singles[c["id"]] = rec
        passed = [c for c in candidates if c["id"] in singles]
        if len(passed) == 1:
            adopt(passed, singles[passed[0]["id"]])
        elif len(passed) > 1:
            union = None
            if combined is not None and len(passed) == len(candidates):
                union = combined                   # the union is the combination already rejected
            elif can_run():
                union = run(passed)
            if union is not None and union["adopted"]:
                adopt(passed, union)
            else:
                best = min(passed, key=lambda c: (-singles[c["id"]]["agree_after"],
                                                  len(singles[c["id"]]["anchor_drops"]), c["id"]))
                adopt([best], singles[best["id"]])
                if union is not None and union["complete"]:
                    why_union = f"一起试不行（{union['why_zh'].removesuffix('，未采用')}）"
                else:
                    why_union = "一起没能试：" + {"budget": "预算不够", "ok": "试验次数用完"}.get(out["status"],
                                                                                         "Jev 中途停了")
                for c in passed:
                    if c is not best:
                        decided[c["id"]] = f"单独试可以，但和 {best['id']} {why_union}；这次只采用 {best['id']}"
    adopted_ids = {c["id"] for c in out["adopted"]}
    out["rejected"] = [{"id": c["id"], "why_zh": decided[c["id"]], "at": now, **({"terms": c["terms"]}
                                                                               if c.get("terms") else {})}
                       for c in candidates if c["id"] not in adopted_ids and decided.get(c["id"])]
    out["untried"] = [c["id"] for c in candidates if c["id"] not in adopted_ids and not decided.get(c["id"])]
    out["cost_usd"] = round(float(getattr(client, "spent_usd", 0.0) or 0.0) - spent0, 6)
    return out


def adopt_rules(sieve: dict[str, Any], trial: dict[str, Any]) -> dict[str, Any]:
    """sieve' with the trial's adopted rules in sieve.rules (replacing an older entry of the same id and, for
    scope_narrow / scope_broad, the other one) and its rejections in sieve.rejected_rules. Not saved."""
    sv = json.loads(json.dumps(sieve))
    rules = list(sv.get("rules") or [])
    for e in trial.get("adopted") or []:
        drop = {e["id"]} | (EXCLUSIVE_RULES if e["id"] in EXCLUSIVE_RULES else set())
        rules = [r for r in rules if _rule_id(r) not in drop] + [e]
    sv["rules"] = rules
    sv["rejected_rules"] = list(sv.get("rejected_rules") or []) + list(trial.get("rejected") or [])
    return sv


def render_trial_zh(trial: dict[str, Any]) -> str:
    """The trial printout: agreement before -> after per trial (labelled in-sample), adoptions and rejections."""
    lines = []
    adopted = {c["id"] for c in trial.get("adopted") or []}
    why_rejected = {r["id"]: r.get("why_zh") for r in trial.get("rejected") or []}
    for t in trial.get("trials") or []:
        head = "+".join(t["rules"])
        if t["adopted"] and set(t["rules"]) <= adopted:
            verdict = "采用"
        elif t["adopted"]:                     # passed alone, but not adopted (see trial_rules)
            verdict = "；".join(dict.fromkeys(why_rejected.get(r) or "没有采用" for r in t["rules"]))
        else:
            verdict = t["why_zh"]
        lines.append(f"  试 {head}：一致 {t['agree_before']}→{t['agree_after']}（${t['cost_usd']:.4f}）· {verdict}")
        for n in t.get("noise") or []:
            how = {"reread": f"多读 2 次回到 {n.get('trial')}",
                   "control": f"不加这条规则也只有 {n.get('control')}，这条规则下 {n.get('trial')}，不是规则造成的"
                   }.get(n.get("why"), "")
            lines.append(f"    {n.get('name')} 的下降算读数噪声（原来 {n.get('base')}；{how}）")
    why_untried = {"budget": "预算不够", "incomplete": "Jev 中途停了", "jev_unavailable": "Jev 中途停了",
                   "jev_busy": "Jev 中途停了"}.get(trial.get("status"), "已达试验次数上限")
    for rid in trial.get("untried") or []:
        lines.append(f"  {rid}：没试（{why_untried}）")
    if lines:
        lines.append(f"  （{trial.get('note_zh') or IN_SAMPLE_NOTE_ZH}）")
    return "\n".join(["规则试验"] + lines) if lines else "规则试验：没有候选规则"


# ---------------------------------------------------------------------------------------------------------------
# Keywords: background document frequency, noisy seeds, mining local vocabulary, peer preview

def _fold(text: str) -> str:
    """NFKC, lower case, whitespace removed, Traditional Chinese folded to Simplified (zhvariants): a necessary
    condition for a _term_pattern match is that the folded term is a substring of the folded text (used to skip most
    regex searches)."""
    return zhvariants.to_simplified(re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "").lower()))


def _hits(text: str, folded: str, term: str, pat: re.Pattern) -> bool:
    ft = _fold(term)
    return bool(ft) and ft in folded and pat.search(text) is not None


def doc_freq(docs: Iterable[tuple[str | None, str]], terms: Iterable[str]) -> dict[str, Any]:
    """Document frequency of terms over (company_key, text) documents with screen._term_pattern matching:
    {'n', 'doc_companies': [company_key per document], 'hits': {term: [company_key of each hitting document]}}."""
    terms = list(dict.fromkeys(t for t in terms if t and t.strip()))
    pats = {t: screen._term_pattern(t) for t in terms}
    out: dict[str, Any] = {"n": 0, "doc_companies": [], "hits": {t: [] for t in terms}}
    for ck, text in docs:
        out["n"] += 1
        out["doc_companies"].append(ck)
        folded = _fold(text)
        for t in terms:
            if _hits(text, folded, t, pats[t]):
                out["hits"][t].append(ck)
    return out


def _source_doc_map(con, source_id: str) -> dict[str, tuple[str, str | None, str]]:
    """{doc_id: (text_path, company_key, signature)} of one source's documents with text; the signature (text sha256,
    else fetched_at) changes when the text is re-extracted."""
    rows = con.execute("""
        SELECT d.doc_id, d.text_path, COALESCE(d.company_key, s.company_key, s2.company_key) AS ck,
               COALESCE(d.text_sha256, CAST(d.fetched_at AS VARCHAR), '')
        FROM documents d
        LEFT JOIN securities s ON s.security_id = d.security_id
        LEFT JOIN identifiers i ON i.id_value = d.cik AND i.id_type IN ('sec_cik', 'cninfo_orgid', 'edinet_code',
                                                                        'dart_corp_code', 'mops_co_id',
                                                                        'bse_scrip_code')
        LEFT JOIN securities s2 ON s2.security_id = i.security_id
        WHERE d.source_id = ? AND d.text_path IS NOT NULL
        ORDER BY d.doc_id""", [source_id]).fetchall()
    out: dict[str, tuple[str, str | None, str]] = {}
    for doc_id, path, ck, sig in rows:          # the identifier join can repeat a document: one entry per doc_id
        out.setdefault(doc_id, (path, ck, str(sig)))
    return out


def background_df(cfg, con, source_id: str, terms: Iterable[str]) -> dict[str, Any]:
    """doc_freq of `terms` over every document of one source (e.g. all EDINET filings), cached in
    <home>/calib/df-<source_id>.json per document (doc_id -> signature, hits as doc_ids). Incremental: a new document
    (a sync or an on-demand fetch) is counted alone for the cached terms; only a removed document, a re-extracted
    one (another signature) or another Chinese variant table (zhvariants.TABLE_VERSION, which changes what matches)
    rebuilds the cache. Terms not yet counted read every text once. Unreadable files count as empty."""
    terms = list(dict.fromkeys(t for t in terms if t and t.strip()))
    cur = _source_doc_map(con, source_id)
    path = Path(cfg.home) / "calib" / f"df-{source_id}.json"
    cache = None
    with contextlib.suppress(OSError, ValueError):
        cache = json.loads(path.read_text(encoding="utf-8"))
    ok = (isinstance(cache, dict) and cache.get("version") == 2 and cache.get("source_id") == source_id
          and cache.get("matcher") == zhvariants.TABLE_VERSION    # counts made with another variant table: rebuild
          and isinstance(cache.get("docs"), dict) and isinstance(cache.get("hits"), dict)
          and all(cur.get(d, (None, None, None))[2] == sig for d, sig in cache["docs"].items()))
    if not ok:
        cache = {"version": 2, "source_id": source_id, "matcher": zhvariants.TABLE_VERSION, "docs": {}, "hits": {}}
    changed = False

    def texts(ids: Iterable[str]):
        for d in ids:
            p, _ck, _ = cur[d]
            try:
                yield d, Path(p).read_text(encoding="utf-8", errors="replace")
            except OSError:
                yield d, ""
    new_ids = [d for d in cur if d not in cache["docs"]]
    if new_ids and cache["hits"]:
        got = doc_freq(texts(new_ids), list(cache["hits"]))
        for t, ids in got["hits"].items():
            cache["hits"][t] = cache["hits"][t] + ids
    if new_ids:
        cache["docs"].update({d: cur[d][2] for d in new_ids})
        changed = True
    missing = [t for t in terms if t not in cache["hits"]]
    if missing:
        got = doc_freq(texts(list(cur)), missing)
        cache["hits"].update(got["hits"])
        changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    ck_of = {d: v[1] for d, v in cur.items()}
    return {"source_id": source_id, "n": len(cur), "doc_companies": [ck_of[d] for d in cur],
            "hits": {t: [ck_of.get(d) for d in cache["hits"][t]] for t in terms}}


def df_count(df: dict[str, Any], term: str) -> int | None:
    h = (df.get("hits") or {}).get(term)
    return None if h is None else len(h)


def lift_of(df: dict[str, Any], term: str, l1_rel: Iterable[str]) -> float | None:
    """(share of the term's hits in companies L1 judged core / adjacent) / (that share over the whole corpus)."""
    rel = set(l1_rel)
    hits = (df.get("hits") or {}).get(term)
    docs = df.get("doc_companies") or []
    base = sum(1 for ck in docs if ck in rel) / len(docs) if docs else 0.0
    if not hits or base <= 0:
        return None
    return (sum(1 for ck in hits if ck in rel) / len(hits)) / base


def noisy_terms(cfg, con, terms: Iterable[str], lang: str, *, df: dict[str, Any] | None = None) -> dict[str, Any]:
    """The seeds of a language that appear in >= NOISY_DF_SHARE of that language's filings (background_df over the
    source of that language): they become weak (build_excerpts down-weights them; they are not deleted). Terms that
    appear in no filing are listed as absent (年报里没出现). {'source_id', 'n', 'weak', 'absent', 'stats': {term:
    {df, share}}}. `df` (a doc_freq result) replaces the store (tests)."""
    terms = list(dict.fromkeys(t for t in terms if t and t.strip()))
    src = LANG_SOURCE.get(lang)
    if df is None:
        if src is None or not terms:
            return {"source_id": src, "n": 0, "weak": [], "absent": [], "stats": {}}
        df = background_df(cfg, con, src, terms)
    n = df.get("n") or 0
    stats, weak, absent = {}, [], []
    for t in terms:
        c = df_count(df, t) or 0
        share = c / n if n else 0.0
        stats[t] = {"df": c, "share": round(share, 4)}
        if n and share >= NOISY_DF_SHARE:
            weak.append(t)
        elif n and c == 0:
            absent.append(t)
    return {"source_id": src, "n": n, "weak": weak, "absent": absent, "stats": stats}


# a compound does not start with a Chinese function word (的 和 与 …): '的统一身份认证' is not a term
_ZH_NO_START = frozenset("的和与及或为在对是了将并等从向把被由其之于以而也都")


def _candidates_in(window: str, lang: str) -> set[str]:
    """Candidate terms of a window: katakana runs (ja), all-caps acronyms, and every compound of 3-8 Han / kana /
    Hangul characters inside a run that ends with an auth / control suffix. Chinese (and Japanese Han after
    katakana) has no word breaks, so a compound may start anywhere in the run: all start positions are candidates,
    and mine_terms' support / df / lift / name gates and its longest-with-the-same-support rule pick the term."""
    out: set[str] = set()
    out.update(_KATAKANA_RUN.findall(window) if lang == "ja" else ())
    out.update(_ACRONYM_RUN.findall(window))
    suffix = _MINE_SUFFIX.get(lang)
    if suffix:
        body = {"ja": r"[ァ-ヺー一-鿿]", "zh": r"[一-鿿]", "ko": r"[가-힣]"}[lang]
        for m in re.finditer(rf"{body}{{3,}}", window):
            run = m.group()
            for s in suffix:
                for e in re.finditer(re.escape(s), run):
                    for start in range(max(0, e.end() - 8), e.end() - 2):
                        c = run[start:e.end()]
                        if lang == "zh" and c[0] in _ZH_NO_START:
                            continue
                        if start > 0 and _is_kana(run[start]) and _is_kana(run[start - 1]):
                            continue                    # inside a katakana word: 'クセス制御' of 'アクセス制御'
                        out.add(c)
    return out


def _is_kana(ch: str) -> bool:
    return bool(ch) and ("ァ" <= ch <= "ヺ" or ch == "ー")


def _is_han(ch: str) -> bool:
    return bool(ch) and ("一" <= ch <= "鿿" or "㐀" <= ch <= "䶿")


def _is_hangul(ch: str) -> bool:
    return bool(ch) and "가" <= ch <= "힣"


def fragment_of_word(term: str, lang: str, docs: Iterable[str]) -> bool:
    """True when a mined CJK candidate is a piece of a longer word rather than a term of its own:
    - it starts with a word-final Han character (化 性 式 型 等 …: '化运维管理') or, in Chinese, runs across 的 / 和
      ('经销商的管理', '置和管理');
    - it starts inside a katakana word ('クセス制御') or a Hangul word in at least half of its occurrences;
    - a Han start: one and the same Han character glued to its left (not a function word or a word-final character)
      in at least 2/3 of its occurrences in the yes docs ('限管理' of 权限管理, '群管理' of 集群管理)."""
    if not term:
        return False
    first = term[0]
    if lang in ("zh", "ja") and first in _BOUND_START:
        return True
    if lang == "zh" and any(ch in _ZH_INTERIOR_STOP for ch in term[1:-1]):
        return True
    pat = screen._term_pattern(term)
    lefts = [d[m.start() - 1] if m.start() > 0 else "" for d in docs for m in pat.finditer(d)]
    if not lefts:
        return False
    if _is_kana(first) or _is_hangul(first):
        same = _is_kana if _is_kana(first) else _is_hangul
        return 2 * sum(1 for x in lefts if same(x)) >= len(lefts)
    if _is_han(first):
        glued = [zhvariants.to_simplified(x) for x in lefts
                 if _is_han(x) and x not in _ZH_NO_START and x not in _BOUND_START]
        if glued:
            top = max(glued.count(x) for x in set(glued))
            return 3 * top >= 2 * len(lefts)
    return False


def _acronym_ok(term: str, seeded: set[str]) -> bool:
    """An all-caps Latin candidate is kept when the run seeded it (any language) or it has >= MINE_ACRONYM_MIN
    letters and is not a GENERIC_ACRONYMS word."""
    t = unicodedata.normalize("NFKC", term).strip().upper()
    if t in seeded:
        return True
    return len(t) >= MINE_ACRONYM_MIN and t not in GENERIC_ACRONYMS


def mine_terms(lang: str, yes_docs: list[str], current_terms: list[str], no_docs: list[str], names: list[str],
               df: dict[str, Any] | Callable[[list[str]], dict[str, Any]], l1_rel: Iterable[str], *,
               seeded: Iterable[str] = (), yes_keys: list[str] | None = None) -> dict[str, Any]:
    """Local vocabulary for a CJK language from the filings of yes companies (answered yes, or verified with p̄ >=
    0.8). Candidates within +-MINE_WINDOW characters of each current-term hit (NFKC): katakana runs of >= 4 (ja),
    Han / kana (ja), Han (zh) or Hangul (ko) compounds of 3-8 characters ending in an auth / control suffix, and
    all-caps Latin acronyms of 2-6 letters (other Latin words are dropped). Kept only when (a) in the docs of >=
    min(3, number of yes companies) distinct yes companies (yes_keys: the company of each doc; default one company per
    doc), (b) an acronym: seeded by the run (`seeded`: the terms of every language) or >= 4 letters and not a
    GENERIC_ACRONYMS word ('acronym'), (c) a whole word, not a piece of one (fragment_of_word: 'fragment'), (d) df <=
    max(3, 1% of N), (e) lift >= 3 against the companies L1 judged core / adjacent (l1_rel), (f) not a substring of a
    yes company's name or a 「…」 product name, (g) not in the keyword excerpt of a company answered no / should_fail
    (no_docs); a shorter candidate contained in a kept longer one with the same support is dropped. `df` is a
    doc_freq result covering the candidates or a function terms -> doc_freq. Returns {'add': [...], 'log': [{term,
    action 'add', source 'mined', df, lift, yes_docs}], 'rejected': [{term, why}]}."""
    if lang not in screen.CJK_LANGS or not yes_docs:
        return {"add": [], "log": [], "rejected": []}
    cur_f = {_fold(t) for t in current_terms}
    pats = [screen._term_pattern(t) for t in current_terms if t and t.strip()]
    cands: set[str] = set()
    products: set[str] = set()
    norm_docs = [unicodedata.normalize("NFKC", d or "") for d in yes_docs]
    for d in norm_docs:
        products.update(m.strip() for m in _PRODUCT_NAME.findall(d))
        for p in pats:
            for m in p.finditer(d):
                cands |= _candidates_in(d[max(0, m.start() - MINE_WINDOW):m.end() + MINE_WINDOW], lang)
    cands = {c for c in cands if _fold(c) not in cur_f}
    rejected: list[dict[str, str]] = []
    support: dict[str, int] = {}
    keys = list(yes_keys) if yes_keys is not None and len(yes_keys) == len(norm_docs) else \
        [str(i) for i in range(len(norm_docs))]
    need = min(MINE_MIN_YES, len(set(keys)))
    for c in sorted(cands):
        pat = screen._term_pattern(c)
        support[c] = len({k for k, d in zip(keys, norm_docs) if pat.search(d)})
    seed_up = {unicodedata.normalize("NFKC", t).strip().upper() for t in list(seeded) + list(current_terms) if t}
    stage = []
    for c in sorted(cands):
        if support[c] < need:
            rejected.append({"term": c, "why": "yes_docs"})
        elif _ACRONYM_RUN.fullmatch(c) and not _acronym_ok(c, seed_up):
            rejected.append({"term": c, "why": "acronym"})
        elif not _ACRONYM_RUN.fullmatch(c) and fragment_of_word(c, lang, norm_docs):
            rejected.append({"term": c, "why": "fragment"})
        else:
            stage.append(c)
    return _gate_candidates(stage, support, df, l1_rel, list(names) + sorted(products), no_docs, rejected, "mined")


def _gate_candidates(stage: list[str], support: dict[str, int], df: Any, l1_rel: Iterable[str],
                     names: Iterable[str], no_docs: Iterable[str], rejected: list[dict[str, str]], source: str,
                     max_add: int | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The df / lift / name / no-answer gates and the longest-with-the-same-support rule of mine_terms and
    mine_replacements (their result shape; log entries carry `source` and `extra`)."""
    l1_rel = set(l1_rel)
    dfr = df(stage) if callable(df) else df
    n = dfr.get("n") or 0
    max_df = max(MINE_MAX_DF_MIN, MINE_MAX_DF_SHARE * n)
    name_f = [_fold(x) for x in names if x]
    no_f = [(d, _fold(d)) for d in no_docs if d]
    kept: list[tuple[str, int, float | None]] = []
    for c in stage:
        cnt = df_count(dfr, c) or 0
        lift = lift_of(dfr, c, l1_rel)
        fc = _fold(c)
        if cnt > max_df:
            rejected.append({"term": c, "why": f"df {cnt} > {max_df:g}"})
        elif lift is None or lift < MINE_MIN_LIFT:
            rejected.append({"term": c, "why": f"lift {lift if lift is None else round(lift, 2)} < {MINE_MIN_LIFT}"})
        elif any(fc in nm for nm in name_f):
            rejected.append({"term": c, "why": "name"})
        elif any(_hits(d, fd, c, screen._term_pattern(c)) for d, fd in no_f):
            rejected.append({"term": c, "why": "no_answer"})
        else:
            kept.append((c, cnt, lift))
    final = [(c, cnt, lift) for c, cnt, lift in kept
             if not any(c != o and c in o and support[o] >= support[c] for o, _, _ in kept)]
    final.sort(key=lambda x: (-support[x[0]], x[0]))
    if max_add is not None:
        rejected += [{"term": c, "why": f"over {max_add}"} for c, _, _ in final[max_add:]]
        final = final[:max_add]
    return {"add": [c for c, _, _ in final],
            "log": [{"term": c, "action": "add", "source": source, "df": cnt, "lift": round(lift, 2),
                     "yes_docs": support[c], "gate": MINE_GATE_VERSION, **(extra or {})} for c, cnt, lift in final],
            "rejected": rejected}


MINE_REPLACE_MAX = 5          # replacement terms per language and run
MINE_REPLACE_MIN_COMPANIES = 3   # replacements need this many yes companies (no 'all of them when fewer': one or
                                 # two texts give their own addresses and slogans, not the language's vocabulary)
MINE_REPLACE_CANDS = 80       # candidates (most supported first) that reach the background df
_KO_PARTICLES = ("에서는", "으로는", "에서", "으로", "에게", "까지", "부터", "보다", "처럼", "은", "는", "이", "가",
                 "을", "를", "의", "에", "로", "와", "과", "도", "만")


def _replacement_candidates(text: str, lang: str) -> set[str]:
    """Words of a CJK text for mine_replacements: katakana words of >= 3 (ja), Han n-grams of 2-6 inside Han runs
    (zh, ja) and Hangul words without their trailing particle (ko)."""
    t = unicodedata.normalize("NFKC", text or "")
    out: set[str] = set()
    if lang == "ja":
        out.update(re.findall(r"[ァ-ヺー]{3,}", t))
    if lang in ("zh", "ja"):
        for run in re.findall(r"[一-鿿]{2,}", t):
            for n in range(2, 7):
                out.update(run[i:i + n] for i in range(0, len(run) - n + 1))
    if lang == "ko":
        for w in re.findall(r"[가-힣]{2,}", t):
            for p in _KO_PARTICLES:
                if w.endswith(p) and len(w) - len(p) >= 2:
                    w = w[:-len(p)]
                    break
            if 2 <= len(w) <= 10:
                out.add(w)
    return out


def mine_replacements(lang: str, texts: list[str], keys: list[str], absent: list[str], no_docs: list[str],
                      names: list[str], df: dict[str, Any] | Callable[[list[str]], dict[str, Any]],
                      l1_rel: Iterable[str], *, current_terms: Iterable[str] = ()) -> dict[str, Any]:
    """Replacement terms for seeds that appear in no filing of their source (noisy_terms 'absent': the local model
    wrote them, e.g. 펩타드 for 펩타이드): words of the real texts of the yes companies (`texts`, one per company
    in `keys`: answered yes / should_pass or verified with p̄ >= MINE_YES_P; the L2 texts that verified them; not a
    company-name katakana word such as インコーポレイテッド), kept only when found at >= 3 companies (nothing at all
    with fewer than MINE_REPLACE_MIN_COMPANIES yes companies), a whole word (fragment_of_word), df <= max(3, 1% of N), lift >= 3, not a company name, not in a rejected company's
    excerpt; the MINE_REPLACE_CANDS most supported reach the df, the MINE_REPLACE_MAX best are added. Log entries
    have source 'replacement' and 'replaces' (the absent seeds). Nothing when no seed is absent."""
    if lang not in screen.CJK_LANGS or not absent or not texts:
        return {"add": [], "log": [], "rejected": []}
    norm = [unicodedata.normalize("NFKC", t or "") for t in texts]
    keys = list(keys) if len(keys) == len(norm) else [str(i) for i in range(len(norm))]
    if len(set(keys)) < MINE_REPLACE_MIN_COMPANIES:
        return {"add": [], "log": [], "rejected": [], "why": f"fewer than {MINE_REPLACE_MIN_COMPANIES} yes companies"}
    need = MINE_MIN_YES
    cur_f = {_fold(t) for t in list(current_terms) + list(absent)}
    by_key: dict[str, set[str]] = {}
    for k, t in zip(keys, norm):
        by_key.setdefault(k, set()).update(_replacement_candidates(t, lang))
    support: dict[str, int] = {}
    for words in by_key.values():
        for w in words:
            support[w] = support.get(w, 0) + 1
    rejected: list[dict[str, str]] = []
    stage = []
    for c in sorted(w for w, n in support.items() if n >= need and _fold(w) not in cur_f):
        if c in _CORPORATE_KATAKANA:
            rejected.append({"term": c, "why": "name"})
        elif fragment_of_word(c, lang, norm):
            rejected.append({"term": c, "why": "fragment"})
        else:
            stage.append(c)
    stage = sorted(stage, key=lambda c: (-support[c], -len(c), c))[:MINE_REPLACE_CANDS]
    return _gate_candidates(sorted(stage), support, df, l1_rel, names, no_docs, rejected, "replacement",
                            max_add=MINE_REPLACE_MAX, extra={"replaces": list(absent)})


def peer_preview(entries: Iterable[tuple[dict[str, Any], dict[str, Any] | None]], lang: str,
                 old: dict[str, list[str]], new: dict[str, list[str]], *, protected: Iterable[str] = (),
                 today: dt.date | None = None) -> dict[str, Any]:
    """What a keyword change does to every L2 input in `lang`, free: each (c, doc) (screen's company dict with
    'desc', and its document) is rebuilt with screen._l2_input under the old and the new {'terms', 'weak'}; every
    company whose keyword excerpt or keyword_hit changes is listed. A change that makes the excerpt of a `protected`
    company (answered yes / should_pass; company_key or security_id) worse is rejected: it loses its keyword hit
    ('lost') or no longer matches an old term it matched that stays strong ('worse'; moving off a term that is weak
    before or after the change is what down-weighting is for).
    {'changes': [{company_key, security_id, name, hit_before, hit_after, kw_before, kw_after, terms_lost}], 'lost',
    'worse', 'rejected': bool, 'why_zh'}."""
    prot = set(protected)
    weak_any = {_fold(t) for t in list(old.get("weak") or []) + list(new.get("weak") or [])}
    strong = [t for t in old.get("terms") or [] if _fold(t) not in weak_any]     # a term this change makes weak
    changes = []                                                                  # may leave the excerpt: that is the point
    for c, doc in entries:
        a = screen._l2_input(c, doc, {lang: list(old.get("terms") or [])}, today, {lang: list(old.get("weak") or [])})
        if a.get("lang") != lang:
            continue
        b = screen._l2_input(c, doc, {lang: list(new.get("terms") or [])}, today, {lang: list(new.get("weak") or [])})

        def kw(x):
            return next((e["text"] for e in x.get("excerpts") or [] if e["kind"] == "keywords"), None)
        if (kw(a), bool(a.get("keyword_hit"))) == (kw(b), bool(b.get("keyword_hit"))):
            continue
        before = screen.matched_terms(kw(a), strong)
        after = set(screen.matched_terms(kw(b), strong))
        changes.append({"company_key": c.get("company_key"), "security_id": c.get("security_id"),
                        "name": c.get("name"), "hit_before": bool(a.get("keyword_hit")),
                        "hit_after": bool(b.get("keyword_hit")), "kw_before": kw(a), "kw_after": kw(b),
                        "terms_lost": [t for t in before if t not in after]})
    mine = [x for x in changes if {x["company_key"], x["security_id"]} & prot]
    lost = [x for x in mine if x["hit_before"] and not x["hit_after"]]
    worse = [x for x in mine if x not in lost and x["terms_lost"]]
    why = None
    if lost:
        why = f"会让 {'、'.join(x['name'] or x['security_id'] for x in lost)} 失去关键词段落（你答了要），未采用"
    elif worse:
        why = ("会让 " + "、".join(f"{x['name'] or x['security_id']} 的关键词段落变差（不再含 {'、'.join(x['terms_lost'])}）"
                                  for x in worse) + "（你答了要），未采用")
    return {"changes": changes, "lost": lost, "worse": worse, "rejected": bool(lost or worse), "why_zh": why}


def drop_worsening_terms(entries: Iterable[tuple[dict[str, Any], dict[str, Any] | None]], lang: str,
                         old: dict[str, list[str]], add: Iterable[str], *, protected: Iterable[str],
                         today: dt.date | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """(kept, dropped): each mined term tried alone on the protected companies (answered yes / should_pass): a term
    that would cost one of them its keyword paragraph, or an old term its excerpt matched, is dropped ({term, why
    'worsens <name>', names}). A new term never makes a yes company's excerpt worse."""
    prot = set(protected)
    mine = [(c, d) for c, d in entries if {c.get("company_key"), c.get("security_id")} & prot]
    kept, dropped = [], []
    for t in add:
        if not mine:
            kept.append(t)
            continue
        prev = peer_preview(mine, lang, old, {"terms": list(old.get("terms") or []) + [t],
                                              "weak": list(old.get("weak") or [])}, protected=prot, today=today)
        if prev["rejected"]:
            names = [x["name"] or x["security_id"] for x in prev["lost"] + prev["worse"]]
            dropped.append({"term": t, "why": "worsens " + "、".join(names), "names": names})
        else:
            kept.append(t)
    return kept, dropped


def render_peer_preview_zh(preview: dict[str, Any], lang: str) -> str:
    lines = [f"{LANG_ZH.get(lang, lang)}关键词影响预览：{len(preview.get('changes') or [])} 家公司的摘录会变"]
    for x in preview.get("changes") or []:
        hit = ("失去关键词段" if x["hit_before"] and not x["hit_after"] else
               "新增关键词段" if x["hit_after"] and not x["hit_before"] else "关键词段换了")
        lines.append(f"  {_display(x)}：{hit}")
    if preview.get("why_zh"):
        lines.append(f"  {preview['why_zh']}")
    return "\n".join(lines)


def merge_keywords(sieve: dict[str, Any], lang: str, *, add: Iterable[str] = (), weak: Iterable[str] = (),
                   log: Iterable[dict[str, Any]] = (), remove: Iterable[str] = (), now: str | None = None
                   ) -> dict[str, Any]:
    """sieve' with terms added to / made weak in / removed from sieve.keywords[lang].add (deduplicated; a weak
    term is not also added) and the log entries appended (each gets 'at'). Not saved."""
    now = now or now_iso()
    sv = json.loads(json.dumps(sieve))
    kw = sv.get("keywords") if isinstance(sv.get("keywords"), dict) else {}
    cur = kw.get(lang) if isinstance(kw.get(lang), dict) else {}
    w = list(dict.fromkeys(list(cur.get("weak") or []) + list(weak)))
    wf = {_fold(t) for t in w} | {_fold(t) for t in remove}
    a = [t for t in dict.fromkeys(list(cur.get("add") or []) + list(add)) if _fold(t) not in wf]
    lg = list(cur.get("log") or []) + [{**e, "at": e.get("at") or now} for e in log]
    kw[lang] = {"add": a, "weak": w, "log": lg}
    sv["keywords"] = kw
    return sv


_REMOVE_WHY_ZH = {"acronym": "泛用缩写", "fragment": "词的碎片", "yes_docs": "少于 3 家公司"}


def keyword_log_zh(lang: str, added: Iterable[str], noisy: dict[str, Any] | None = None, *,
                   removed: Iterable[dict[str, str]] = ()) -> str:
    """'日文关键词 +シングルサインオン +SAML；「認証」降权（6.9% 的日本年报都有）', plus 年报里没出现 for absent seeds and
    '-クセス制御（词的碎片，旧版挖出的）' for learned adds removed by the current gates."""
    parts = []
    add = [f"+{t}" for t in added]
    if add:
        parts.append(" ".join(add))
    rm = [f"-{x['term']}（{_REMOVE_WHY_ZH.get(x.get('why'), x.get('why'))}，旧版挖出的）" for x in removed]
    if rm:
        parts.append(" ".join(rm))
    for t in (noisy or {}).get("weak") or []:
        st = ((noisy or {}).get("stats") or {}).get(t) or {}
        parts.append(f"「{t}」降权（{st.get('share', 0):.1%} 的{CORPUS_ZH.get(lang, '年报')}都有）")
    absent = (noisy or {}).get("absent") or []
    if absent:
        parts.append("、".join(absent) + " 年报里没出现")
    if not parts:
        return f"{LANG_ZH.get(lang, lang)}关键词：没有变化"
    return f"{LANG_ZH.get(lang, lang)}关键词 " + "；".join(parts)
