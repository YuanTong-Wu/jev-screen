"""One language per human-facing output (the owner rule: a zh page is fully Chinese, an en page fully English).

Plain words for what the data names by code: sources (CNINFO -> 巨潮资讯), filing forms (annual_report_summary ->
年报摘要, 有価証券報告書 -> 日本有价证券报告书), countries, dates and money; and the language of a piece of text
(text_lang) with its translation key (text_sha), for the content the user's own AI agent translates
(jevscreen.translations). Nothing here reads the store or the network.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from typing import Any

LANGS = ("zh", "en")

COUNTRY_ZH: dict[str, str] = {
    "China": "中国", "United States": "美国", "Japan": "日本", "India": "印度", "Taiwan": "台湾", "South Korea": "韩国",
    "Korea": "韩国", "Canada": "加拿大", "United Kingdom": "英国", "Australia": "澳大利亚", "Hong Kong": "香港",
    "Indonesia": "印尼", "Israel": "以色列", "Sweden": "瑞典", "Malaysia": "马来西亚", "France": "法国", "Germany": "德国",
    "Turkey": "土耳其", "Thailand": "泰国", "Saudi Arabia": "沙特阿拉伯", "Singapore": "新加坡", "Brazil": "巴西",
    "Switzerland": "瑞士", "Vietnam": "越南", "Italy": "意大利", "Spain": "西班牙", "United Arab Emirates": "阿联酋",
    "Norway": "挪威", "South Africa": "南非", "Russian Federation": "俄罗斯", "Russia": "俄罗斯", "Mexico": "墨西哥",
    "Poland": "波兰", "Philippines": "菲律宾", "Chile": "智利", "Netherlands": "荷兰", "Kuwait": "科威特",
    "Finland": "芬兰", "Egypt": "埃及", "Belgium": "比利时", "Greece": "希腊", "Denmark": "丹麦", "Pakistan": "巴基斯坦",
    "New Zealand": "新西兰", "Bermuda": "百慕大", "Morocco": "摩洛哥", "Austria": "奥地利", "Ireland": "爱尔兰",
    "Nigeria": "尼日利亚", "Qatar": "卡塔尔", "Luxembourg": "卢森堡", "Peru": "秘鲁", "Argentina": "阿根廷",
    "Romania": "罗马尼亚", "Bangladesh": "孟加拉国", "Sri Lanka": "斯里兰卡", "Colombia": "哥伦比亚",
    "Croatia": "克罗地亚", "Portugal": "葡萄牙", "Iceland": "冰岛", "Kenya": "肯尼亚", "Tunisia": "突尼斯",
    "Cayman Islands": "开曼群岛", "Hungary": "匈牙利", "Cyprus": "塞浦路斯", "Bulgaria": "保加利亚",
    "Lithuania": "立陶宛", "Slovenia": "斯洛文尼亚", "Bahrain": "巴林", "Czech Republic": "捷克", "Czechia": "捷克",
    "Estonia": "爱沙尼亚", "Venezuela": "委内瑞拉", "Monaco": "摩纳哥", "Malta": "马耳他", "Puerto Rico": "波多黎各",
    "British Virgin Islands": "英属维尔京群岛", "Panama": "巴拿马", "Uruguay": "乌拉圭", "Serbia": "塞尔维亚",
    "Mauritius": "毛里求斯", "Gibraltar": "直布罗陀", "Papua New Guinea": "巴布亚新几内亚", "Slovakia": "斯洛伐克",
    "Macau": "澳门", "Macao": "澳门", "Aland Islands": "奥兰群岛", "Faroe Islands": "法罗群岛", "Mongolia": "蒙古",
    "Cambodia": "柬埔寨", "Azerbaijan": "阿塞拜疆", "Barbados": "巴巴多斯", "Jordan": "约旦", "Togo": "多哥",
    "Gabon": "加蓬", "Macedonia": "北马其顿", "North Macedonia": "北马其顿", "Ukraine": "乌克兰",
    "Liechtenstein": "列支敦士登", "Greenland": "格陵兰", "Sudan": "苏丹", "Rwanda": "卢旺达",
    "Kazakhstan": "哈萨克斯坦", "Bahamas": "巴哈马", "Latvia": "拉脱维亚", "Oman": "阿曼", "Jersey": "泽西岛",
    "Guernsey": "根西岛", "Isle of Man": "马恩岛", "Curacao": "库拉索", "Marshall Islands": "马绍尔群岛",
    "Liberia": "利比里亚", "Ghana": "加纳", "Zambia": "赞比亚", "Botswana": "博茨瓦纳", "Zimbabwe": "津巴布韦",
    "Costa Rica": "哥斯达黎加", "Ecuador": "厄瓜多尔", "Jamaica": "牙买加", "Lebanon": "黎巴嫩", "Iraq": "伊拉克",
}

# Sources as the data names them (screen.source_label, a card's quote source, an on-demand fetch key) -> (zh, en)
SOURCE_WORDS: dict[str, tuple[str, str]] = {
    "SEC": ("美国 SEC", "SEC"), "CNINFO": ("巨潮资讯", "CNINFO"), "EDINET": ("日本 EDINET", "EDINET"),
    "DART": ("韩国 DART", "DART"), "MOPS": ("台湾公开资讯观测站 MOPS", "MOPS Taiwan"),
    "BSE": ("印度孟买证交所 BSE", "BSE India"), "profile": ("公司简介", "company profile"),
}
SOURCE_KEYS = {"sec": "SEC", "sec_filing_text": "SEC", "cninfo": "CNINFO", "cninfo_annual_report": "CNINFO",
               "edinet": "EDINET", "edinet_yuho": "EDINET", "dart": "DART", "dart_business_report": "DART",
               "mops": "MOPS", "mops_annual_report": "MOPS", "bse": "BSE", "bse_annual_report": "BSE",
               "公司简介": "profile", "profile": "profile"}

# Filing form ids in plain words (never an internal id such as annual_report_summary on a page)
FORM_WORDS: dict[str, tuple[str, str]] = {
    "annual_report_summary": ("年报摘要", "annual report summary"), "annual_report": ("年报", "annual report"),
    "10-K": ("10-K 年报", "10-K annual report"), "10-K/A": ("10-K 年报（修订）", "10-K annual report (amended)"),
    "10-KT": ("10-K 年报（过渡期）", "10-K annual report (transition period)"),
    "20-F": ("20-F 年报", "20-F annual report"), "20-F/A": ("20-F 年报（修订）", "20-F annual report (amended)"),
    "40-F": ("40-F 年报", "40-F annual report"),
    "有価証券報告書": ("有价证券报告书（年报）", "securities report (annual report)"),
    "사업보고서": ("事业报告（年报）", "business report (annual report)"),
    "股東會年報": ("股东会年报", "annual report to shareholders"),
}
# a source together with its own form, named as one thing
SOURCE_FORM_WORDS: dict[tuple[str, str], tuple[str, str]] = {
    ("CNINFO", "annual_report"): ("巨潮资讯 · 年报", "CNINFO · annual report"),
    ("CNINFO", "annual_report_summary"): ("巨潮资讯 · 年报摘要", "CNINFO · annual report summary"),
    ("SEC", "10-K"): ("美国年报 10-K", "SEC 10-K"), ("SEC", "10-K/A"): ("美国年报 10-K（修订）", "SEC 10-K/A"),
    ("SEC", "10-KT"): ("美国年报 10-K（过渡期）", "SEC 10-KT"),
    ("SEC", "20-F"): ("美国年报 20-F（外国公司）", "SEC 20-F"), ("SEC", "20-F/A"): ("美国年报 20-F（修订）", "SEC 20-F/A"),
    ("SEC", "40-F"): ("美国年报 40-F（加拿大公司）", "SEC 40-F"),
    ("EDINET", "有価証券報告書"): ("日本有价证券报告书", "Japan annual securities report"),
    ("DART", "사업보고서"): ("韩国事业报告（年报）", "Korea business report (annual report)"),
    ("MOPS", "股東會年報"): ("台湾股东会年报", "Taiwan annual report to shareholders"),
    ("BSE", "annual_report"): ("印度年报（孟买证交所）", "BSE India annual report"),
}
MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def pick(pair: tuple[str, str], lang: str) -> str:
    return pair[1] if lang == "en" else pair[0]


def country_words(country: str | None, lang: str) -> str | None:
    """The country in the reader's language (zh: 中国; an unknown name stays as it is)."""
    if not country:
        return None
    return COUNTRY_ZH.get(country, country) if lang == "zh" else country


