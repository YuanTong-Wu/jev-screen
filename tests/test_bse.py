"""Tests for sources.bse. No network: clients are fakes; PDFs are synthetic (built with PyMuPDF in the test); scrip
lists and annual report listings are hand-made with invented companies (no BSE / NSE page dumps)."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import cli, guard, provenance, store  # noqa: E402
from jevscreen.config import Config  # noqa: E402
from jevscreen.http import Blocked, Halted, Response  # noqa: E402
from jevscreen.sources import bse  # noqa: E402

try:
    import fitz  # noqa: F401
    HAVE_FITZ = True
except ImportError:   # pragma: no cover
    HAVE_FITZ = False

# ---- hand-made data (invented companies) ------------------------------------------------------------------

SCRIPS = [
    {"SCRIP_CD": "500001", "Scrip_Name": "Alpha Pumps Ltd", "Status": "Active", "GROUP": "A",
     "ISIN_NUMBER": "INE000A01011", "scrip_id": "ALPHAPUMP", "Segment": "Equity", "Issuer_Name": "Alpha Pumps Limited"},
    {"SCRIP_CD": "500002", "Scrip_Name": "Beta Foods Ltd", "Status": "Active", "GROUP": "B",
     "ISIN_NUMBER": "INE000B01012", "scrip_id": "BETAFOOD", "Segment": "Equity", "Issuer_Name": "Beta Foods Limited"},
    {"SCRIP_CD": "700003", "Scrip_Name": "Beta Foods Ltd", "Status": "Active", "GROUP": "P",
     "ISIN_NUMBER": "INE000B04013", "scrip_id": "BETAPREF", "Segment": "PreferenceShares", "Issuer_Name": "Beta"},
    {"SCRIP_CD": "500004", "Scrip_Name": "Gamma Textiles Ltd", "Status": "Active", "GROUP": "X",
     "ISIN_NUMBER": "INE000C01014", "scrip_id": "GAMMATEX", "Segment": "Equity", "Issuer_Name": "Gamma Textiles"},
]
SCRIPS_RAW = json.dumps(SCRIPS).encode()


def listing(*rows: dict) -> bytes:
    return json.dumps({"Table": list(rows)}).encode()


def ar_row(code: str, year: str, url: str, *, status: str = "New", auth: str | None = "2026-08-01T10:00:00",
           revised: str | None = None) -> dict:
    return {"Scripcode": code, "Year": year, "PDFDownload": url, "Flag": 0, "Fld_AuthoriseDate": auth,
            "Fld_ReSubmit": 1 if status == "Revised" else 0, "Fld_ResubReason": " ", "revised_date_time": revised,
            "status": status, "RN": 1}


URL_A = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/aaaaaaaa-0000-0000-0000-000000000001.pdf"
URL_A_OLD = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/aaaaaaaa-0000-0000-0000-000000000000.pdf"
URL_B = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/bbbbbbbb-0000-0000-0000-000000000001.pdf"

LOREM_A = ("Alpha Pumps Limited designs and manufactures industrial centrifugal pumps, valves and motors for water "
           "utilities, oil refineries and power plants. The Company operates four plants in Gujarat and "
           "Maharashtra and exports to thirty countries through a network of distributors. ")
LOREM_MACRO = ("The Indian economy grew at a healthy pace during the year, supported by public capital expenditure "
               "and resilient consumption. Inflation moderated and the central bank eased policy rates. ")
LOREM_B = ("Beta Foods Limited processes frozen vegetables and potato products and sells them to quick service "
           "restaurants and retail chains across India. The Company also trades mushrooms. ")


def sample_pages() -> list[list[str]]:
    """A small annual report: cover, contents, overview, directors' report pointing at the MD&A, MD&A (heading split
    over two blocks, running header, table numbers, business sub-heading after a macro opening), governance."""
    header = "Alpha Pumps Limited | Annual Report 2025-26"
    return [
        ["Alpha Pumps Limited", "34th Annual Report 2025-26"],
        ["Contents", "Notice 2", "Directors' Report 10", "Management Discussion and Analysis 20",
         "Report on Corporate Governance 30", "Independent Auditor's Report 40"],
        ["About the Company", LOREM_A * 2],
        [header, "Directors' Report", "1. Financial results", "Revenue grew by 12 per cent during the year.",
         "Management Discussion and Analysis Report",
         "The Management Discussion and Analysis Report forms part of this report as Annexure B.",
         LOREM_MACRO],
        [header, "Annexure B", "Management", "Discussion and Analysis", "Economic overview", LOREM_MACRO * 5,
         "1,234 5,678 9,012", "12"],
        [header, "Industry structure and developments", LOREM_MACRO * 4, "13"],
        [header, "Company Overview", LOREM_A * 2, "Opportunities and threats", LOREM_MACRO, "14"],
        [header, "Report on Corporate Governance", "The Company believes in good governance. " * 10, "15"],
    ]


# =========================================================================== mapping


class TestMapping(unittest.TestCase):
    def test_parse_and_map(self):
        rows = bse.parse_scrip_list(SCRIPS_RAW)
        self.assertEqual(len(rows), 4)
        secs = [
            {"security_id": "NSE:ALPHAPUMP", "exchange": "NSE", "symbol": "ALPHAPUMP", "isin": "INE000A01011"},
            {"security_id": "BSE:BETAFOOD", "exchange": "BSE", "symbol": "BETAFOOD", "isin": "INE999Z01019"},
            {"security_id": "NSE:DELTA", "exchange": "NSE", "symbol": "DELTA", "isin": "INE000D01010"},
            {"security_id": "BSE:500004", "exchange": "BSE", "symbol": "500004", "isin": None},
            {"security_id": "BSE:830799", "exchange": "BSE", "symbol": "830799", "isin": "CNE1000000Q1"},  # Beijing
            {"security_id": "NASDAQ:X", "exchange": "NASDAQ", "symbol": "X", "isin": "US0000000001"},
            {"security_id": "BSE:BETAPREF", "exchange": "BSE", "symbol": "BETAPREF", "isin": "INE000B04013"},
        ]
        rep: dict = {}
        out = bse.map_securities_to_scrip(secs, rows, report=rep)
        self.assertEqual(out, [("NSE:ALPHAPUMP", "500001", "isin"), ("BSE:BETAFOOD", "500002", "scrip_id"),
                               ("BSE:500004", "500004", "scrip_code")])
        self.assertEqual(rep["unmatched_by_exchange"], {"NSE": 1, "BSE": 1})   # NSE-only company, preference line

    def test_ambiguous_isin_not_mapped(self):
        rows = bse.parse_scrip_list(json.dumps(SCRIPS + [dict(SCRIPS[0], SCRIP_CD="500009", scrip_id="ALPHA2")]))
        rep: dict = {}
        out = bse.map_securities_to_scrip([{"security_id": "NSE:ALPHAPUMP", "exchange": "NSE",
                                            "isin": "INE000A01011"}], rows, report=rep)
        self.assertEqual((out, rep["ambiguous"]), ([], ["NSE:ALPHAPUMP"]))

    def test_bad_scrip_list(self):
        with self.assertRaises(ValueError):
            bse.parse_scrip_list(b'{"not": "a list"}')


# =========================================================================== listing


class TestListing(unittest.TestCase):
    def test_pdf_url(self):
        self.assertEqual(bse.pdf_url("https://www.bseindia.com/xml-data/corpfiling/AttachHis/\\b55b.pdf"),
                         "https://www.bseindia.com/xml-data/corpfiling/AttachHis/b55b.pdf")
        self.assertEqual(bse.pdf_url("http://www.bseindia.com/bseplus/AnnualReport/500001/1.pdf"),
                         "https://www.bseindia.com/bseplus/AnnualReport/500001/1.pdf")
        self.assertEqual(bse.pdf_url("/bseplus/AnnualReport/500001/1.pdf"),
                         "https://www.bseindia.com/bseplus/AnnualReport/500001/1.pdf")
        self.assertIsNone(bse.pdf_url("https://evil.example.com/x.pdf"))
        self.assertIsNone(bse.pdf_url(""))

    def test_select_latest_prefers_newest_year_then_revision(self):
        raw = listing(ar_row("500001", "2025", URL_A_OLD, auth="2025-08-01T00:00:00"),
                      ar_row("500001", "2026", URL_A_OLD.replace("000000000000", "0000000000aa"),
                             auth="2026-08-01T00:00:00"),
                      ar_row("500001", "2026", URL_A, status="Revised", auth=None, revised="2026-08-20T09:00:00"),
                      ar_row("500001", "20x6", URL_B), ar_row("500001", "2027", "https://elsewhere.test/a.pdf"))
        rows = bse.parse_ar_listing(raw)
        self.assertEqual(len(rows), 3)
        doc = bse.select_latest(rows)
        self.assertEqual((doc["year"], doc["url"], doc["revised"], doc["filing_date"]),
                         (2026, URL_A, True, dt.date(2026, 8, 20)))
        self.assertEqual(doc["accession"], "aaaaaaaa-0000-0000-0000-000000000001")

    def test_empty_and_bad_listing(self):
        self.assertEqual(bse.parse_ar_listing(b'{"Table": null}'), [])
        self.assertIsNone(bse.select_latest([]))
        with self.assertRaises(ValueError):
            bse.parse_ar_listing(b"[1, 2]")

    def test_labels_and_note(self):
        self.assertEqual(bse.newest_possible_label(dt.date(2026, 3, 31)), 2025)
        self.assertEqual(bse.newest_possible_label(dt.date(2026, 4, 1)), 2026)
        self.assertEqual(bse.expected_latest_label(dt.date(2026, 9, 30)), 2025)
        self.assertEqual(bse.expected_latest_label(dt.date(2026, 10, 1)), 2026)
        doc = {"year": 2024, "revised": True}
        self.assertEqual(bse.doc_note(doc, dt.date(2026, 10, 2)), "fy_label:2024;revision;stale:2024")

    def test_stale_needs_age_as_well_as_label(self):
        # a June year-end company files its FY label-2025 report in October 2025: not stale a year later, while its
        # next report is not yet due; a March company whose label-2025 report is 14 months old is stale
        june = {"year": 2025, "revised": False, "filing_date": dt.date(2025, 10, 20)}
        self.assertEqual(bse.doc_note(june, dt.date(2026, 10, 15)), "fy_label:2025")
        self.assertEqual(bse.doc_note(june, dt.date(2026, 11, 30)), "fy_label:2025;stale:2025")
        march = {"year": 2025, "revised": False, "filing_date": dt.date(2025, 8, 10)}
        self.assertEqual(bse.doc_note(march, dt.date(2026, 10, 15)), "fy_label:2025;stale:2025")
        self.assertEqual(bse.doc_note(march, dt.date(2026, 9, 15)), "fy_label:2025")


# =========================================================================== extraction


class TestExtractSection(unittest.TestCase):
    def test_heading_scan(self):
        pages = bse.Pages.from_lists(sample_pages())
        text, note = bse.extract_section(pages)
        self.assertEqual(note, f"overview:heading;mda:heading;mda_part:business_first;pages:{pages.read}/8")
        self.assertTrue(text.startswith("About the Company\n" + LOREM_A[:40]))   # overview first
        mda = text.split("\n\n", 1)[1]
        self.assertTrue(mda.startswith("Company Overview\n"))            # business sub-heading moved up
        self.assertIn("Economic overview", mda)                          # the opening is kept after it
        self.assertNotIn("Alpha Pumps Limited | Annual Report", text)    # running header dropped
        self.assertNotIn("1,234 5,678", text)                            # table numbers dropped
        self.assertNotIn("good governance", text)                        # ends at Corporate Governance
        self.assertNotIn("forms part of this report", text)              # the directors' report pointer is not MD&A

    def test_outline_is_used_and_verified(self):
        pages_ = sample_pages()
        toc = [[1, "About the Company", 3], [1, "Directors' Report", 4], [1, "MD&A", 5],
               [1, "Management Discussion and Analysis", 4], [1, "Report on Corporate Governance", 8]]
        # the outline says page 4 (1-based) but the heading is on page 5: found within +-2 pages
        pages = bse.Pages.from_lists(pages_, toc)
        text, note = bse.extract_section(pages)
        self.assertIn("overview:outline", note)
        self.assertIn("mda:outline", note)
        self.assertNotIn("good governance", text)

    def test_mda_opening_when_business_heading_is_early(self):
        pages = bse.Pages.from_lists([["Management Discussion & Analysis", "Business Overview", LOREM_B * 5]])
        text, note = bse.extract_section(pages)
        self.assertIn("mda_part:opening", note)
        self.assertTrue(text.startswith("Business Overview\n" + LOREM_B.strip()[:30]))

    def test_no_text_layer(self):
        text, note = bse.extract_section(bse.Pages.from_lists([["12"], [""], ["3"]]))
        self.assertIsNone(text)
        self.assertEqual(note, "no_text_layer;pages:3/3")

    def test_directors_report_fallback_and_no_section(self):
        body = "The Company is engaged in the business of " + LOREM_B * 3
        text, note = bse.extract_section(bse.Pages.from_lists([["Directors' Report", "2. State of the Company's "
                                                                "Affairs:", body]]))
        self.assertTrue(note.startswith("fallback:directors_report"))
        self.assertIn("frozen vegetables", text)
        text, note = bse.extract_section(bse.Pages.from_lists([
            ["Notes forming part of the financial statements", "Note No 1", "Company Information",
             "Beta Foods Limited was incorporated in 2001. The main objective of the company is processing of frozen "
             "vegetables and potato products for restaurants and retail chains in India.", "Summary of significant accounting policies", LOREM_MACRO * 3],
            ["Directors' Report", "State of the Company's affairs", "Revenue grew; the business did well. " * 5]]))
        self.assertTrue(note.startswith("fallback:corporate_information"))
        self.assertTrue(text.startswith("Beta Foods Limited was incorporated") and "accounting" not in text)
        text, note = bse.extract_section(bse.Pages.from_lists([["Notice", LOREM_MACRO * 5]]))
        self.assertIsNone(text)
        self.assertTrue(note.startswith("no_section"))

    def test_toc_page_is_not_the_mda(self):
        p = sample_pages()
        self.assertTrue(bse.is_toc_page(p[1][:bse.HEAD_BLOCKS], p[1]))
        self.assertFalse(bse.page_has_mda_heading(p[3]))   # pointer inside the directors' report
        self.assertFalse(bse.page_has_mda_heading([
            "Directors' Report", "Management Discussion and Analysis Report",
            "The report on Management Discussion and Analysis, forming part of this Annual Report, covers it."]))
        self.assertTrue(bse.page_has_mda_heading(p[4]))    # split heading after 'Annexure B'

    def test_directors_report_pointer_wordings(self):
        wordings = ("The Management Discussion and Analysis for the year under review is included in this Annual Report.",
                    "The Management Discussion and Analysis for the year is part of this Annual Report.",
                    "The Management Discussion and Analysis Report is enclosed with this report.",
                    "The Management Discussion and Analysis is furnished separately in this Annual Report.",
                    "Pursuant to Regulation 34(2)(e) of the Listing Regulations, the report is enclosed as Annexure C.",
                    "The Management Discussion and Analysis has been included in this Annual Report.",
                    "The Management Discussion and Analysis Report is disclosed separately.",
                    "A detailed review of operations is covered in the Management Discussion and Analysis section.",
                    "The Management Discussion and Analysis is incorporated in this Annual Report by reference.",
                    "The report, as per Annexure C, reviews the business.")
        filler = [["Cover page of Alpha Pumps Limited annual report with a lot of words " * 5]] * 3
        real = [["Annexure C", "Management Discussion and Analysis", "Industry structure and developments", LOREM_A],
                ["Opportunities and threats", LOREM_MACRO * 2],
                ["Report on Corporate Governance", "The Company believes in good governance. " * 10]]
        for w in wordings:
            head = ["Board's Report", "Management Discussion and Analysis Report", w]
            self.assertFalse(bse.page_has_mda_heading(head), w)
            dr = [["Directors' Report", "Dear Members, your Directors present the report. " * 5,
                   "Management Discussion and Analysis", w, "Other items of the directors' report follow here. " * 5]]
            pages = bse.Pages.from_lists(filler + dr + [["Directors' report text continues. " * 10]] * 2 + real)
            self.assertEqual(bse.find_mda(pages), (6, 8, "heading"), w)
        # a real chapter opening is still the MD&A
        self.assertTrue(bse.page_has_mda_heading(["Management Discussion and Analysis",
                                                  "Industry structure and developments", LOREM_MACRO]))
        self.assertTrue(bse.page_has_mda_heading(["Management Discussion and Analysis",
                                                  "Alpha Pumps is part of the engineering sector. " + LOREM_A]))

    def test_divider_page_is_not_the_mda(self):
        # a section divider listing three chapters is not a contents page (4+ names) but opens nothing: the next
        # heading page is tried when the first range has too little text
        divider = ["Statutory Reports", "Management Discussion and Analysis", "52", "Board's Report", "78",
                   "Report on Corporate Governance", "110"]
        board = ["Board's Report", "Dear Members, your Directors present the annual report. " * 10]
        mda = [["Management Discussion and Analysis", "Industry structure and developments", LOREM_A * 2],
               ["Opportunities and threats", LOREM_MACRO * 3]]
        pages = bse.Pages.from_lists([["Cover page " * 60], divider, board, board, board] + mda
                                     + [["Report on Corporate Governance", LOREM_MACRO]])
        self.assertEqual(bse.find_mda(pages), (5, 7, "heading"))
        text, note = bse.extract_section(pages)
        self.assertTrue(note.startswith("mda:heading;mda_part:opening"), note)
        self.assertIn("centrifugal pumps", text)
        # a heading page whose range holds too little text (here: a one-line teaser before another chapter): the next
        # heading page is tried
        teaser = ["Management Discussion and Analysis", "Industry structure and developments", "See page 52."]
        pages = bse.Pages.from_lists([["Cover page " * 60], teaser, ["Notice", "Notice is hereby given. " * 20],
                                      board] + mda + [["Report on Corporate Governance", LOREM_MACRO]])
        self.assertFalse(bse.is_divider_page(teaser))
        self.assertEqual(bse.find_mda(pages), (4, 6, "heading"))
        # without any better candidate the short range is still returned (and fails as too_short)
        pages = bse.Pages.from_lists([["Cover page " * 60], divider, board])
        self.assertEqual(bse.find_mda(pages), (1, 2, "heading"))
        self.assertTrue(bse.extract_section(pages)[1].startswith("too_short:"))

    def test_heading_drawn_last_but_at_the_top(self):
        # designed reports often put the page heading last in the content stream: with block positions the head is
        # the top of the page, not the first blocks
        body = [(LOREM_MACRO, 0.35 + 0.05 * k) for k in range(12)]
        mda_page = body + [("Management Discussion and Analysis", 0.04), ("312", 0.96)]
        ref_page = [(LOREM_MACRO, 0.1), ("Management Discussion and Analysis", 0.6),
                    ("Report on Management Discussion and Analysis as stipulated under the regulations.", 0.62)]
        pages = bse.Pages(2, lambda i: [ref_page, mda_page][i])
        self.assertEqual(pages.head(1)[-1], "Management Discussion and Analysis")   # 13th block, but at the top
        self.assertNotIn("312", pages.head(1))
        self.assertFalse(bse.page_has_mda_heading(pages.head(0)))
        self.assertEqual(bse.find_mda(pages)[:2], (1, 2))
        self.assertEqual(bse.before_heading(pages, 1), set())       # nothing above it: the whole page is MD&A

    def test_heading_mid_page_drops_the_previous_chapter(self):
        page = [("Capital adequacy ratio", 0.1), ("The Bank's capital ratio stood well above the minimum.", 0.2),
                ("MANAGEMENT DISCUSSION AND ANALYSIS", 0.7), ("BUSINESS REVIEW", 0.73),
                ("Your Bank's operations are split into Domestic and International business. " * 4, 0.76),
                ("Retail banking serves individuals with deposits, loans and cards. " * 4, 0.08)]   # 2nd column
        pages = bse.Pages(1, lambda i: page)
        self.assertEqual(bse.before_heading(pages, 0), {0, 1})
        text, note = bse.extract_section(pages)
        self.assertNotIn("capital ratio", text)
        self.assertTrue(text.startswith("BUSINESS REVIEW") and "Retail banking" in text)

    def test_running_tabs_do_not_end_the_mda(self):
        # Indian reports print section tabs on every page; a tab that looks like an end heading ('Financial
        # Statements', "Board's Report", 'Annexure B') must not end the MD&A on its second page
        filler = [["Cover page of Alpha Pumps Limited annual report with a lot of words " * 5]] * 3

        def mda_pages(tab: str) -> list[list[str]]:
            return [[tab, "Management Discussion and Analysis", "Industry structure and developments", LOREM_A],
                    [tab, "Opportunities and threats", LOREM_MACRO * 2],
                    [tab, "Segment-wise performance", LOREM_A],
                    [tab, "Outlook", LOREM_MACRO * 2],
                    [tab, "Report on Corporate Governance", "The Company believes in good governance. " * 10]]
        for tab in ("Financial Statements", "Board's Report", "Directors' Report", "Annexure B", "Statutory Reports"):
            pages = bse.Pages.from_lists(filler + mda_pages(tab))
            self.assertEqual(bse.find_mda(pages), (3, 7, "heading"), tab)
        # a navigation strip drawn last as separate blocks at the top of every page (PyMuPDF positions)
        strip = [("Corporate Overview", 0.02), ("Statutory Reports", 0.02), ("Financial Statements", 0.02)]
        body = [(LOREM_MACRO, 0.3 + 0.05 * k) for k in range(12)]
        raw = ([[(LOREM_A * 2, 0.3)]] * 3
               + [body[:1] + [("Management Discussion and Analysis", 0.1)] + body[1:] + strip]
               + [body + strip] * 4
               + [[("Report on Corporate Governance", 0.1)] + body + strip])
        pages = bse.Pages(len(raw), lambda i: raw[i])
        self.assertEqual(bse.find_mda(pages), (3, 8, "heading"))

    def test_overview_page_choice(self):
        board = ["Board's Report", "Dear Members, your Directors present the annual report. " * 10]
        mda = [["Management Discussion and Analysis", "Industry structure and developments", LOREM_MACRO * 5]]
        about = ["About Us", LOREM_A * 2]
        # (a) a numbers-only 'at a glance' page does not hide the later About Us page
        glance = ["Company at a Glance", "12%", "4,500 crore", "Revenue", "Plants", "4"]
        text, note = bse.extract_section(bse.Pages.from_lists([["Cover page " * 60], glance, about, board] + mda))
        self.assertTrue(note.startswith("overview:heading;mda:heading"), note)
        self.assertTrue(text.startswith("About Us\n" + LOREM_A[:30]), text[:80])
        # (b) pages about the report, the chairman, ESG or the year are not the company overview
        report_pages = (["About Integrated Reporting", "This report follows the IIRC framework and covers the "
                         "reporting period and boundary of the report. " * 3],
                        ["About the Chairman", "Mr. A. Kumar has led the board since 2010 and chairs its committees. " * 4],
                        ["ESG at a Glance", "Our emissions intensity fell and water recycling rose during the year. " * 4],
                        ["Sustainability at a glance", "Renewable power now meets a third of our needs. " * 5],
                        ["The Year at a Glance", "A year of record orders, new plants and higher exports. " * 5])
        for page in report_pages:
            text, note = bse.extract_section(bse.Pages.from_lists([["Cover page " * 60], page, about, board] + mda))
            self.assertTrue(text.startswith("About Us\n"), (page[0], text[:60]))
            self.assertIn("overview:heading", note)
        # (c) without an MD&A, a directors' report 'Business Overview' item is not a front-matter overview page: the
        # 'Corporate information' note is used
        dr = ["Directors' Report", "Dear Members, your Directors present the annual report.", "Business Overview",
              "During the year the Company declared a dividend, accepted no deposits and re-appointed its auditors. " * 3]
        notes = ["Notes to the financial statements", "Note 1", "Corporate Information",
                 "Beta Foods Limited is engaged in processing frozen vegetables and potato products for restaurants and "
                 "retail chains in India."]
        text, note = bse.extract_section(bse.Pages.from_lists([["Cover page " * 60], dr, notes]))
        self.assertTrue(note.startswith("fallback:corporate_information"), note)
        self.assertNotIn("dividend", text)

    def test_normalise_block(self):
        self.assertEqual(bse.normalise_block("end-to-\nend value-\nadded manufac-\nturing  of\tpumps"),
                         "end-to-end value-added manufacturing of pumps")

    def test_short_description(self):
        s = bse.short_description(("A sentence here. " * 200).strip())
        self.assertTrue(s.endswith(".") and len(s) <= bse.SHORT_DESC_CHARS)


def make_pdf(pages: list[list[str]], toc: list | None = None) -> bytes:
    doc = fitz.open()
    for blocks in pages:
        page = doc.new_page(width=595, height=842)
        y = 40.0
        for b in blocks:
            h = 14.0 * (1 + len(b) // 90) + 4
            page.insert_textbox(fitz.Rect(40, y, 555, y + h), b, fontsize=9)
            y += h + 16
    if toc:
        doc.set_toc(toc)
    data = doc.tobytes()
    doc.close()
    return data


@unittest.skipUnless(HAVE_FITZ, "PyMuPDF not installed")
class TestExtractFromPdf(unittest.TestCase):
    def test_synthetic_pdf(self):
        data = make_pdf(sample_pages())
        text, note, backend = bse.extract_from_pdf(data)
        self.assertEqual(backend, "pymupdf")
        self.assertIn("mda:heading", note)
        self.assertIn("mda_part:business_first", note)
        self.assertIn("centrifugal pumps", text)
        self.assertNotIn("good governance", text)

    def test_synthetic_pdf_outline_reads_few_pages(self):
        pages = [["Cover page of the report with enough words to count as a text layer. " * 4]] * 30
        pages = pages + [["Management Discussion and Analysis", LOREM_A * 3],
                         ["Report on Corporate Governance", LOREM_MACRO]]
        data = make_pdf(pages, toc=[[1, "Management Discussion and Analysis", 31], [1, "Governance", 32]])
        text, note, _ = bse.extract_from_pdf(data)
        self.assertIn("mda:outline", note)
        read = int(note.rsplit("pages:", 1)[1].split("/")[0])
        self.assertLess(read, 32)        # only the pages needed were parsed
        self.assertIn("centrifugal pumps", text)

    def test_not_a_pdf(self):
        text, note, backend = bse.extract_from_pdf(b"%PDF-1.4 garbage")
        self.assertIsNone(text)


# =========================================================================== fetching


class FakeClient:
    """Stands in for http.Client: GET url -> bytes | (status, bytes, headers) | Response | Exception. Once halt is set
    (a Blocked raised here or by the adapter) a call raises Halted and is not recorded, as http.Client does."""
    thread_safe = True

    def __init__(self, gets: dict):
        self.gets = dict(gets)
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.min_interval_s, self.requests_made = 1.0, 0
        self.halt = threading.Event()
        self._lock = threading.Lock()

    def get(self, url: str, **kw) -> Response:
        if self.halt.is_set():             # like http.Client: after a block nothing more is sent
            raise Halted(url)
        with self._lock:
            self.calls.append(url)
            self.kwargs.append(kw)
            self.requests_made += 1
        item = self.gets.get(url, 404)
        if isinstance(item, BaseException):
            if isinstance(item, Blocked):
                self.halt.set()
            raise item
        if isinstance(item, Response):
            return item
        if isinstance(item, int):
            return Response(url, item, b"", {}, 0.0)
        if isinstance(item, tuple):
            return Response(url, item[0], item[1], item[2] if len(item) > 2 else {}, 0.0)
        return Response(url, 200, item, {"Content-Type": "application/pdf" if item[:4] == b"%PDF" else "json"}, 0.0)


def list_url(code: str) -> str:
    return bse.AR_LIST_URL.format(code=code)


@unittest.skipUnless(HAVE_FITZ, "PyMuPDF not installed")
class TestFetchCompany(unittest.TestCase):
    def test_ok(self):
        c = FakeClient({list_url("500001"): listing(ar_row("500001", "2026", URL_A)), URL_A: make_pdf(sample_pages())})
        res = bse.fetch_company(c, "500001", today=dt.date(2026, 9, 27))
        self.assertEqual((res.status, res.requests), ("ok", 2))
        self.assertTrue(res.extract_note.endswith(";fy_label:2026"))
        self.assertEqual(c.kwargs[0]["headers"]["Referer"], "https://www.bseindia.com/")
        self.assertEqual(c.kwargs[1]["max_bytes"], bse.MAX_PDF_BYTES)

    def test_listing_redirect_is_a_block(self):
        c = FakeClient({list_url("500001"): (301, b"", {"Location": "https://www.bseindia.com/members/x.aspx"})})
        with self.assertRaises(Blocked) as cm:
            bse.fetch_company(c, "500001")
        self.assertEqual((cm.exception.status, cm.exception.reason), (301, "redirect_refusal"))
        self.assertTrue(c.halt.is_set())

    def test_listing_html_page_is_a_block(self):
        for body, ctype in ((b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>Reference #18.x</BODY></HTML>",
                             "text/html"), (b"  <!DOCTYPE html><html>maintenance</html>", "")):
            c = FakeClient({list_url("500001"): (200, body, {"Content-Type": ctype})})
            with self.assertRaises(Blocked) as cm:
                bse.fetch_company(c, "500001")
            self.assertEqual((cm.exception.status, cm.exception.reason), (200, "non_json_page"))
            self.assertTrue(c.halt.is_set())
        c = FakeClient({list_url("500001"): (200, b'{"Table": [', {"Content-Type": "application/json"})})
        res = bse.fetch_company(c, "500001")                   # broken JSON (not a page): an ordinary error
        self.assertEqual((res.status, res.note), ("error", "list_bad_json:JSONDecodeError"))

    def test_pdf_html_page_is_a_block_unless_the_site_home_page(self):
        deny = b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>You don't have permission.</BODY></HTML>"
        for body in (deny, b"<!DOCTYPE html><html>home</html>"):
            c = FakeClient({list_url("500001"): listing(ar_row("500001", "2026", URL_A)),
                            URL_A: (200, body, {"Content-Type": "text/html"})})
            with self.assertRaises(Blocked) as cm:
                bse.fetch_company(c, "500001")
            self.assertEqual((cm.exception.url, cm.exception.status, cm.exception.reason), (URL_A, 200, "non_pdf_body"))
            self.assertTrue(c.halt.is_set())
        home = b"<!DOCTYPE html><html><head><title>BSE Ltd. | Live Stock Market</title></head><app-root></app-root>"
        c = FakeClient({list_url("500001"): listing(ar_row("500001", "2026", URL_A)),
                        URL_A: (200, home, {"Content-Type": "text/html"})})
        res = bse.fetch_company(c, "500001")                   # the site's home page for a dead link: an error
        self.assertEqual((res.status, res.note), ("error", "pdf_html_page"))
        self.assertFalse(c.halt.is_set())
        self.assertFalse(bse.is_site_home_page(b"<title>BSE - Access Denied</title>"))
        self.assertFalse(bse.is_site_home_page(b"<title>BSE</title><body>Access Denied. Reference #18.2f</body>"))
        self.assertFalse(bse.is_site_home_page(b"<title>Stock market</title>"))       # not BSE's own title
        self.assertTrue(bse.is_site_home_page(b"<title>BSE Ltd.</title><a>Bulk / Block Deals</a> securities"))

    def test_too_large(self):
        big = Response(URL_A, 200, b"%PDF-1.7 ...", {"Content-Type": "application/pdf"}, 0.0, truncated=True)
        c = FakeClient({list_url("500001"): listing(ar_row("500001", "2026", URL_A)), URL_A: big})
        res = bse.fetch_company(c, "500001")
        self.assertEqual(res.status, "extract_failed")
        self.assertTrue(res.extract_note.startswith("too_large:"))

    def test_no_report_and_blocked(self):
        c = FakeClient({list_url("500001"): listing()})
        self.assertEqual(bse.fetch_company(c, "500001").status, "no_annual_report")
        c = FakeClient({list_url("500001"): listing(ar_row("500001", "2026", URL_A)),
                        URL_A: Blocked(URL_A, 403, "http_403")})
        with self.assertRaises(Blocked):
            bse.fetch_company(c, "500001")


# =========================================================================== sync


class SyncBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), min_interval_s=1.0)
        with store.session(self.cfg) as con:
            self.snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="test", request=None,
                                              raw_path=None, raw_sha256=None, raw_bytes=None, rows=None,
                                              duration_s=None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add(self, security_id: str, cap: float | None, isin: str | None) -> None:
        exchange, symbol = security_id.split(":", 1)
        row = dict(security_id=security_id, exchange=exchange, symbol=symbol, name=symbol, isin=isin, country="India",
                   tv_type="stock", tv_subtype="common", is_primary=True, price_currency=None,
                   fundamental_currency=None, sector=None, industry=None,
                   company_key=store.company_key(isin, security_id), first_seen_snapshot=self.snap,
                   last_seen_snapshot=self.snap, last_seen_at=store.now_utc(), active=True)
        with store.session(self.cfg) as con:
            store.upsert_many(con, "securities", list(row), [list(row.values())])
            store.upsert_many(con, "market_daily", ["security_id", "as_of", "market_cap_usd", "snapshot_id"],
                              [[security_id, dt.date(2026, 9, 26), cap, self.snap]])

    def add_standard(self) -> None:
        self.add("NSE:ALPHAPUMP", 5e9, "INE000A01011")
        self.add("BSE:BETAFOOD", 1e8, "INE000B01012")
        self.add("NSE:DELTA", 3e9, "INE000D01010")          # NSE only: unmapped
        self.add("NASDAQ:NICE", 9e9, "US6536561086")        # never considered

    def client(self, **over) -> FakeClient:
        gets = {bse.SCRIP_LIST_URL: SCRIPS_RAW,
                list_url("500001"): listing(ar_row("500001", "2026", URL_A)),
                list_url("500002"): listing(ar_row("500002", "2026", URL_B)),
                URL_A: make_pdf(sample_pages()),
                URL_B: make_pdf([["Management Discussion and Analysis", LOREM_B * 6]])}
        gets.update(over)
        return FakeClient(gets)

    def q(self, sql: str, params=()) -> list[tuple]:
        with store.session(self.cfg, read_only=True, wait_s=5) as con:
            return con.execute(sql, list(params)).fetchall()

    def run_sync(self, client, **kw) -> dict:
        kw.setdefault("min_scrip_rows", 1)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return bse.sync(self.cfg, client, **kw)


@unittest.skipUnless(HAVE_FITZ, "PyMuPDF not installed")
class TestSync(SyncBase):
    def test_full_run_then_current_skip(self):
        self.add_standard()
        c = self.client()
        s = self.run_sync(c, workers=2)
        self.assertEqual(s["status"], "ok", s)
        self.assertEqual((s["securities_mapped"], s["queued"], s["unmatched"]), (2, 2, 1))
        self.assertEqual(c.calls[:2], [bse.SCRIP_LIST_URL, list_url("500001")])      # market cap order
        self.assertEqual(sorted(c.calls), sorted([bse.SCRIP_LIST_URL, list_url("500001"), list_url("500002"),
                                                  URL_A, URL_B]))
        ids = dict(self.q("SELECT security_id, id_value FROM identifiers WHERE id_type = 'bse_scrip_code'"))
        self.assertEqual(ids, {"NSE:ALPHAPUMP": "500001", "BSE:BETAFOOD": "500002"})
        docs = self.q("SELECT doc_id, cik, form, text_path, extractor, extract_note, report_date FROM documents "
                      "WHERE source_id = ? ORDER BY cik", [bse.SOURCE_ID])
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0][0], f"{bse.SOURCE_ID}:500001:aaaaaaaa-0000-0000-0000-000000000001:business")
        self.assertEqual(docs[0][4], f"{bse.EXTRACTOR_VERSION}/pymupdf")
        self.assertIn("fy_label:2026", docs[0][5])
        self.assertIsNone(docs[0][6])                       # the fiscal year end is unknown: stays NULL
        self.assertIn("centrifugal pumps", Path(docs[0][3]).read_text())
        states = dict(self.q("SELECT security_id, status FROM crawl_state WHERE source_id = ?", [bse.SOURCE_ID]))
        self.assertEqual(states, {"NSE:ALPHAPUMP": "ok", "BSE:BETAFOOD": "ok"})
        lang = self.q("SELECT DISTINCT lang FROM descriptions WHERE source_id = ?", [bse.SOURCE_ID])
        self.assertEqual(lang, [("en",)])
        self.assertEqual(self.q("SELECT status, command FROM runs WHERE command = 'sync-bse'"), [("ok", "sync-bse")])
        # second run: both companies hold the newest possible year -> no listing or PDF request, and the scrip
        # list saved by the first run is reused
        c2 = self.client()
        s2 = self.run_sync(c2)
        self.assertEqual((s2["status"], s2["skipped_current"], s2["queued"]), ("ok", 2, 0))
        self.assertEqual(c2.calls, [])

    def test_codes_and_unchanged_report(self):
        self.add_standard()
        self.run_sync(self.client(), codes=["BETAFOOD"])
        self.assertEqual(self.q("SELECT count(*) FROM documents")[0][0], 1)
        c = self.client()
        s = self.run_sync(c, codes="500002", refresh=False)
        self.assertEqual(s["skipped_current"], 1)
        s = self.run_sync(c, codes="500002", refresh=True)
        self.assertEqual((s["attempted"], s["pdfs"], s["skipped_current"]), (1, 1, 0))

    def test_recent_report_is_current_whatever_its_label(self):
        # a December year-end company: its label-2025 report filed in April 2026 is its newest; from April 2026 the
        # March label rule alone would list it again on every run
        self.add_standard()
        over = {list_url("500002"): listing(ar_row("500002", "2025", URL_B, auth="2026-04-15T10:00:00"))}
        with unittest.mock.patch.object(bse, "_today", return_value=dt.date(2026, 9, 27)):
            self.run_sync(self.client(**over), codes=["500002"])
            c = self.client(**over)
            s = self.run_sync(c, codes=["500002"])
        self.assertEqual((s["skipped_current"], s["queued"], c.calls), (1, 0, []))
        with unittest.mock.patch.object(bse, "_today", return_value=dt.date(2027, 1, 20)):   # 280 days on
            c = self.client(**over)
            s = self.run_sync(c, codes=["500002"])
        self.assertEqual((s["skipped_current"], s["queued"]), (0, 1))
        self.assertEqual(c.calls, [list_url("500002")])          # listed again; the same report is not re-downloaded

    def test_blocked_pdf_stops_and_marks(self):
        self.add_standard()
        c = self.client(**{URL_A: Blocked(URL_A, 403, "http_403")})
        s = self.run_sync(c, workers=1)
        self.assertEqual(s["status"], "blocked")
        self.assertEqual((s["blocked_url"], s["blocked_status"]), (URL_A, 403))
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, bse.COMMAND))
        self.assertEqual(self.q("SELECT count(*) FROM documents WHERE cik = '500001'")[0][0], 0)

    def test_suspect_scrip_list_changes_nothing(self):
        self.add_standard()
        s = self.run_sync(self.client(), min_scrip_rows=100)
        self.assertEqual((s["status"], s["stopped_reason"]), ("error", "scrip_list_suspect"))
        self.assertEqual(self.q("SELECT count(*) FROM identifiers")[0][0], 0)

    def many(self, n: int) -> dict:
        """n invented companies Z0..Z(n-1) in the universe (market cap order) and the scrip list serving them."""
        for i in range(n):
            self.add(f"BSE:Z{i}", 1e9 - i, f"INE00{i}Z01010")
        scrips = [{"SCRIP_CD": f"53000{i}", "Status": "Active", "ISIN_NUMBER": f"INE00{i}Z01010",
                   "scrip_id": f"Z{i}", "Segment": "Equity"} for i in range(n)]
        return {bse.SCRIP_LIST_URL: json.dumps(scrips).encode()}

    def test_first_refusal_or_page_stops_the_run(self):
        for answer, reason in (((302, b"", {"Location": "/members/x"}), "redirect_refusal"),
                               ((200, b"<html><title>Access Denied</title></html>", {"Content-Type": "text/html"}),
                                "non_json_page")):
            with self.subTest(reason=reason):
                self.tearDown()
                self.setUp()
                gets = self.many(4)
                gets.update({list_url(f"53000{i}"): answer for i in range(4)})
                c = FakeClient(gets)
                s = self.run_sync(c)
                self.assertEqual((s["status"], s["stopped_reason"], s["blocked_reason"]), ("blocked", "blocked", reason))
                self.assertEqual(c.calls, [bse.SCRIP_LIST_URL, list_url("530000")])     # nothing after the first
                self.assertIn(reason, guard.recent_block_marker(self.cfg, bse.COMMAND)["note"])

    def test_pdf_denial_page_stops_the_run(self):
        gets = self.many(3)
        urls = [URL_A.replace("000000000001", f"00000000010{i}") for i in range(3)]
        gets.update({list_url(f"53000{i}"): listing(ar_row(f"53000{i}", "2026", urls[i])) for i in range(3)})
        gets.update({u: (200, b"<html><title>Access Denied</title></html>", {"Content-Type": "text/html"})
                     for u in urls})
        c = FakeClient(gets)
        s = self.run_sync(c, workers=1)
        self.assertEqual((s["status"], s["blocked_reason"], s["blocked_url"]), ("blocked", "non_pdf_body", urls[0]))
        self.assertEqual([u for u in c.calls if u.endswith(".pdf")], urls[:1])
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, bse.COMMAND))

    def test_scrip_list_reused(self):
        self.add_standard()
        self.run_sync(self.client(), codes=["500001"])
        c = self.client()
        s = self.run_sync(c, codes=["500002"])
        self.assertTrue(s["scrip_list_reused"])
        self.assertNotIn(bse.SCRIP_LIST_URL, c.calls)


@unittest.skipUnless(HAVE_FITZ, "PyMuPDF not installed")
class TestSyncStops(SyncBase):
    """How a run ends: a block with workers > 1, Ctrl-C, consecutive errors."""

    def many(self, n: int) -> dict:
        for i in range(n):
            self.add(f"BSE:Z{i}", 1e9 - i, f"INE00{i}Z01010")
        scrips = [{"SCRIP_CD": f"53000{i}", "Status": "Active", "ISIN_NUMBER": f"INE00{i}Z01010",
                   "scrip_id": f"Z{i}", "Segment": "Equity"} for i in range(n)]
        return {bse.SCRIP_LIST_URL: json.dumps(scrips).encode()}

    def test_blocked_pdf_with_workers_stops_listing(self):
        gets = self.many(8)
        urls = [URL_B.replace("000000000001", f"00000000020{i}") for i in range(8)]
        gets.update({list_url(f"53000{i}"): listing(ar_row(f"53000{i}", "2026", urls[i])) for i in range(8)})
        gets.update({u: make_pdf([["Management Discussion and Analysis", LOREM_B * 6]]) for u in urls[1:]})
        gets[urls[0]] = Blocked(urls[0], 429, "http_429")

        class Gated(FakeClient):
            """The other PDFs wait until the block has happened, so the run's order is fixed."""
            def get(self, url: str, **kw) -> Response:
                if url.endswith(".pdf") and url != urls[0]:
                    self.halt.wait(5)
                return super().get(url, **kw)
        c = Gated(gets)
        s = self.run_sync(c, workers=2)
        self.assertEqual((s["status"], s["blocked_url"], s["blocked_status"]), ("blocked", urls[0], 429))
        listed = [u for u in c.calls if "AnnualReport_New" in u]
        self.assertLessEqual(len(listed), 2 * 2)          # the queue bound (workers * 2); nothing listed after
        self.assertEqual([u for u in c.calls if u.endswith(".pdf")], urls[:1])    # queued PDFs never sent
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, bse.COMMAND))
        self.assertEqual(self.q("SELECT count(*) FROM documents")[0][0], 0)
        self.assertEqual(self.q("SELECT status FROM runs WHERE command = 'sync-bse'"), [("blocked",)])

    def test_ctrl_c_flushes_and_records_interrupted(self):
        gets = self.many(3)
        gets.update({list_url("530000"): listing(), list_url("530001"): KeyboardInterrupt()})
        c = FakeClient(gets)
        s = self.run_sync(c)
        self.assertEqual((s["status"], s["stopped_reason"]), ("interrupted", "interrupted"))
        self.assertNotIn(list_url("530002"), c.calls)
        self.assertEqual(self.q("SELECT security_id, status FROM crawl_state WHERE source_id = ?", [bse.SOURCE_ID]),
                         [("BSE:Z0", "no_annual_report")])            # the finished company was flushed
        self.assertEqual(self.q("SELECT status FROM runs WHERE command = 'sync-bse'"), [("interrupted",)])
        self.assertIsNone(guard.recent_block_marker(self.cfg, bse.COMMAND))

    def test_consecutive_errors_stop_the_run(self):
        gets = self.many(7)
        gets.update({list_url(f"53000{i}"): 500 for i in range(7)})
        c = FakeClient(gets)
        s = self.run_sync(c)
        self.assertEqual((s["status"], s["stopped_reason"]), ("stopped_errors", "consecutive_errors"))
        self.assertEqual(len([u for u in c.calls if "AnnualReport_New" in u]), bse.MAX_CONSECUTIVE_ERRORS)
        self.assertIsNone(guard.recent_block_marker(self.cfg, bse.COMMAND))
        self.assertEqual(self.q("SELECT count(*) FROM crawl_state WHERE status = 'error'")[0][0],
                         bse.MAX_CONSECUTIVE_ERRORS)


