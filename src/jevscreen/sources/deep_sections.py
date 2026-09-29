"""Deeper sections of CJK annual reports (CNINFO / EDINET / DART), used ONLY by an on-demand fetch.

The bulk syncs store a short business section: median ~4k characters for China / Japan / Korea against ~45k for a
US 10-K Item 1, so layer 2 often has no paragraph that names the idea's product. On demand (the top-N fetch of
jevscreen.topn_fetch, or `sync-* --deep` style calls with explicit codes) the adapters append, after the business
section they already extract, a few bounded blocks of the same filing that state products and segments:

- CNINFO full report (cninfo_appendix): 【营业收入构成】 the revenue-composition tables of 主营业务分析 (分行业 / 分产品
  / 分地区, and the 10%-or-more table that follows), 【核心竞争力分析】, and 【管理层讨论与分析（节选）】 the opening of
  the MD&A chapter. Forward-looking 未来发展的展望 is left out on purpose (plans are 'edge' in the label policy).
- EDINET type=5 CSV (edinet_appendix): 【経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析】 (MD&A, which
  holds 生産、受注及び販売の実績) and 【セグメント情報】 (segment note), matched by the element's local name so the
  taxonomy prefix does not matter.
- DART web viewer (dart_deep_nodes): 【매출 및 수주상황】 and 【이사의 경영진단 및 분석의견】 (the MD&A chapter), each one
  more viewer.do page of the same report.

Rules: facts only (text of the filing itself, never a summary or an inference), each block capped (BLOCK_CAPS) and
the appendix as a whole capped (DEEP_MAX_CHARS), paragraphs already present in the business section are not
repeated, and very short table-cell paragraphs are packed into lines long enough for the excerpt picker
(screen.split_paragraphs drops paragraphs under 40 CJK characters). The adapters record the deep extraction with
their own DEEP_* extractor version.

Isolation (the levers are off by default and a baseline must stay comparable): a deep text is its OWN documents
row, section DEEP_SECTION ('business_deep': its own doc_id '<source>:<cik>:<accession>:business_deep' and its own
text file '<accession>-business_deep.txt'), next to the shallow row, which is never touched. screen.load_documents
drops DEEP_SECTION rows unless the company is in the current deep view (deep_view(): the top-N companies of a
top-N fetch and its update pass only), so a run without the levers reads exactly what it read before. A bulk sync
never reads, settles on or replaces a deep row; coverage, the keyword background counts and the open-data pack
leave them out too.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

DEEP_SECTION = "business_deep"
_VIEW: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar("jevscreen_deep_view", default=None)


@contextlib.contextmanager
def deep_view(company_keys: Iterable[str]) -> Iterator[frozenset[str]]:
    """Within the block, screen.load_documents reads the deep text (DEEP_SECTION row) of these companies instead of
    their shallow text of the same report. Outside any deep_view no deep row is ever read."""
    keys = frozenset(str(k) for k in company_keys if k)
    token = _VIEW.set(keys | (_VIEW.get() or frozenset()))
    try:
        yield keys
    finally:
        _VIEW.reset(token)


def current_view() -> frozenset[str]:
    return _VIEW.get() or frozenset()


def is_deep(doc: Mapping[str, Any] | None) -> bool:
    return bool(doc) and doc.get("section") == DEEP_SECTION


def view_candidates(company_key: str, cands: list[dict[str, Any]], view: Iterable[str] | None = None
                    ) -> list[dict[str, Any]]:
    """The document candidates of one company as the current deep view sees them: outside the view every
    DEEP_SECTION row is dropped; inside it a deep row replaces the shallow row of the same report (source,
    accession)."""
    deep = [d for d in cands if is_deep(d)]
    if not deep:
        return cands
    v = current_view() if view is None else frozenset(view)
    if company_key not in v:
        return [d for d in cands if not is_deep(d)]
    twins = {report_key(d) for d in deep}
    return [d for d in cands if is_deep(d) or report_key(d) not in twins]


def report_key(doc: Mapping[str, Any]) -> str:
    """'<source>:<cik>:<accession>' of a documents row (its doc_id without the section)."""
    return str(doc.get("doc_id") or "").rsplit(":", 1)[0]


def deep_doc_id(shallow_doc_id: str) -> str:
    return shallow_doc_id.rsplit(":", 1)[0] + ":" + DEEP_SECTION

DEEP_MAX_CHARS = 20_000          # the whole appendix (on top of the business section)
BLOCK_CAPS = {"revenue": 5_000, "core": 5_000, "mda": 8_000,          # CNINFO
              "ja_mda": 10_000, "ja_segment": 6_000,                   # EDINET
              "ko_sales": 6_000, "ko_mda": 10_000}                    # DART
HEADINGS = {"revenue": "【营业收入构成】", "core": "【核心竞争力分析】", "mda": "【管理层讨论与分析（节选）】",
            "ja_mda": "【経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析】", "ja_segment": "【セグメント情報】",
            "ko_sales": "【매출 및 수주상황】", "ko_mda": "【이사의 경영진단 및 분석의견】"}
PACK_MIN_CHARS = 40              # screen.EXCERPT_MIN_PARA_CJK: shorter paragraphs never reach layer 2
PACK_MAX_CHARS = 400
REVENUE_MAX_LINES = 160

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub("", s or "")


def clip(text: str, limit: int) -> str:
    """text cut to at most `limit` chars at a paragraph end, else a sentence end, else hard."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    pos = cut.rfind("\n")
    if pos >= limit * 0.5:
        return cut[:pos].rstrip()
    pos = max(cut.rfind(ch) for ch in "。；！？.")
    return (cut[:pos + 1] if pos >= limit * 0.5 else cut).rstrip()


