"""Render a screen result (jevscreen.screen.screen) as report.md and as a short console summary.

Pure formatting: no store access, no network. Missing values print as '-', never 0.
"""
from __future__ import annotations

from typing import Any

GAP_LIST_LIMIT = 30
USER_ONLY_ZH = "用户判断（年报未写明）"     # calib.USER_ONLY_NOTE: a user yes the filing does not support
WANT_ZH = {"explicit": "要a（直接）", "partial": "要b（相关）", "no": "不要", "unsure": "?"}
# calib.NO_CHIP_TEXT (+ g; h-k the role chips): the reason letter of a 不要 answer, spelled out for report.md
NO_CHIP_ZH = {"c": "只是做/用这项技术，不卖想法里的东西", "d": "词对上了，意思不一样", "e": "泛泛的服务/转售，罗列很多",
              "f": "只是一句展望/计划", "g": "只有大类，没提想法的对象", "h": "是买方/客户/用户，不是供应方",
              "i": "只是持股/投资，自己不做", "j": "上游通用零件/材料/设备", "k": "卖硬件给运营方，自己不运营"}
BELOW_CUT_ZH = "在截线外，你答了要，照样列出"
READ_NOTE_PREFIX = "只读了"
# shells marks (jevscreen.shells.MARK_ZH; spac only shows when the user kept shells with --shells keep)
FLAG_ZH = {"st": "ST风险警示", "star_st": "*ST退市风险", "spac": "SPAC", "spac_like": "像SPAC的名字"}
WHY_HINT_ZH = "想知道某家公司为什么不在，直接问你的 AI"
WHY_HINT_EN = "Ask your AI why a company is missing"
STAGE_ZH = {"null_mcap": "没有市值数据", "below_min_mcap": "市值低于门槛", "below_min_volume": "成交量低于门槛",
            "other_country": "不在所选国家/地区", "shell": "排除壳公司", "no_description": "没有公司简介"}

GAP_TITLES: dict[str, str] = {
    "no_description": "No description in the store (not screened)",
    "l1_skipped_budget": "L1 skipped: budget exhausted",
    "l1_failed": "L1 failed or uncertain",
    "l2_not_sent_l2_max": "L1 pass but not sent to L2 (beyond --l2-max)",
    "l2_skipped_budget": "L2 skipped: budget exhausted",
    "l2_failed": "L2 failed or uncertain",
    "l2_contradicted": "L2 contradicted (dropped from the output)",
    "l2_profile_only": "No annual report text: L2 read the company profile (the same kind of text as L1)",
    "sec_text_fallback": "Annual-report document listed but text unreadable (profile used instead)",
    "sec_document_old": "Annual-report text older than 3 years (flagged l2_doc_stale)",
}
EVIDENCE_TITLE = {"zh": "年报原文与缺口", "en": "Annual-report evidence and gaps"}
MAYBE_MISSING_TITLE = {"zh": "可能被漏掉的公司（只读了简介，不是'不符合'）",
                       "en": "May be missing from your list (profile only, not a 'no')"}
MAYBE_MISSING_LIMIT = 20
DOC_MARK = {"profile": {"zh": "简介", "en": "profile"}, "fetched_now": {"zh": "新年报", "en": "new report"}}

# Official annual-report sources (screen.OFFICIAL_DOC_SOURCES, same labels; tests/test_bse.py checks they agree).
SOURCE_LABELS = {"sec_filing_text": "SEC", "cninfo_annual_report": "CNINFO", "edinet_yuho": "EDINET",
                 "dart_business_report": "DART"}
SOURCE_LABELS["mops_annual_report"] = "MOPS"   # Taiwan (sources/mops.py)
SOURCE_LABELS["bse_annual_report"] = "BSE"     # India (sources/bse.py)
# "SEC / CNINFO / ..." for the funnel row, "SEC, CNINFO, ..." for the licence note: built here so a new source
# cannot be left out of either.
OFFICIAL_SOURCES_SLASHED = " / ".join(SOURCE_LABELS.values())
OFFICIAL_SOURCES_LISTED = ", ".join(SOURCE_LABELS.values())

# (funnel key, label, shown in dry runs)
FUNNEL_STEPS: tuple[tuple[str, str, bool], ...] = (
    ("universe_rows", "universe rows (primary stocks + DRs, one per company)", True),
    ("null_mcap", "  - NULL market cap (excluded)", True),
    ("below_min_mcap", "  - below --min-mcap", True),
    ("below_min_volume", "  - below --min-volume / NULL volume", True),
    ("other_country", "  - other countries (--countries)", True),
    ("universe", "universe after filters", True),
    ("shells_dropped", "  - shells filter (blank-check companies; list in shells_dropped.csv)", True),
    ("no_description", "  - no description (gap, not screened)", True),
    ("described", "with a description = L1 inputs", True),
    ("l1_sent", "L1 sent", False),
    ("l1_ok", "L1 answered", False),
    ("l1_pass", "L1 pass", False),
    ("l2_inputs", "L2 inputs", True),
    ("l2_sec_inputs", f"  - annual-report excerpts ({OFFICIAL_SOURCES_SLASHED})", True),
    ("l2_doc_fetched", "    of which fetched now (on-demand annual reports)", False),
    ("l2_profile_inputs", "  - profile only (no annual report)", True),
    ("l2_doc_fetch_planned", "    on-demand fetch: planned", False),
    ("l2_doc_deferred", "    on-demand fetch: deferred (time / per-run cap)", False),
    ("l2_doc_unavailable", "    still profile only after the fetch (gaps)", False),
    ("l2_sent", "L2 sent", False),
    ("l2_ok", "L2 answered", False),
    ("l2_verified", "L2 explicit / partial (ranked)", False),
    ("l2_contradicted", "L2 contradicted (dropped)", False),
    ("unverified", "unverified L1 passes (separate list)", False),
    ("output", "output rows", False),
)