@unittest.skipUnless(HAVE_FITZ, "PyMuPDF not installed")
class TestCliEndToEnd(SyncBase):
    """`jevscreen sync-bse` through cli.cmd_sync_official with the real adapter; http.Client.get is faked."""

    def setUp(self) -> None:
        super().setUp()
        env = unittest.mock.patch.dict("os.environ", {"JEVSCREEN_HOME": self.tmp.name,
                                                       "JEVSCREEN_MIN_INTERVAL_S": "1.0"})
        env.start()
        self.addCleanup(env.stop)
        self.calls: list[str] = []
        filler = [{"SCRIP_CD": str(600000 + i), "Status": "Active", "ISIN_NUMBER": f"INE9{i:07d}1",
                   "scrip_id": f"FILL{i}", "Segment": "Equity"} for i in range(bse.MIN_SCRIP_ROWS)]
        self.gets = {bse.SCRIP_LIST_URL: json.dumps(SCRIPS + filler).encode(),
                     list_url("500001"): listing(ar_row("500001", "2026", URL_A)),
                     list_url("500002"): listing(ar_row("500002", "2026", URL_B)),
                     URL_A: Blocked(URL_A, 403, "http_403"),
                     URL_B: make_pdf([["Management Discussion and Analysis", LOREM_B * 6]])}
        test = self

        def fake_get(client, url, **kw):
            if client.halt.is_set():
                raise Halted(url)
            test.calls.append(url)
            item = test.gets.get(url, 404)
            if isinstance(item, Blocked):
                client.halt.set()
                raise item
            if isinstance(item, int):
                return Response(url, item, b"", {}, 0.0)
            return Response(url, 200, item, {"Content-Type": "application/pdf" if item[:4] == b"%PDF" else
                                             "application/json"}, 0.0)
        p = unittest.mock.patch("jevscreen.http.Client.get", autospec=True, side_effect=fake_get)
        p.start()
        self.addCleanup(p.stop)

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        text = out.getvalue()
        start = text.rfind("\n{") + 1 if "\n{" in text else text.find("{")
        obj, _ = json.JSONDecoder().raw_decode(text[start:]) if start >= 0 else ({}, 0)
        return code, obj

    def test_block_exit_2_marker_and_cooldown(self):
        self.add_standard()
        code, out = self.run_cli("sync-bse", "--workers", "1")
        self.assertEqual((code, out.get("status")), (cli.EXIT_BLOCKED, "blocked"))
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, bse.COMMAND))
        self.assertEqual(self.calls[:2], [bse.SCRIP_LIST_URL, list_url("500001")])
        self.assertIn(URL_A, self.calls)
        self.assertNotIn(URL_B, self.calls)                      # queued behind the block: never sent
        self.assertEqual(self.q("SELECT count(*) FROM documents")[0][0], 0)
        # a rerun without --after-block is refused before any request
        self.calls.clear()
        code, out = self.run_cli("sync-bse")
        self.assertEqual((code, out.get("status"), self.calls), (cli.EXIT_BLOCKED, "cooldown", []))
        # --after-block runs (the PDF now answers) and clears the marker
        self.gets[URL_A] = make_pdf(sample_pages())
        code, out = self.run_cli("sync-bse", "--after-block")
        self.assertEqual(code, cli.EXIT_OK, out)
        self.assertIsNone(guard.recent_block_marker(self.cfg, bse.COMMAND))
        self.assertEqual(self.q("SELECT count(*) FROM documents WHERE text_path IS NOT NULL")[0][0], 2)