def pack_short(text: str, min_len: int = PACK_MIN_CHARS, max_len: int = PACK_MAX_CHARS) -> str:
    """Join runs of short paragraphs (table cells and rows one per line) with ' | ' into lines of at most max_len
    chars, so a revenue table survives the excerpt picker's minimum paragraph length. Long paragraphs stay."""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append(" | ".join(buf))
            buf.clear()
    for p in (x.strip() for x in text.split("\n")):
        if not p:
            continue
        if len(p) >= min_len:
            flush()
            out.append(p)
            continue
        if buf and len(" | ".join(buf)) + 3 + len(p) > max_len:
            flush()
        buf.append(p)
    flush()
    return "\n".join(out)


def _new_paragraphs(block: str, base: str) -> str:
    """The block's paragraphs that the base text does not already contain (whitespace ignored)."""
    seen = _norm(base)
    keep = [p for p in block.split("\n") if p.strip() and not (len(_norm(p)) >= 4 and _norm(p) in seen)]
    return "\n".join(keep).strip()


def assemble(base: str, blocks: Iterable[tuple[str, str | None]], *, max_chars: int = DEEP_MAX_CHARS
             ) -> tuple[str, list[str]]:
    """(appendix text to add after `base`, names of the blocks kept). blocks: (name, text) in priority order; each
    text is de-duplicated against base and the blocks before it, capped at BLOCK_CAPS[name], and the appendix
    stops at max_chars. A block with no new content (fewer than 20 chars) is dropped."""
    out: list[str] = []
    names: list[str] = []
    so_far = base
    used = 0
    for name, text in blocks:
        if not text:
            continue
        fresh = _new_paragraphs(text, so_far)
        fresh = clip(fresh, BLOCK_CAPS.get(name, 5_000))
        if len(_norm(fresh)) < 20:
            continue
        piece = f"{HEADINGS.get(name, name)}\n{fresh}"
        room = max_chars - used
        if room < 200:
            break
        if len(piece) > room:
            piece = clip(piece, room)
        out.append(piece)
        names.append(name)
        used += len(piece) + 2
        so_far += "\n" + fresh
    return ("\n\n" + "\n\n".join(out)) if out else "", names


# ---------------------------------------------------------------------------------------------------------------
# CNINFO (cleaned PDF lines of a full annual report; the helpers of sources/cninfo.py are passed in or imported
# lazily, so this module never imports an adapter at load time)

_REV_START = re.compile(r"营业收入(?:的)?构成(?:情况)?$")
_REV_TOP = re.compile(r"^占公司营业收入或营业利润\s*10\s*[%％]以上")
_CORE_START = re.compile(r"^(?:报告期内)?(?:公司)?核心竞争力(?:分析)?$")


def revenue_block(lines: Sequence[str], parse_heading: Callable[[str], dict | None]) -> str | None:
    """The revenue-composition tables: from the '营业收入构成' heading (a heading line, not a TOC row) to the next
    heading that is not the '占公司营业收入或营业利润10%以上…' table (a new '(n)' item, 'n、', '一、' or '第X节'), at most
    REVENUE_MAX_LINES lines; short cell lines packed (pack_short)."""
    for i, ln in enumerate(lines):
        h = parse_heading(ln)
        if h is None or h["level"] == "part" or not _REV_START.search(h["title"]):
            continue
        body: list[str] = []
        for ln2 in lines[i + 1:i + 1 + REVENUE_MAX_LINES]:
            h2 = parse_heading(ln2)
            if h2 is not None and h2["level"] in ("part", "cn", "ar", "sub", "paren") \
                    and not _REV_TOP.match(h2["title"]):
                break
            body.append(ln2)
        text = pack_short("\n".join(body))
        if len(_norm(text)) >= 20:
            return text
    return None


# the MD&A block stops where the revenue analysis (its own block) or the forward-looking outlook (left out) begins
_MDA_STOP = re.compile(r"^(?:[一二三四五六七八九十]{1,3}[、.．]\s*|\d{1,2}(?:\.\d{1,2})?[、.．]?\s*)?"
                       r"(?:主营业务分析|(?:公司)?未来发展的?展望|(?:公司)?关于公司未来发展的讨论与分析|经营计划)")