def form_words(form: Any) -> tuple[str | None, str | None]:
    """(zh, en) plain words of a filing form id; an unknown internal id (snake_case) is left out."""
    f = str(form or "").strip()
    if not f:
        return None, None
    if f in FORM_WORDS:
        return FORM_WORDS[f]
    if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)+", f):
        return None, None
    return f, f


def source_key(source: Any) -> str | None:
    """SEC | CNINFO | EDINET | DART | MOPS | BSE | profile for a source id, label or fetch key (None: unknown)."""
    s = str(source or "").strip()
    if not s:
        return None
    if s in SOURCE_WORDS:
        return s
    return SOURCE_KEYS.get(s) or SOURCE_KEYS.get(s.lower()) or ("SEC" if s.lower().startswith("sec") else None)


def source_words(source: Any, lang: str) -> str | None:
    """A source in plain words (CNINFO -> 巨潮资讯 / CNINFO); None for an unknown one."""
    k = source_key(source)
    return pick(SOURCE_WORDS[k], lang) if k else None


def source_form_words(source: Any, form: Any, lang: str) -> str | None:
    """Where a quote comes from, in one label: '巨潮资讯 · 年报摘要' / 'CNINFO · annual report summary',
    '美国年报 10-K' / 'SEC 10-K', '日本有价证券报告书' / 'Japan annual securities report'; a profile is
    '公司简介' / 'company profile'. Unknown internal ids never show."""
    k = source_key(source)
    f = str(form or "").strip()
    if k == "profile":
        return pick(SOURCE_WORDS["profile"], lang)
    if k and (k, f) in SOURCE_FORM_WORDS:
        return pick(SOURCE_FORM_WORDS[(k, f)], lang)
    fz, fe = form_words(f)
    fw = fe if lang == "en" else fz
    parts = [x for x in (source_words(source, lang), fw) if x]
    return " · ".join(parts) or None


