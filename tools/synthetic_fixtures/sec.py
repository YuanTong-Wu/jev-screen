"""Synthetic SEC EDGAR fixtures: company_tickers_exchange.json, three submissions files and two annual reports.

All issuers, CIKs (99901xx / 99902xx, far above any assigned CIK), accession numbers, tickers and every sentence
are invented. The HTML reports copy only the *structure* the extractor has to handle in inline-XBRL filings: a
hidden ix:header with dei:/us-gaap: facts, a cover page, a table of contents whose rows look like headings, page
footers ('14' / 'Table of Contents') before each heading, 'ITEM 1A.' and 'RISK FACTORS' in adjacent inline
elements, 20-F sub-headings 'Item 4.A ...' / 'B. Business Overview' inside Item 4.
Run: python3 tools/synthetic_fixtures/sec.py
"""
from __future__ import annotations

import gzip
import json
import random
from pathlib import Path

FIX = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

PICKS = {
    "QKNW": {"cik": 9990101, "form": "10-K", "acc": "0009990101-26-000012", "doc": "qknw-20260630x10k.htm",
             "date": "2026-09-10", "report": "2026-06-30"},
    "QCXL": {"cik": 9990102, "form": "20-F", "acc": "0009990102-26-000010", "doc": "qcxl-20251231.htm",
             "date": "2026-02-26", "report": "2025-12-31"},
    "QFRT": {"cik": 9990103, "form": "40-F", "acc": "0009990103-26-000003", "doc": "qfrt-20260131x40f.htm",
             "date": "2026-03-11", "report": "2026-01-31"},
}
NAMES = {"QKNW": "Quillknow Inc.", "QCXL": "Calyx Listen Ltd.", "QFRT": "Fretline Systems Inc."}


# --------------------------------------------------------------------------- tickers

def tickers() -> dict:
    data = [
        [9990101, "Quillknow Inc.", "QKNW", "Nasdaq"],
        [9990102, "Calyx Listen Ltd.", "QCXL", "Nasdaq"],
        [9990103, "Fretline Systems Inc.", "QFRT", "Nasdaq"],
        [9990201, "Holdfast Holdings Inc.", "HLDG-B", "NYSE"],      # class lines of one CIK: '.B' / '-B' / '/B'
        [9990201, "Holdfast Holdings Inc.", "HLDG-A", "NYSE"],
        [9990202, "Brown Barrel Distillers Corp", "BRBL-B", "NYSE"],  # only the B line is listed
        [9990203, "Bazaar Group Holding Ltd.", "BZAR", "NYSE"],       # DR line is the US primary
        [9990301, "Quartzline Semiconductor Corp", "QQSC", "Nasdaq"],
        [9990302, "Orchard Devices Inc.", "ORCD", "Nasdaq"],
        [9990303, "Gridsearch Holdings Inc.", "QGRD", "Nasdaq"],
        [9990304, "Mosaic Softworks Corp", "QMSW", "Nasdaq"],
        [9990305, "Amberway Commerce Inc.", "QAMB", "Nasdaq"],
        [9990401, "Pinecrest Bancorp", "PCBK", "OTC"],
        [9990402, "Tidewater Options Exchange Inc.", "QTOX", "CBOE"],
        [9990403, "Lantern Trust", "QLTN", None],                     # exchange unknown
        [9990404, "Blank Ticker Fund", "", "NYSE"],                   # no ticker: skipped by parse_tickers
    ]
    rng = random.Random(9990)
    for i in range(40):                                               # filler rows, invented names
        cik = 9991000 + i
        sym = "Z" + "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXY") for _ in range(3))
        data.append([cik, f"Filler Company {i:02d} Inc.", sym, rng.choice(["Nasdaq", "NYSE", "OTC"])])
    return {"fields": ["cik", "name", "ticker", "exchange"], "data": data}


# --------------------------------------------------------------------------- submissions

RECENT_KEYS = ["accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "act", "form", "fileNumber",
               "filmNumber", "items", "core_type", "size", "isXBRL", "isInlineXBRL", "isXBRLNumeric",
               "primaryDocument", "primaryDocDescription"]