@unittest.skipUnless(HAVE_FITZ, "PyMuPDF not installed")
class TestScreenReadsBse(SyncBase):
    """Layer 2 of a screen reads the stored BSE section, and coverage counts exactly what the screen reads."""

    def test_layer2_reads_bse_documents(self):
        from jevscreen import coverage, report, screen
        self.add_standard()
        self.assertEqual(self.run_sync(self.client())["status"], "ok")
        with store.session(self.cfg, read_only=True, wait_s=5) as con:
            docs = screen.load_documents(con, min_mcap_usd=0)
            by_source = coverage.report(con)["official_document_by_source"]
        d = docs["isin:INE000A01011"]
        self.assertEqual((d["source_id"], d["form"]), (bse.SOURCE_ID, bse.FORM))
        self.assertIn("centrifugal pumps", Path(d["text_path"]).read_text())
        self.assertIsNone(d["report_date"])
        self.assertIsNotNone(screen.fiscal_year_of(d))        # dated by filing_date while report_date is NULL
        self.assertIn("isin:INE000B01012", docs)
        self.assertEqual(by_source[bse.SOURCE_ID], 2)
        self.assertEqual(screen.OFFICIAL_DOC_SOURCES[bse.SOURCE_ID], ("BSE", "en"))
        self.assertEqual(screen.NATIVE_ID_TYPES["bse_scrip_code"], bse.SOURCE_ID)
        self.assertEqual(screen.source_label(bse.SOURCE_ID), "BSE")
        self.assertEqual(report.SOURCE_LABELS[bse.SOURCE_ID], "BSE")