def mda_head(text: str) -> str:
    """The MD&A opening up to (not including) the revenue analysis or the outlook heading."""
    out = []
    for p in text.split("\n"):
        if _MDA_STOP.match(_norm(p)) and len(_norm(p)) <= 30:
            break
        out.append(p)
    return "\n".join(out)


def cninfo_appendix(lines: Sequence[str], layout: Any, base: str) -> tuple[str, str]:
    """(appendix, note) for a cleaned CNINFO report: revenue tables, core competence, MD&A opening (see the module
    docstring). note: 'deep:<names>' or 'deep:none'."""
    from . import cninfo
    blocks: list[tuple[str, str | None]] = [("revenue", revenue_block(lines, cninfo.parse_heading))]
    core = None
    for _fam, text, _end, toc in cninfo._family_sections(lines, [("core", _CORE_START)], layout):
        if text and not toc and len(text) >= 80:
            core = text
            break
    blocks.append(("core", core))
    mda, _mnote = cninfo.extract_mda_opening(lines, layout)
    blocks.append(("mda", mda_head(mda) if mda else None))
    app, names = assemble(base, blocks)
    return app, "deep:" + (",".join(names) or "none")


# ---------------------------------------------------------------------------------------------------------------
# EDINET (rows of the type=5 CSVs)

JA_MDA_SUFFIX = "ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock"
JA_SEGMENT_RX = re.compile(r"SegmentInformation\w*ConsolidatedFinancialStatements\w*TextBlock$|"
                           r"SegmentInformation\w*TextBlock$")


def _local(element: str) -> str:
    return element.strip().strip('"').lstrip("﻿").rsplit(":", 1)[-1]


def element_like(rows: Sequence[Sequence[str]], match: Callable[[str], bool]) -> str | None:
    """The longest value of any element whose local name (prefix dropped) satisfies `match`; the columns as in
    edinet.find_element_value (要素ID / 値 header, else first / last column)."""
    if not rows:
        return None
    header = [c.strip().strip('"').lstrip("﻿") for c in rows[0]]
    id_col = next((i for i, c in enumerate(header) if c in ("要素ID", "要素id", "ElementID", "Element ID")), None)
    val_col = next((i for i, c in enumerate(header) if c in ("値", "Value")), None)
    body = rows[1:] if id_col is not None or val_col is not None else rows
    id_col = 0 if id_col is None else id_col
    hits = []
    for row in body:
        if len(row) <= id_col or not match(_local(row[id_col])):
            continue
        vc = val_col if val_col is not None and val_col < len(row) else len(row) - 1
        hits.append(row[vc])
    return max(hits, key=len) if hits else None


def edinet_appendix(row_sets: Sequence[Sequence[Sequence[str]]], base: str,
                    to_text: Callable[[str], str]) -> tuple[str, str]:
    """(appendix, note) from every parsed CSV of the type=5 ZIP (first hit wins, in the given order): MD&A, then
    the segment note. to_text: edinet.text_block_to_text."""
    def first(match: Callable[[str], bool]) -> str | None:
        for rows in row_sets:
            v = element_like(rows, match)
            if v is not None:
                return v
        return None
    mda = first(lambda e: e.endswith(JA_MDA_SUFFIX))
    seg = first(lambda e: bool(JA_SEGMENT_RX.search(e)))
    blocks = [("ja_mda", to_text(mda) if mda else None),
              ("ja_segment", pack_short(to_text(seg)) if seg else None)]
    app, names = assemble(base, blocks)
    return app, "deep:" + (",".join(names) or "none")


# ---------------------------------------------------------------------------------------------------------------
# DART (TOC nodes of main.do)

def dart_deep_nodes(nodes: Sequence[Mapping[str, Any]], title_key: Callable[[str | None], str],
                    has_range: Callable[[Mapping[str, Any]], bool]) -> list[tuple[str, Mapping[str, Any]]]:
    """[(block name, TOC node)] of the deep blocks present with a usable range, in order: '매출 및 수주상황' (also
    '매출에 관한 사항'), then the MD&A chapter '이사의 경영진단 및 분석의견'. First match of each."""
    out: list[tuple[str, Mapping[str, Any]]] = []
    for name, pred in (("ko_sales", lambda k: k.startswith("매출및수주") or k.startswith("매출에관한사항")),
                       ("ko_mda", lambda k: k.startswith("이사의경영진단"))):
        node = next((n for n in nodes if pred(title_key(n.get("text"))) and has_range(n)), None)
        if node is not None:
            out.append((name, node))
    return out


def dart_appendix(base: str, texts: Sequence[tuple[str, str]]) -> tuple[str, str]:
    """(appendix, note) from the fetched deep section texts [(block name, text)]."""
    blocks = [(n, pack_short(t) if n == "ko_sales" else t) for n, t in texts]
    app, names = assemble(base, blocks)
    return app, "deep:" + (",".join(names) or "none")