def submissions(t: str) -> dict:
    p, rng = PICKS[t], random.Random(p_seed(t))
    cik, base_form = p["cik"], p["form"]
    periodic = {"10-K": "10-Q", "20-F": "6-K", "40-F": "6-K"}[base_form]
    rows = []
    lag = int(p["date"][:4]) - int(p["report"][:4])       # report year = filing year - lag
    for y in range(2026, 2016, -1):                     # 10 years of invented filings
        for q in range(3, 0, -1):
            rows.append((periodic, f"{y}-{q * 3 + 1:02d}-{rng.randrange(10, 28)}", f"{y}-{q * 3:02d}-30"))
        rows.append(("8-K" if base_form == "10-K" else "6-K", f"{y}-{rng.randrange(1, 12):02d}-{rng.randrange(10, 28)}", ""))
        rows.append((base_form, f"{y}-{p['date'][5:]}", f"{y - lag}-{p['report'][5:]}"))
    rows = [r for r in rows if r[1] <= "2026-09-20"]    # nothing after the fixture's as-of date
    rows.append(("SC 13G/A", "2025-02-14", ""))
    if base_form == "10-K":
        rows.append(("10-K/A", "2024-10-28", "2024-06-30"))       # an older amendment: ignored
    rows.sort(key=lambda r: r[1], reverse=True)
    recent = {k: [] for k in RECENT_KEYS}
    n = 0
    for form, fdate, rdate in rows:
        n += 1
        acc = f"{cik:010d}-{fdate[2:4]}-{n:06d}"
        doc = f"{t.lower()}-{fdate.replace('-', '')}x{form.lower().replace('/', '').replace(' ', '')}.htm"
        if form == base_form and fdate == p["date"]:
            acc, doc = p["acc"], p["doc"]
        vals = {"accessionNumber": acc, "filingDate": fdate, "reportDate": rdate,
                "acceptanceDateTime": f"{fdate}T16:05:{n % 60:02d}.000Z", "act": "34", "form": form,
                "fileNumber": f"000-9{cik % 100000:05d}", "filmNumber": f"26{n:07d}", "items": "",
                "core_type": form, "size": rng.randrange(20_000, 9_000_000), "isXBRL": int(form != "SC 13G/A"),
                "isInlineXBRL": int(form != "SC 13G/A"), "isXBRLNumeric": int(form in (base_form, periodic)),
                "primaryDocument": doc, "primaryDocDescription": form}
        for k in RECENT_KEYS:
            recent[k].append(vals[k])
    return {"cik": str(cik), "entityType": "operating", "sic": "7372", "sicDescription": "Services-Prepackaged Software",
            "ownerOrg": "06 Technology", "insiderTransactionForOwnerExists": 0, "insiderTransactionForIssuerExists": 1,
            "name": NAMES[t].upper().rstrip("."), "tickers": [t], "exchanges": ["Nasdaq"], "ein": "000000000",
            "lei": None, "description": "", "website": "", "investorWebsite": "",
            "category": "Non-accelerated filer", "fiscalYearEnd": p["report"][5:].replace("-", ""),
            "stateOfIncorporation": "DE" if base_form == "10-K" else "", "stateOfIncorporationDescription": "",
            "addresses": {"mailing": {"street1": "1 Example Way", "city": "Exampleton", "stateOrCountry": "XX",
                                      "zipCode": "00000"}},
            "phone": "000-000-0000", "flags": "", "formerNames": [],
            "filings": {"recent": recent, "files": []}}


def p_seed(t: str) -> int:
    return sum(map(ord, t))


# --------------------------------------------------------------------------- documents

WORDS = {
    "subject": ["Our platform", "The knowledge hub", "Our answer engine", "The authoring studio", "Our analytics suite",
                "The connector library", "Our agent console", "The content graph"],
    "verb": ["helps", "lets", "enables", "allows"],
    "who": ["service teams", "contact-center agents", "support engineers", "field technicians", "self-service portals",
            "AI assistants", "compliance reviewers", "customer engagement teams"],
    "what": ["find approved answers quickly", "keep articles consistent across channels",
             "resolve cases without escalation", "reuse vetted knowledge in chat and email",
             "measure which answers close cases", "retire stale content on a schedule",
             "route questions to the right expert", "ground AI responses in reviewed knowledge"],
    "tail": ["in regulated industries such as banking and insurance", "across web, mobile and voice channels",
             "for customers in more than twenty countries", "under subscription contracts of one to three years",
             "with deployment in our cloud or a customer's own cloud account", "in several languages"],
}


def paragraphs(rng: random.Random, n: int, company: str) -> list[str]:
    out = []
    for i in range(n):
        sents = []
        for _ in range(rng.randrange(3, 6)):
            sents.append(f"{rng.choice(WORDS['subject'])} {rng.choice(WORDS['verb'])} "
                         f"{rng.choice(WORDS['who'])} {rng.choice(WORDS['what'])} {rng.choice(WORDS['tail'])}.")
        if i % 4 == 0:
            sents.append(f"{company} sells these products through a direct sales force and a small number of "
                         f"resellers (synthetic test text).")
        out.append(" ".join(sents))
    return out