class TestOfficialSourceRegistry(unittest.TestCase):
    """screen (layer 2), coverage, report and the calibration cards name the same official sources."""

    def test_registries_agree(self):
        from jevscreen import calib, coverage, pack, report, screen
        self.assertEqual(set(coverage.OFFICIAL_DOC_SOURCES), set(screen.OFFICIAL_DOC_SOURCES))
        self.assertEqual(coverage._NATIVE_IDS, screen.NATIVE_ID_TYPES)
        for sid, (label, _lang) in screen.OFFICIAL_DOC_SOURCES.items():
            self.assertEqual(report.SOURCE_LABELS.get(sid), label, sid)
            self.assertEqual(provenance.tier_of(sid), provenance.LicenseTier.OFFICIAL_PRIVATE, sid)
            self.assertIn(label, calib.LICENCE_OFFICIAL_ZH, sid)
            self.assertTrue(sid in pack.PACK_SOURCES or sid in pack.EXCLUDED_REASONS, sid)

    def test_report_names_every_official_source(self):
        """The screen report's funnel row and licence note list every source layer 2 can read (BSE, MOPS...)."""
        from jevscreen import report, screen
        res = {
            "params": {}, "funnel": {"l2_sec_inputs": 1, "l2_inputs": 1}, "idea": "x", "run_id": "r",
            "status": "ok", "started_at": "a", "finished_at": "b", "timing": {"total_s": 1.0}, "cost_usd": 0.0,
            "budget_usd": 1.0, "layers": {"l1": {}, "l2": {"sec_inputs": 1, "inputs_by_source": {"BSE": 1}}},
            "results": [], "tiers_used": ["official-private"], "dry_run": True,
        }
        lines = report.render_markdown(res).splitlines()
        note = [ln for ln in lines if ln.startswith("Annual-report excerpts (")]
        funnel = [ln for ln in lines if ln.startswith("|") and "annual-report excerpts (" in ln]
        self.assertEqual((len(note), len(funnel)), (1, 1), lines)
        for sid, (label, _lang) in screen.OFFICIAL_DOC_SOURCES.items():
            self.assertIn(label, note[0], sid)
            self.assertIn(label, funnel[0], sid)


# =========================================================================== wiring


class TestWiring(unittest.TestCase):
    def test_provenance(self):
        self.assertEqual(provenance.tier_of(bse.SOURCE_ID), provenance.LicenseTier.OFFICIAL_PRIVATE)

    def test_cli(self):
        args = cli.build_parser().parse_args(["sync-bse", "--codes", "500325,TCS", "--workers", "2", "--limit", "5"])
        self.assertEqual((args.command, args.codes, args.workers, args.limit), ("sync-bse", ["500325", "TCS"], 2, 5))
        self.assertIs(cli.COMMANDS["sync-bse"], cli.cmd_sync_official)
        self.assertEqual(cli.OFFICIAL_SYNC["sync-bse"][0], "jevscreen.sources.bse")
        self.assertEqual(cli.CLI_RATE_BUDGETS["sync-bse"], "bse")
        self.assertTrue(cli._accepts(bse.sync, "workers") and cli._accepts(bse.sync, "on_blocked"))


if __name__ == "__main__":
    unittest.main()