def money(v: float | None) -> str:
    if v is None:
        return "-"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:,.0f}"


def pct(v: float | None) -> str:
    return "-" if v is None else f"{100 * v:.1f}%"


def num(v: float | None, fmt: str = "{:.2f}") -> str:
    return "-" if v is None else fmt.format(v)


def usd(v: float | None) -> str:
    return "-" if v is None else f"${v:.4f}"


def _cell(text: Any) -> str:
    s = "-" if text is None or text == "" else str(text)
    return s.replace("|", "\\|").replace("\n", " ")


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _text_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    fmt = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths))  # noqa: E731
    return "\n".join([fmt(headers), fmt(["-" * w for w in widths]), *(fmt(r) for r in rows)])


def _cagr(r: dict) -> str:
    v = r.get("revenue_cagr_3y_local")
    if v is None:
        return "-"
    return f"{pct(v)} ({r.get('cagr_currency') or '?'} {r.get('cagr_years') or ''})".strip()


def _layer_rows(result: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for layer in ("l1", "l2"):
        v = result["layers"].get(layer, {})
        est = v.get("estimate") or {}
        by = v.get("by_status") or {}
        rows.append([layer.upper(), v.get("inputs", 0),
                     est.get("requests", "-") if "error" not in est else "error",
                     usd(est.get("est_cost_usd")) if est else "-",
                     num(est.get("est_seconds"), "{:.0f} s") if est else "-",
                     v.get("requests", 0), usd(v.get("cost_usd")), v.get("cache_hits", 0),
                     ", ".join(f"{k} {n}" for k, n in sorted(by.items())) or "-", num(v.get("seconds"), "{:.1f} s")])
    return rows


def _labels(result: dict[str, Any], layer: str) -> str:
    lab = (result["layers"].get(layer) or {}).get("labels") or {}
    return ", ".join(f"{k} {n}" for k, n in lab.items()) or "-"


def _evidence_cell(r: dict, lang: str) -> str | None:
    """L2 read column: 'profile' (简介) on profile-only rows, 'new report' (新年报) on rows read from a document the
    on-demand fetch just stored, else the evidence kind as before."""
    ev = r.get("l2_evidence")
    if ev == "profile":
        return DOC_MARK["profile"][lang]
    if ev and r.get("doc_fetch") == "fetched_now":
        return f"{ev} · {DOC_MARK['fetched_now'][lang]}"
    return ev


def _result_table(rows: list[dict], ranked: bool, lang: str = "en") -> str:
    return _md_table(
        (["#"] if ranked else []) + ["security", "name", "region", "mcap USD", "rev TTM USD", "rev 3y CAGR (local)",
                                     "L1", "p_core", "L2", "L2 read", "score", "evidence", "filing", "tiers"],
        [([r["rank"]] if ranked else []) + [
            r["security_id"], r["name"], r["region"], money(r["market_cap_usd"]), money(r["revenue_ttm_usd"]),
            _cagr(r), r["l1_label"], num(r["l1_p_core"]), _l2_cell(r),
            _evidence_cell(r, lang), num(r["score"]),
            (r["evidence_excerpt"] or "") + (f" ({r['evidence_url']})" if r["evidence_url"] else ""),
            (" ".join(x for x in (_src(r.get("filing_source")), r.get("filing_form"), r.get("filing_date"),
                                  f"({r['doc_lang']})" if r.get("doc_lang") else None) if x)
             + (" (old)" if r.get("l2_doc_stale") else "")) or None,
            " / ".join(t for t in (r["l1_input_tier"], r["l2_input_tier"]) if t)] for r in rows])


def _marks(r: dict) -> list[str]:
    """Calibration marks of a row: the user's judgment, backfill, 边缘 and a short read count."""
    out = []
    if r.get("verdict_source") == "user":
        out.append(USER_ONLY_ZH)
    elif r.get("verdict_source") == "evidence+user":
        out.append("你也选了要")
    if r.get("below_cut"):
        out.append(BELOW_CUT_ZH)
    if r.get("backfill"):
        out.append("递补，未经确认")
    if r.get("l2_edge"):
        out.append("边缘")
    if r.get("l2_read_note"):
        out.append(r["l2_read_note"])
    if r.get("l2_label_cap"):
        out.append(r["l2_label_cap"])
    if r.get("l2_should_pass"):
        out.append("应通过" + ("" if r["l2_should_pass"].get("held") else "，仍偏低"))
    out += [FLAG_ZH[f] for f in r.get("flags") or () if f in FLAG_ZH and f != "spac_like"]
    return out


def _pin_source(r: dict) -> str:
    """A `sieve pin` the human asked their AI for, with its date ('' for a card answer: the section says so)."""
    if r.get("user_pin_via") == "pin":
        return f"你让 AI 钉选的（{str(r.get('user_pin_at') or '')[:10]}）"
    return ""


def _chip_zh(chip: str | None) -> str | None:
    """'c 只是做/用这项技术，不卖想法里的东西' (the letter kept: it is what the user typed)."""
    return f"{chip} {NO_CHIP_ZH[chip]}" if chip in NO_CHIP_ZH else chip


def _l2_cell(r: dict) -> str | None:
    base = r.get("l2_status") or r["l2_label"]
    marks = _marks(r)
    return " · ".join([str(base or "-")] + marks) if marks else base


KEYWORD_STATUS = {
    "generated": "translated by the local model",
    "user": "English idea with --keywords (no translation needed)",
    "not_needed": "English idea (no translation needed)",
    "disabled": "translation turned off (--no-translate)",
    "unavailable": "local model unavailable: idea used verbatim",
    "sieve": "idea_en from the sieve (written by your AI; the local model was not called)",
    "frozen": "idea_en of the last paid run (frozen: the sieve's newer idea_en needs `sieve set --reprice`)",
}


def _src(source_id: str | None) -> str | None:
    return SOURCE_LABELS.get(source_id, source_id) if source_id else None


def _kv(d: dict[str, Any]) -> str:
    return ", ".join(f"{_src(k) or k} {v}" for k, v in d.items()) or "-"


def _keywords_section(result: dict[str, Any]) -> list[str]:
    k = result.get("keywords") or {}
    tb = result.get("terms_by_lang") or {"en": result.get("terms") or []}
    out = ["## Idea and keywords", ""]
    out.append(f"- idea: {result['idea']}")
    if k.get("idea_en"):
        out.append(f"- idea (English, used in the Jev questions with the original): {k['idea_en']}")
    status = k.get("status")
    if status:
        line = f"- keywords: {KEYWORD_STATUS.get(status, status)}"
        if status == "generated":
            line += (f" (model {k.get('model') or '-'}, {'cached' if k.get('cached') else 'generated now'}, "
                     f"{num(k.get('seconds'), '{:.1f} s')})")
            if k.get("user_keywords"):
                line += "; English excerpt terms from --keywords"
        if k.get("error"):
            line += f": {k['error']}"
        out.append(line)
    out.append("")
    out.append(_md_table(["document language", "excerpt keyword terms"],
                         [[lang, ", ".join(v) if v else None] for lang, v in tb.items()]))
    out.append("")
    return out


def _has_calibration(result: dict[str, Any]) -> bool:
    rows = (result.get("rows") or []) + (result.get("unverified") or [])
    return bool(result.get("calibration") or result.get("excluded_by_user") or result.get("sieve_hint")
                or (((result.get("layers") or {}).get("l2") or {}).get("band") or {}).get("items")
                or any(r.get("l2_edge") for r in rows))


def _who(r: dict[str, Any]) -> str:
    return f"{r.get('name') or '-'} ({r.get('security_id') or r.get('company_key') or '?'})"


def _check_line(c: dict[str, Any]) -> str:
    """'should_pass Sailwise 通过（#3）' / 'should_pass Sailwise 未通过（L1 没过）' / 'should_fail X 没进名单'."""
    yes = c.get("want") in ("explicit", "partial")
    name = f"{c.get('name') or c.get('security_id')}"
    if not c.get("in_universe"):
        why = "不在这次的公司范围里（市值 / 国家 / 没有简介）"
        return f"should_{'pass' if yes else 'fail'} {name}：{why}"
    if yes:
        flag = f"；{c['flag_zh']}" if c.get("flag_zh") else ""
        if c.get("in_output"):
            return f"should_pass {name} 通过（#{c.get('rank')}{'，L2 ' + c['l2_label'] if flag else ''}{flag}）"
        why = "L1 没过" if not c.get("l1_pass") else f"L2 {c.get('l2_label') or '没读到'}"
        return f"should_pass {name} 未通过（{why}{flag}）"
    return (f"should_fail {name} 仍在名单里（#{c.get('rank')}）" if c.get("in_output")
            else f"should_fail {name} 没进名单")


def _calibration_section(result: dict[str, Any]) -> list[str]:
    """## 校准: the sieve loaded (answers, pins, rules adopted / rejected, keyword log), the user's judgments
    (用户判断 rows, 你排除的, backfill), example checks, companies L1 missed, and the repeated reads (band, 边缘, read
    counts). Empty for a run without any of these."""
    if not _has_calibration(result):
        return []
    cal = result.get("calibration") or {}
    rows, unv = result.get("rows") or [], result.get("unverified") or []
    out = ["## 校准", ""]
    if result.get("sieve_hint"):
        h = result["sieve_hint"]
        out.append(f"- 这个想法还没有校准文件。最接近的是「{h.get('idea')}」（相似度 {h.get('similarity')}）：要用它，加 "
                   f"`--sieve {h.get('path')}`")
    if cal:
        out.append(f"- {cal.get('summary_zh')}（{cal.get('sieve_path') or '-'}，版本 {cal.get('sieve_version')}；"
                   f"钉选 {cal.get('pins', 0)} 家）")
        out.append(f"- 采用的规则：{', '.join(cal.get('rules') or []) or '（无）'}")
        for r in cal.get("rejected_rules") or []:
            out.append(f"- 没采用：{r.get('id')} — {r.get('why_zh') or ''}")
        for lang, terms in (cal.get("keywords_add") or {}).items():
            out.append(f"- {lang} 关键词（校准加的）：{', '.join(terms)}")
        for lang, terms in (cal.get("keywords_weak") or {}).items():
            out.append(f"- {lang} 降权的词：{', '.join(terms)}")
        for lang, log in (cal.get("keyword_log") or {}).items():
            for e in log[-10:]:
                extra = (f"df {e.get('df')}" + (f"，lift {e['lift']}" if e.get("lift") is not None else "")
                         + (f"，{100 * e['share']:.1f}%" if e.get("share") is not None else ""))
                out.append(f"- 关键词记录 {lang}：{e.get('action')} {e.get('term')}（{e.get('source')}，{extra}）")
    users = [r for r in rows if r.get("user_verdict")]
    if users:
        out.append("")
        out.append("### 用户判断（你答过的公司）")
        out.append("")
        out.append("钉选是你的判断，不是年报证据：年报没写明的标为「" + USER_ONLY_ZH + "」。")
        out.append("")
        out.append(_md_table(["#", "security", "name", "你", "年报（L2）", "来源"],
                             [[r.get("rank"), r["security_id"], r["name"], WANT_ZH.get(r["user_verdict"], "?"),
                               r.get("l2_status") or r.get("l2_label"),
                               (USER_ONLY_ZH if r.get("verdict_source") == "user" else "年报也支持")
                               + (f" · {BELOW_CUT_ZH}" if r.get("below_cut") else "")
                               + (f" · {_pin_source(r)}" if _pin_source(r) else "")]
                              for r in users]))
    excl = result.get("excluded_by_user") or []
    if excl:
        out.append("")
        out.append(f"### 你排除的：{len(excl)}")
        out.append("")
        out.append("你答了「不要」，所以不排名（证据照列，没有删掉）。")
        out.append("")
        out.append(_md_table(["security", "name", "理由", "L2", "evidence"],
                             [[r["security_id"], r["name"], _chip_zh(r.get("user_chip")) or _pin_source(r) or None,
                               r.get("l2_status") or r.get("l2_label"), r.get("evidence_excerpt")] for r in excl]))
    back = [r for r in rows if r.get("backfill")]
    if back:
        out.append("")
        out.append(f"- 递补，未经确认：{'、'.join(_who(r) for r in back)}（别人被你排除后才进前 "
                   f"{(result.get('params') or {}).get('max_out', '-')}）")
    listed = {r.get("company_key") for r in rows + excl}      # a pinned company without a profile is ranked
    extras = [x for x in cal.get("extras") or [] if x.get("company_key") not in listed]
    if extras:
        out.append("")
        out.append(f"### 你关心的公司（在这次筛选条件以外）：{len(extras)}")
        out.append("")
        out.append("这些公司在你的校准文件里，但被市值 / 国家 / 成交量 / 没有简介等条件挡在外面：只读了证据写在这里，不进名单。")
        out.append("")
        out.append(_md_table(["security", "name", "挡住它的条件", "mcap USD", "L2", "读的是"],
                             [[x["security_id"], x["name"], STAGE_ZH.get(x.get("stage"), x.get("stage")),
                               money(x.get("market_cap_usd")), x.get("l2_label") or x.get("l2_status") or "-",
                               {"annual_report": "年报", "profile": "简介"}.get(x.get("l2_evidence"), "-")]
                              for x in extras]))
    checks = cal.get("checks") or []
    forced = [f for f in cal.get("forced") or [] if not f.get("l1_pass")]
    if checks or forced:
        out.append("")
        out.append("### 示例检查")
        out.append("")
        out += [f"- {_check_line(c)}" for c in checks]
        out += [f"- L1 漏掉了 {_who(f)}（L1 {f.get('l1_label') or '-'}；L2 照读：{f.get('l2_label') or '-'}）"
                for f in forced]
    band = (((result.get("layers") or {}).get("l2") or {}).get("band") or {})
    band = band if band.get("items") else None
    edge = [r for r in rows + unv if r.get("l2_edge")]
    reads: dict[int, int] = {}
    for r in rows + unv:
        if r.get("l2_reads"):
            reads[r["l2_reads"]] = reads.get(r["l2_reads"], 0) + 1
    once = [r for r in rows + unv if r.get("l2_read_note")]
    if band or edge or reads:
        out.append("")
        out.append("### 多读几次（稳定性）")
        out.append("")
        if band:
            out.append(f"- 边界附近（第 1 次读 P(explicit)+P(partial) 在 0.30–0.75）{band.get('items', 0)} 家，多读 "
                       f"{len(band.get('reads') or [])} 次：成功 {band.get('ok', 0)}，跳过 {band.get('skipped', 0)}，"
                       f"{usd(band.get('cost_usd'))}；按平均值判断")
        if reads:
            out.append("- 读取次数（列表中）：" + "，".join(f"{n} 次 {c} 家" for n, c in sorted(reads.items())))
        out.append(f"- 边缘（平均 0.40–0.60，再读可能翻）：{len(edge)} 家"
                   + (f"：{'、'.join(_who(r) for r in edge[:12])}" + ("…" if len(edge) > 12 else "") if edge else ""))
        if once:
            out.append(f"- 预算不够没读满：{'、'.join(_who(r) + ' ' + r['l2_read_note'] for r in once[:12])}")
    out.append("")
    return out


def _shells_lines(result: dict[str, Any]) -> list[str]:
    """One line for the shells filter (only when it removed or kept something) and the ST-list gap."""
    sh = result.get("shells") or {}
    out = []
    if sh.get("dropped"):
        out.append(f"- 排除壳公司：{sh['dropped']} 家（空壳收购公司 SPAC；名单在 shells_dropped.csv）。"
                   f"问你的 AI：为什么排除了某家。 / Shells filter: {sh['dropped']} blank-check companies removed.")
    if sh.get("kept_protected"):
        out.append(f"- 你的校准文件里的 {sh['kept_protected']} 家像壳公司，但没有排除（你点名了它们）。 / "
                   f"{sh['kept_protected']} shell-like companies your sieve names were kept.")
    if sh.get("kept_mode"):
        out.append(f"- --shells keep：{sh['kept_mode']} 家壳公司保留在筛选里（表里标 SPAC）。 / --shells keep: "
                   f"{sh['kept_mode']} shells stayed in the screen (marked SPAC).")
    if (sh.get("st_gap") or {}).get("zh"):
        out.append(f"- 缺口：{sh['st_gap']['zh']}" + (f" / Gap: {sh['st_gap']['en']}" if sh["st_gap"].get("en")
                                                     else ""))
    if out:
        out.append("")
    return out


def _lang(result: dict[str, Any]) -> str:
    from .ondemand import lang_of
    return lang_of(result.get("idea"))


def _evidence_section(result: dict[str, Any], lang: str) -> list[str]:
    """## 年报原文与缺口 / Annual-report evidence and gaps: which L2 inputs read an official annual report (fact),
    why the others read only a profile (gap, grouped by reason and market), and the profile-only companies L2 did
    not confirm (may be missing: a gap, not a 'no'). Renders phase-1 reports too; the fetch details only when
    layers['fetch'] exists."""
    if result.get("dry_run"):
        return []
    from . import ondemand
    l2 = result["layers"].get("l2") or {}
    f = result.get("funnel") or {}
    fetch = result["layers"].get("fetch")
    gaps = result.get("gaps") or {}
    n_doc, n_prof = int(l2.get("sec_inputs") or 0), int(l2.get("profile_inputs") or 0)
    if not (n_doc or n_prof):
        return []
    n_new = int(f.get("l2_doc_fetched") or 0)
    out = [f"## {EVIDENCE_TITLE[lang]}", ""]
    if lang == "zh":
        out.append(f"- 事实：{n_doc} 家读了官方年报原文" + (f"（其中 {n_new} 家本次新抓）" if n_new else "")
                   + f"；{n_prof} 家只读了公司简介（系统判断的依据较弱，正面结论只算一半分）。")
    else:
        out.append(f"- Fact: {n_doc} companies were read from official annual reports"
                   + (f" ({n_new} fetched now)" if n_new else "")
                   + f"; {n_prof} read the company profile only (weaker evidence: a positive label counts half).")
    if fetch:
        out.append(f"- {fetch.get('summary_zh') if lang == 'zh' else fetch.get('summary_en')}")
    items = gaps.get("l2_doc_unavailable")
    if items:
        out.append("")
        out.append("缺口（按原因）：" if lang == "zh" else "Gaps (by reason):")
        out.append("")
        out += [f"- {line}" for line in ondemand.gap_lines(items, lang)]
    elif n_prof and not fetch:
        out.append("- " + ("缺口：这次没有补抓年报，只读简介的原因没有逐家查（jevscreen fetch-docs latest 可以补抓）"
                           if lang == "zh" else "Gap: no annual-report fetch ran for this run, so the reasons were not "
                           "checked per company (jevscreen fetch-docs latest fetches them)"))
    reason_of = {g["security_id"]: g for g in items or []}
    maybe = [r for r in (result.get("rows") or []) + (result.get("unverified") or [])
             if r.get("l2_evidence") == "profile" and r.get("l2_label") in ("insufficient", "partial")]
    maybe.sort(key=lambda r: (-(r.get("l1_p_core") or 0.0), -(r.get("market_cap_usd") or 0.0), r["security_id"]))
    if maybe:
        out.append("")
        out.append(f"### {MAYBE_MISSING_TITLE[lang]}: {len(maybe)}")
        out.append("")
        head = (["证券", "名称", "市值 USD", "L1 p_core", "L2（简介）", "为什么只有简介"] if lang == "zh"
                else ["security", "name", "mcap USD", "L1 p_core", "L2 (profile)", "why profile only"])
        out.append(_md_table(head, [[r["security_id"], r.get("name"), money(r.get("market_cap_usd")),
                                     num(r.get("l1_p_core")), r.get("l2_label"),
                                     ondemand.short_reason(reason_of[r["security_id"]]["reason"], lang)
                                     if r["security_id"] in reason_of else "-"]
                                    for r in maybe[:MAYBE_MISSING_LIMIT]]))
        if len(maybe) > MAYBE_MISSING_LIMIT:
            out.append("\n" + (f"……另有 {len(maybe) - MAYBE_MISSING_LIMIT} 家（见 results.csv / results.json）"
                               if lang == "zh" else f"... and {len(maybe) - MAYBE_MISSING_LIMIT} more (results.csv / "
                               "results.json)"))
    out.append("")
    return out


def render_markdown(result: dict[str, Any]) -> str:
    p, f = result["params"], result["funnel"]
    dry = result.get("dry_run")
    lang = _lang(result)
    out = [f"# Jev screen: {result['idea']}", ""]
    out.append(f"- run: `{result['run_id']}`  status: **{result['status']}**"
               + ("  (dry run: no paid calls, nothing written to the store)" if dry else ""))
    out.append(f"- started {result['started_at']}, finished {result['finished_at']} "
               f"({result['timing'].get('total_s', 0):.1f} s)")
    out.append(f"- cost: {usd(result['cost_usd'])} of budget {usd(result['budget_usd'])}")
    out.append("")
    stw = result.get("st_warning")
    if stw:
        out.append(f"> **{stw['text_zh']}** （{', '.join(stw['security_ids'])}）")
        out.append(f"> {stw['text_en']}")
        out.append("")
    if result.get("warnings"):
        out.append("## Warnings")
        out.append("")
        out += [f"- **{w}**" for w in result["warnings"]]
        out.append("")
    out.append("## Parameters")
    out.append("")
    out.append(_md_table(["parameter", "value"], [[k, v] for k, v in p.items()]))
    out.append("")
    out += _keywords_section(result)
    out.append("## Funnel")
    out.append("")
    frows = [[label + (" (upper bound)" if dry and k.startswith("l2_") else ""), f.get(k, 0)]
             for k, label, in_dry in FUNNEL_STEPS if (in_dry or not dry) and k in f]
    out.append(_md_table(["step", "companies"], frows))
    out.append("")
    out += _shells_lines(result)
    if not dry:
        out.append(f"- L1 labels: {_labels(result, 'l1')}")
        out.append(f"- L2 labels: {_labels(result, 'l2')}")
    l2 = result["layers"].get("l2") or {}
    out.append(f"- L2 inputs: {l2.get('sec_inputs', 0)} annual-report excerpts "
               f"({l2.get('keyword_excerpts', 0)} with a keyword paragraph), {l2.get('profile_inputs', 0)} profile "
               "only. A profile-only L2 label reads the same kind of text as L1: it is marked 'profile' and a positive "
               "label weighs half.")
    if l2.get("inputs_by_source"):
        out.append(f"- Annual-report excerpts by source: {_kv(l2['inputs_by_source'])}; by language: "
                   f"{_kv(l2.get('inputs_by_lang') or {})}"
                   + (f"; {l2['summary_inputs']} from a short summary (no full report of that year)"
                      if l2.get("summary_inputs") else ""))
    out.append("")
    if dry and result.get("dry_run_budget"):
        b = result["dry_run_budget"]
        out.append("### Estimate against the budget")
        out.append("")
        out.append(_md_table(["", "USD"], [
            ["estimated cost L1 + L2 (L2 upper bound)", usd(b["est_cost_usd"])],
            ["reserved against the budget (x reserve factor)", usd(b["est_reserved_usd"])],
            ["budget", usd(b["budget_usd"])]]))
        out.append("")
        out.append(f"- estimated time: ~{b.get('est_seconds', 0):.0f} s")
        if b.get("reservation_exceeds_budget"):
            out.append("- **The reservation exceeds the budget: the run would stop early.**")
        out.append("- The L2 figures bound the number of companies only (top companies by market cap stand in for "
                   "the unknown L1 passes); the real L2 input texts are built after L1.")
        out.append("")
    out.append("## Jev layers")
    out.append("")
    out.append(_md_table(["layer", "inputs", "est. requests", "est. cost", "est. time", "requests sent", "cost",
                          "cache hits", "results by status", "time"], _layer_rows(result)))
    out.append("")
    for layer in ("l1", "l2"):
        est = result["layers"].get(layer, {}).get("estimate") or {}
        if est.get("basis") or est.get("error"):
            out.append(f"- {layer.upper()} estimate basis: {est.get('basis') or est.get('error')}")
    if dry:
        out.append("- Estimated time assumes the client's rate and worker limits and ~4 s per request; "
                   "L2 figures are an upper bound (top companies by market cap stand in for L1 passes).")
    out.append("")
    if result.get("errors") or result.get("notes"):
        out.append("## Errors and notes")
        out.append("")
        out += [f"- {e}" for e in result.get("errors", []) + result.get("notes", [])]
        out.append("")
    out.append("## Ranked results (L2 explicit / partial)")
    out.append("")
    rows = result.get("rows") or []
    if rows:
        out.append(_result_table(rows, ranked=True, lang=lang))
    else:
        out.append("(no rows" + (": dry run)" if dry else ": no company passed layer 2)"))
    out.append("")
    unv = result.get("unverified") or []
    if unv:
        out.append(f"## Unverified L1 passes (not ranked): {f.get('unverified', len(unv))}")
        out.append("")
        out.append("Layer 1 passed these companies, but layer 2 did not confirm them: 'insufficient', or L2 failed, "
                   "was skipped for budget, beyond --l2-max, or did not run (column L2).")
        out.append("")
        out.append(_result_table(unv, ranked=False, lang=lang))
        if f.get("unverified", 0) > len(unv):
            out.append(f"\n... and {f['unverified'] - len(unv)} more")
        out.append("")
    out += _calibration_section(result)
    unr = [u for layer in ("l1", "l2") for u in ((result["layers"].get(layer) or {}).get("uncertain_not_resent") or [])]
    if unr:
        out.append("## Earlier sends with an unknown outcome (not resent)")
        out.append("")
        out.append(_md_table(["layer", "items", "previous run"],
                             [[u.get("layer"), u.get("items"), u.get("previous_run_id")] for u in unr]))
        out.append("")
        out.append("Rerun with `--retry-uncertain` to resend them; they may have been charged already.")
        out.append("")
    out += _evidence_section(result, lang)
    out.append("## Gaps")
    out.append("")
    gaps = result.get("gaps") or {}
    any_gap = False
    for key, title in GAP_TITLES.items():
        items = gaps.get(key) or []
        if not items:
            continue
        any_gap = True
        out.append(f"### {title}: {len(items)}")
        out.append("")
        shown = sorted(items, key=lambda x: -(x.get("market_cap_usd") or 0))[:GAP_LIST_LIMIT]
        out.append(_md_table(["security", "name", "mcap USD", "status / note"],
                             [[g["security_id"], g["name"], money(g.get("market_cap_usd")),
                               g.get("error") or g.get("note") or g.get("status")] for g in shown]))
        if len(items) > GAP_LIST_LIMIT:
            out.append(f"\n... and {len(items) - GAP_LIST_LIMIT} more (full list in results.json)")
        out.append("")
    if not any_gap:
        out += ["(none)", ""]
    out.append("## Licence")
    out.append("")
    tiers = result.get("tiers_used") or []
    out.append(f"Input licence tiers: {', '.join(tiers) or '-'}.")
    if result.get("gray_private"):
        out.append("")
        out.append("**Personal use only.** Some judgements were made from gray-private text (TradingView / "
                   "FinanceDatabase descriptions). This report, results.csv, results.json and l2_inputs.jsonl are "
                   "derived from it and must not be published, shared or redistributed.")
    out.append("")
    out.append("**Run folder: personal use only (仅限个人使用).** funnel.jsonl.gz (one line per universe company, with "
               "its market cap) and shells_dropped.csv (industry, revenue) are derived from the TradingView market "
               "data (gray-private); l2_inputs.jsonl holds the step-2 input text. Keep this folder local: do not "
               "share or publish it (不要分享这个运行目录).")
    if "official-private" in tiers:
        out.append("")
        out.append(f"Annual-report excerpts ({OFFICIAL_SOURCES_LISTED}; official-private) are issuer-written text: keep "
                   "the verbatim excerpts local.")
    out.append("")
    return "\n".join(out)


def format_console(result: dict[str, Any], top_n: int = 20) -> str:
    f = result["funnel"]
    lines = [f"screen {result['run_id']}  status={result['status']}"]
    stw = result.get("st_warning")
    if stw:
        lines.append(f"{stw['text_zh']} ({', '.join(stw['security_ids'])})")
        if stw.get("text_en"):
            lines.append(stw["text_en"])
    for w in result.get("warnings") or []:
        lines.append(f"WARNING: {w}")
    k = result.get("keywords") or {}
    if k.get("idea_en"):
        lines.append(f"idea (en): {k['idea_en']}  [{k.get('model') or 'local model'}"
                     + (", cached" if k.get("cached") else "") + f", {num(k.get('seconds'), '{:.1f}')} s]")
    tb = result.get("terms_by_lang") or {"en": result.get("terms") or []}
    lines.append("keyword terms: " + "; ".join(f"{lang} {', '.join(v)}" for lang, v in tb.items() if v)
                 if any(tb.values()) else "keyword terms: -")
    cal = result.get("calibration") or {}
    if cal.get("summary_zh"):
        lines.append(f"{cal['summary_zh']}（{cal.get('sieve_path') or '-'}）")
    for c in cal.get("checks") or []:
        if c.get("flag_zh") or (c.get("want") in ("explicit", "partial") and c.get("in_universe")
                                and not c.get("in_output")):
            lines.append(f"注意：{_check_line(c)}")
    if result.get("sieve_hint"):
        h = result["sieve_hint"]
        lines.append(f"这个想法还没有校准文件；最接近的是「{h.get('idea')}」（相似度 {h.get('similarity')}）："
                     f"--sieve {h.get('path')}")
    if (result.get("params") or {}).get("from_run"):
        lines.append(f"L1 loaded from run {result['params']['from_run']} ($0)")
    filt = (f"universe rows {f.get('universe_rows', '-')} (NULL mcap {f.get('null_mcap', '-')}, below min mcap "
            f"{f.get('below_min_mcap', '-')}, below min volume {f.get('below_min_volume', '-')}, other countries "
            f"{f.get('other_country', '-')}" + (f", shells {f['shells_dropped']}" if f.get("shells_dropped") else "")
            + ")")
    l2 = result["layers"].get("l2") or {}
    split = f"(annual report {l2.get('sec_inputs', 0)}, profile only {l2.get('profile_inputs', 0)})"
    if result.get("dry_run"):
        lines.append(f"funnel: {filt} -> universe {f['universe']} -> described {f['described']} -> L1 inputs "
                     f"{f['l1_inputs']} -> L2 inputs (upper bound) {f['l2_inputs']} {split}")
        for layer in ("l1", "l2"):
            est = result["layers"].get(layer, {}).get("estimate") or {}
            if "error" in est:
                lines.append(f"{layer.upper()} estimate: unavailable ({est['error']})")
            elif est:
                lines.append(f"{layer.upper()} estimate: {est.get('requests', '-')} requests, "
                             f"{usd(est.get('est_cost_usd'))} (reserved {usd(est.get('est_reserved_usd'))}), "
                             f"~{num(est.get('est_seconds'), '{:.0f}')} s")
                if (est.get("band_reads") or {}).get("text"):
                    lines.append(f"  {est['band_reads']['text']} (included above)")
        b = result.get("dry_run_budget")
        if b:
            lines.append(f"total estimate: {usd(b['est_cost_usd'])} (reserved {usd(b['est_reserved_usd'])}) of budget "
                         f"{usd(b['budget_usd'])}, ~{b.get('est_seconds', 0):.0f} s"
                         + ("  RESERVATION EXCEEDS BUDGET" if b.get("reservation_exceeds_budget") else ""))
    else:
        lines.append(f"funnel: {filt}")
        lines.append(f"  -> universe {f['universe']} -> described {f['described']} -> L1 sent {f['l1_sent']} "
                     f"(ok {f.get('l1_ok', 0)}) -> L1 pass {f['l1_pass']} -> L2 sent {f['l2_sent']} {split} "
                     f"(ok {f.get('l2_ok', 0)}) -> verified {f.get('l2_verified', 0)} -> output {f['output']}; "
                     f"contradicted {f.get('l2_contradicted', 0)}, unverified {f.get('unverified', 0)}")
        l1 = result["layers"]["l1"]
        lines.append(f"cost: {usd(result['cost_usd'])} of {usd(result['budget_usd'])}  "
                     f"(L1 {usd(l1.get('cost_usd'))}, {l1.get('requests', 0)} req; "
                     f"L2 {usd(l2.get('cost_usd'))}, {l2.get('requests', 0)} req)")
    for e in result.get("errors") or []:
        lines.append(f"error: {e}")
    lines.append(f"output: {result['output_dir']}")
    rows = (result.get("rows") or [])[:top_n]
    if rows:
        lines.append("")
        lines.append(_text_table(
            ["#", "security", "name", "mcap", "L1", "p_core", "L2", "read", "score"],
            [[str(r["rank"]), r["security_id"], (r["name"] or "")[:32], money(r["market_cap_usd"]),
              r["l1_label"] or "-", num(r["l1_p_core"]),
              " ".join([r["l2_label"] or "-"] + [m for m in _marks(r) if m in (USER_ONLY_ZH, BELOW_CUT_ZH,
                                                                               "递补，未经确认", "边缘", "仅简介",
                                                                               *FLAG_ZH.values())
                                                   or m.startswith(READ_NOTE_PREFIX)]),
              r.get("l2_evidence") or "-", num(r["score"])] for r in rows]))
    if result.get("unverified"):
        lines.append(f"unverified L1 passes (not ranked): {f.get('unverified', len(result['unverified']))}; "
                     "see report.md")
    if result.get("excluded_by_user"):
        lines.append(f"你排除的：{len(result['excluded_by_user'])} 家（不排名，证据见 report.md 校准）")
    band = (result["layers"].get("l2") or {}).get("band")
    if band and band.get("items"):
        n_edge = sum(1 for r in (result.get("rows") or []) + (result.get("unverified") or []) if r.get("l2_edge"))
        if band.get("skipped"):         # say what was actually read: the skipped rows count fewer reads
            lines.append(f"多读：边界 {band.get('items', 0)} 家，计划多读 {band.get('planned', 0)} 次：成功 "
                         f"{band.get('ok', 0)}，跳过 {band.get('skipped', 0)}（预算或 Jev 不够：没读满的按已读的次数判断，"
                         f"表里标「只读了…」）（{usd(band.get('cost_usd'))}），边缘 {n_edge} 家")
        else:
            lines.append(f"多读：边界 {band.get('items', 0)} 家各多读 {len(band.get('reads') or [])} 次"
                         f"（{usd(band.get('cost_usd'))}），边缘 {n_edge} 家")
    if not result.get("dry_run") and (result.get("rows") or result.get("unverified")):
        lines.append(f"{WHY_HINT_ZH} / {WHY_HINT_EN}")
    if result.get("gray_private"):
        lines.append("")
        lines.append("note: derived from gray-private text; personal use only.")
    return "\n".join(lines)