HIDDEN = ("<div style=\"display:none\"><ix:header><ix:hidden>"
          "<ix:nonNumeric name=\"dei:EntityRegistrantName\" contextRef=\"c-1\">{name}</ix:nonNumeric>"
          "<ix:nonNumeric name=\"dei:DocumentType\" contextRef=\"c-1\">{form}</ix:nonNumeric>"
          "<ix:nonFraction name=\"us-gaap:Revenues\" contextRef=\"c-1\" unitRef=\"usd\" decimals=\"-3\">"
          "123456000</ix:nonFraction></ix:hidden><ix:references><link:schemaRef xlink:href=\"x.xsd\"/>"
          "</ix:references></ix:header></div>")
P = "<p style=\"font-family:'Times New Roman';font-size:10pt;margin:0pt 0pt 10pt 0pt;\">{}</p>"
FOOTER = ("<p style=\"text-align:center;margin:0pt;\">{page}</p><hr style=\"page-break-after:always\"/>"
          "<div><p style=\"margin:0pt 0pt 30pt 0pt;\"><a href=\"#Toc\"><span>Table of Contents</span></a></p></div>")


def heading(label: str, title: str) -> str:
    """'ITEM 1A.' and 'RISK FACTORS' in adjacent inline elements -> text 'ITEM 1A.RISK FACTORS'."""
    return (f"<div style=\"clear:both\"><p style=\"font-weight:bold;margin:0pt 0pt 10pt 0pt;\">"
            f"<span style=\"display:inline-block;width:72pt;\"><b>{label}</b></span><b>{title}</b></p></div>")


def toc(rows: list[tuple[str, str, int]]) -> str:
    trs = "".join(f"<tr><td><a href=\"#i{i}\">{a}</a></td><td><a href=\"#i{i}\">{b}</a></td><td>{pg}</td></tr>"
                  for i, (a, b, pg) in enumerate(rows))
    return f"<div id=\"Toc\"><p><b>TABLE OF CONTENTS</b></p><table>{trs}</table></div>"


def cover(name: str, form: str, t: str, fy: str) -> str:
    lines = ["UNITED STATES", "SECURITIES AND EXCHANGE COMMISSION", "Washington, D.C. 20549", f"FORM {form}",
             f"For the fiscal year ended {fy}", f"{name}", "(Exact name of registrant as specified in its charter)",
             f"Trading Symbol: {t}", "Indicate by check mark whether the registrant is a shell company. Yes ☐ No ☒"]
    return "".join(P.format(x) for x in lines)


def doc_10k() -> str:
    t, name, rng = "QKNW", NAMES["QKNW"], random.Random(101)
    toc_rows = [("PART I", "", 0), ("Item 1.", "Business", 4), ("Item 1A.", "Risk Factors", 15),
                ("Item 1B.", "Unresolved Staff Comments", 31), ("Item 1C.", "Cybersecurity", 31),
                ("Item 2.", "Properties", 32), ("Item 3.", "Legal Proceedings", 32)]
    body = [
        "<html xmlns=\"http://www.w3.org/1999/xhtml\" xmlns:ix=\"http://www.xbrl.org/2013/inlineXBRL\">"
        "<head><title>qknw-20260630x10k</title><style>p{margin:0}</style></head><body>",
        HIDDEN.format(name=name, form="10-K"), cover(name, "10-K", t, "June 30, 2026"), FOOTER.format(page=1),
        toc(toc_rows), FOOTER.format(page=2),
        P.format("SPECIAL NOTE REGARDING FORWARD-LOOKING STATEMENTS"),
        P.format("This report contains forward-looking statements within the meaning of the Private Securities "
                 "Litigation Reform Act of 1995. Words such as expect and plan identify them."),
        FOOTER.format(page=3), P.format("PART I"),
        "<div><p style=\"font-weight:bold\"><span style=\"display:inline-block\"><b>ITEM 1.</b></span></p>"
        "<p style=\"font-weight:bold\"><b>BUSINESS</b></p></div>",
        P.format("Overview"),
        P.format(f"{name.split()[0]} builds knowledge management software for customer engagement teams. "
                 "Enterprises use our SaaS platform to give customers, employees and AI agents consistent, "
                 "reviewed answers, which lowers service cost and shortens the time needed to resolve a case."),
    ]
    page = 4
    for sub in ("Our Products", "Customers", "Sales and Marketing", "Research and Development", "Competition",
                "Intellectual Property", "Human Capital", "Available Information"):
        body.append(P.format(f"<b>{sub}</b>"))
        body += [P.format(x) for x in paragraphs(rng, 9, name)]
        body.append(FOOTER.format(page=page))
        page += 1
    body.append(P.format("<b>Information about our Executive Officers</b>"))
    body.append(P.format("Our executive officers are appointed by the board and serve at its discretion. Their "
                         "biographies are omitted from this synthetic document."))
    body.append(FOOTER.format(page=14))
    body.append(heading("ITEM 1A.", "RISK FACTORS"))
    body += [P.format("Investing in our stock involves risk. " + x) for x in paragraphs(rng, 6, name)]
    body.append(FOOTER.format(page=30))
    body.append(heading("ITEM 1B.", "UNRESOLVED STAFF COMMENTS"))
    body.append(P.format("None."))
    body.append(heading("ITEM 1C.", "CYBERSECURITY"))
    body.append(P.format("We maintain a security program overseen by the audit committee (synthetic)."))
    body.append(heading("ITEM 2.", "PROPERTIES"))
    body.append(P.format("We lease our headquarters and two regional offices."))
    body.append(heading("ITEM 3.", "LEGAL PROCEEDINGS"))
    body.append(P.format("We are not party to any material legal proceedings."))
    body.append("</body></html>")
    return "\n".join(body)