def date_words(value: Any, lang: str) -> str | None:
    """2026-04-01 -> '2026年4月1日' / '1 Apr 2026' (a value that is not a date stays as it is)."""
    s = str(value or "").strip()[:10]
    if not s:
        return None
    try:
        d = dt.date.fromisoformat(s)
    except ValueError:
        return s
    return f"{d.year}年{d.month}月{d.day}日" if lang == "zh" else f"{d.day} {MONTHS_EN[d.month - 1]} {d.year}"


def usd_words(v: float | None, lang: str, *, digits: int = 1) -> str:
    """A market cap / floor: zh '21 亿美元' ('3000 万美元' below 100 million), en '$2.1B' / '$300M'."""
    if not v:
        return "?" if v is None else ("0 美元" if lang == "zh" else "$0")
    v = float(v)
    def num(x: float, d: int) -> str:
        t = f"{x:.{d}f}"
        return t.rstrip("0").rstrip(".") if "." in t else t
    # round first, then choose the unit (999.6 million is "$1B", never "$1000M")
    if lang == "zh":
        if round(v / 1e4) >= 1e4:
            x = v / 1e8
            return num(x, 0 if x >= 10 else 1) + " 亿美元"
        return f"{v / 1e4:.0f} 万美元"
    if round(v / 1e6) >= 1e3:
        return f"${num(v / 1e9, digits)}B"
    return f"${v / 1e6:.0f}M"


# ---------------------------------------------------------------------------------------------- text language

_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KANA = re.compile(r"[぀-ヿㇰ-ㇿｦ-ﾟ]")
_HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]")
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")
# kanji-only Japanese (shinjitai forms neither Chinese script uses), as ondemand.lang_of
_JA_ONLY = frozenset("変気険験読売続転伝図県対様楽薬済経関発実観歩戦駅営価拡鉄圧")


def text_lang(text: str | None) -> str | None:
    """'zh' | 'ja' | 'ko' | 'en' by script, for short texts too (a company name); None without letters. Latin
    letters count as English (the page only needs 'not the reader's language')."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    han, kana, hangul, latin = (len(p.findall(t)) for p in (_HAN, _KANA, _HANGUL, _LATIN))
    cjk = han + kana + hangul
    if not cjk and not latin:
        return None
    if hangul and hangul * 3 >= cjk and hangul * 5 >= latin:
        return "ko"
    if han + kana and (han + kana) * 5 >= latin:
        if kana >= max(1, 0.1 * (han + kana)) or "株式会社" in t or set(t) & _JA_ONLY:
            return "ja"
        return "zh"
    return "en" if latin >= 2 else None


def is_foreign(text: str | None, lang: str) -> bool:
    """True when the text is written in another language than the reader's (it needs a translation)."""
    tl = text_lang(text)
    return tl is not None and tl != ("en" if lang == "en" else "zh")


def norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(text))).strip()


def text_sha(text: str) -> str:
    """The translation key of a text: sha256 of its NFC form with whitespace collapsed (hex)."""
    return hashlib.sha256(norm_text(text).encode("utf-8")).hexdigest()