def doc_20f() -> str:
    t, name, rng = "QCXL", NAMES["QCXL"], random.Random(102)
    short = "Calyx Listen"
    toc_rows = [("Item 1.", "Identity of Directors, Senior Management and Advisers", 3),
                ("Item 3.", "Key Information", 3), ("Item 4.", "Information on the Company", 40),
                ("Item 4A.", "Unresolved Staff Comments", 71), ("Item 5.", "Operating and Financial Review and "
                                                                         "Prospects", 71)]
    body = [
        "<html xmlns=\"http://www.w3.org/1999/xhtml\" xmlns:ix=\"http://www.xbrl.org/2013/inlineXBRL\">"
        "<head><title>qcxl-20251231</title></head><body>",
        HIDDEN.format(name=name, form="20-F"), cover(name, "20-F", t, "December 31, 2025"), FOOTER.format(page=1),
        toc(toc_rows), FOOTER.format(page=2),
        P.format("<b>Item 3. Key Information</b>"),
        P.format("D. Risk Factors"),
        P.format("For a description of the risks we face, see this item. " + " ".join(paragraphs(rng, 1, name))),
        FOOTER.format(page=39),
        P.format("<b>Item 4. Information on the Company</b>"),
        P.format("<b>Item 4.A History and Development of the Company</b>"),
        P.format(f"{name} was incorporated in the fictional Republic of Examplia and was founded on March 3, 1994. "
                 "Our principal executive offices are located at 1 Example Way, Exampleton. Our agent in the United "
                 "States is a synthetic service company."),
        P.format("In 2025 we completed the acquisition of a small analytics business (synthetic)."),
        P.format("<b>B. Business Overview</b>"),
        P.format(f"{short} is a global enterprise software company that helps organizations run customer experience "
                 "operations in the cloud. Its platform records, routes and analyses customer interactions across "
                 "voice and digital channels and applies AI to recommend the next best action."),
    ]
    page = 41
    for sub in ("Our Strategy", "Cloud Platform", "Customer Experience Applications", "AI and Analytics",
                "Financial Crime and Compliance", "Customers", "Sales and Distribution", "Competition",
                "Research and Development", "Seasonality", "Intellectual Property", "Government Regulation"):
        body.append(P.format(f"<b>{sub}</b>"))
        body += [P.format(x) for x in paragraphs(rng, 9, short)]
        body.append(FOOTER.format(page=page))
        page += 1
    body.append(P.format("<b>C. Organizational Structure</b>"))
    body.append(P.format(f"{name} is the parent of eleven wholly owned subsidiaries (synthetic)."))
    body.append(P.format("<b>D. Property, Plants and Equipment</b>"))
    body += [P.format(x) for x in paragraphs(rng, 3, short)]
    body.append(FOOTER.format(page=70))
    body.append(P.format("<b>Item 4A. Unresolved Staff Comments</b>"))
    body.append(P.format("Not applicable."))
    body.append(P.format("<b>Item 5. Operating and Financial Review and Prospects</b>"))
    body.append(P.format("The following discussion is omitted from this synthetic document."))
    body.append("</body></html>")
    return "\n".join(body)


def _gz(name: str, data: bytes) -> None:
    with gzip.GzipFile(FIX / name, "wb", mtime=0) as f:     # mtime=0: byte-identical on every run
        f.write(data)


def main() -> None:
    _gz("sec_company_tickers_exchange.json.gz", json.dumps(tickers()).encode())
    for t in PICKS:
        _gz(f"sec_submissions_{t}.json.gz", json.dumps(submissions(t)).encode())
    _gz("sec_doc_QKNW_10-K.htm.gz", doc_10k().encode("utf-8"))
    _gz("sec_doc_QCXL_20-F.htm.gz", doc_20f().encode("utf-8"))
    (FIX / "sec_picks.json").write_text(json.dumps(PICKS, indent=1) + "\n")


if __name__ == "__main__":
    main()
