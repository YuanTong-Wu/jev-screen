# Data licences

The code in this repository is MIT-licensed (see [LICENSE](LICENSE)). **The data it downloads is not.** Every source
jev-screen reads has its own terms, and the MIT licence grants nothing over that data. This file explains, per
source, what you may do with what your copy of jev-screen fetches, and what this project will and will not
redistribute.

Nothing here is legal advice. When in doubt, keep the data on your own machine.

## Licence tiers

Every stored row points to a snapshot, every snapshot to a source, and every source to one of three tiers
(`src/jevscreen/provenance.py`). The tier decides what may ever be redistributed.

| Tier | Meaning | May it be redistributed? |
|---|---|---|
| `official-open` | Official source whose terms allow copying and redistribution | Yes, with attribution to the source |
| `official-private` | Official source, but no redistribution grant for the text (issuer-authored narrative, exchange-disclosed documents) | No. Personal use; keep the verbatim text and anything derived from it (labels, lists, screens, pages) local |
| `gray-private` | Publicly reachable, but the site's terms restrict reuse or automated use | **Never.** Personal use only; nothing derived from it is published |

Profile text and short annual-report excerpts are sent to Jev (through TypeSafe's official API, OpenRouter or Vercel
AI Gateway, whichever key you set) to be read; that is how the screen works, and nothing is published. What you may share: the code, your idea,
and outputs built only from the open data pack.

`jevscreen coverage` shows the tier behind every company's data, and every screen report carries a licence note
listing the tiers its evidence came from.

## Per source

| Source id | What | Tier | Terms | Why this tier |
|---|---|---|---|---|
| `sec_tickers` | SEC `company_tickers_exchange.json` and the submissions API (data.sec.gov) | official-open | <https://www.sec.gov/about/privacy-information> | SEC-compiled public information; may be copied and redistributed. SEC asks for a declared User-Agent with contact details and at most 10 requests/s |
| `sec_filing_text` | 10-K Item 1 / 20-F Item 4 business sections from EDGAR | official-private | same | The filing is public, but the narrative is written by the issuer, not a government work |
| `cninfo_annual_report` | CNINFO (巨潮资讯) A-share annual reports and summaries | official-private | <http://www.cninfo.com.cn/> | Exchange-designated disclosure site; the exchanges assert exclusive rights over securities information; no redistribution grant |
| `edinet_yuho` | EDINET 有価証券報告書 (事業の内容) | official-private | <https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html> | Site content is under PDL 1.0 with attribution. Only the EDINET code list and the 事業の内容 section ship in the open data pack, through an explicit allowlist and with a third-party-rights caveat (see below and [docs/OPEN_PACK.md](docs/OPEN_PACK.md)); every other part of the filing (e.g. the 経営方針 block) stays local. Needs your own free API key |
| `dart_business_report` | 사업보고서 (사업의 개요): the filing is found with the OpenDART API; by default (web mode) the section text is read from the DART website viewer (dart.fss.or.kr `dsaf001/main.do`, `report/viewer.do`), otherwise from OpenDART `document.xml` | official-private | OpenDART: <https://opendart.fss.or.kr/intro/terms.do>; DART website: its terms of use (이용약관), linked from the footer of <https://dart.fss.or.kr/> | No explicit redistribution grant on either site: text from both, including web-mode text from dart.fss.or.kr, stays local. Needs your own free API key |
| `mops_annual_report` | MOPS / TWSE e-documents: annual reports (股東會年報, 營運概況 / 業務內容), served from doc.twse.com.tw | official-private | <https://mops.twse.com.tw/mops/#/web/home> | Issuer-authored reports served by TWSE; no redistribution grant: personal use, keep the text local. doc.twse.com.tw's robots.txt disallows all crawlers, so reports are fetched on demand and slowly, never in bulk |
| `mops_basic` | MOPS basic data (公司基本資料, 主要經營業務) via mops.twse.com.tw | official-private | same | TWSE-operated site; no open-data licence covers this field (the open TWSE/TPEx company datasets lack it). Personal use only |
| `bse_annual_report` | BSE India annual report PDFs (MD&A / company overview pages) | official-private | <https://www.bseindia.com/> | Exchange-hosted issuer filings; no redistribution grant found and no BSE terms-of-use page located (checked 2026-09-27). Personal use only; keep the text local |
| `tradingview_scanner` | TradingView screener endpoint (prices, fundamentals, history) | gray-private | <https://www.tradingview.com/policies/> | Terms forbid non-display use, including algorithmic decision-making |
| `tradingview_profile` | TradingView symbol-page business descriptions | gray-private | same | Terms restrict reuse of page content |
| `financedatabase_local` | A local FinanceDatabase snapshot | gray-private | <https://github.com/JerBouma/FinanceDatabase> | The repository is MIT, but its company summaries originate from Yahoo Finance |

Jev (the model behind layer 1 and layer 2) is called with **your** key and at your cost: Jev via TypeSafe's official
API, OpenRouter or Vercel AI Gateway, same price. Its answers are yours; the issuer text you send it (profile text and short excerpts) stays subject to the tier above.

## What this project redistributes

- **Code and documentation**: MIT.
- **Test fixtures** (`tests/fixtures/`): synthetic, hand-made, or transformed so that no original sentence, name or
  figure survives (transformed files keep a real report's layout, headings and section numbers; every other word
  is filler and every figure's digits are permuted; report years, months, days and small counts are kept), plus
  aggregates of the project's own Jev runs. Each file's origin is declared in
  `tests/fixtures/MANIFEST.json`, and `tools/release_check.py` refuses undeclared fixtures. No third-party page,
  PDF, filing or API response is included. Company names, tickers and ISINs in test code are factual identifiers
  used as examples.
- **The daily open data pack** (hosted by the maintainer as GitHub releases; see [docs/OPEN_PACK.md](docs/OPEN_PACK.md)).
  Only sources on the explicit allowlist `jevscreen.pack.PACK_SOURCES` ship, each with its attribution text:
  - `sec_tickers` (official-open): the SEC ticker list, unchanged in content.
  - `edinet_yuho` (official-private, allowlisted under PDL 1.0): the EDINET code list (listed filers only, never
    individuals) and the 事業の内容 section of each filer's newest 有価証券報告書, with the PDL source and edit
    notice. **Caveat:** 事業の内容 is written by each filing company, PDL 1.0 does not apply to content in which a
    third party holds rights, and whether issuer-written text falls under the grant is not confirmed. The pack's
    manifest and `ATTRIBUTION.txt` carry this caveat; check before republishing the text.

  Everything else never ships: no SEC 10-K / 20-F text (`sec_filing_text`), nothing from CNINFO
  (`cninfo_annual_report`), OpenDART / the DART website (`dart_business_report`), MOPS / TWSE (`mops_annual_report`,
  `mops_basic`) or BSE (`bse_annual_report`), and nothing gray-private. Every pack
  manifest lists these excluded sources with their reasons (`pack.EXCLUDED_REASONS`). Users fetch official-private
  text themselves, on demand, from the official source.
- **Never**: gray-private data (TradingView, FinanceDatabase/Yahoo text), or any database, list, screen, report or
  chart derived from it; official-private verbatim text (the allowlisted EDINET 事業の内容 above is the one
  exception); API keys; anyone's local `data/` directory.

If you run jev-screen yourself, the same rules bind you: the tool puts gray-private and official-private data in
your local `data/` directory for your personal use. Do not publish that directory, your DuckDB file, or screen
reports built from gray-private or official-private evidence.

## Third-party material inside the repository

| Where | What | Licence |
|---|---|---|
| `tests/fixtures/cninfo_309386_*.pdf` | Embedded subset of the Droid Sans Fallback font (Android Open Source Project; the CJK fallback font bundled with PyMuPDF/MuPDF), used to render the invented CJK test text. The subset was modified by subsetting; its own name table carries no copyright notice | Apache License 2.0, full text in [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt) |

## Reporting a problem

If you believe something in this repository should not be here (a real document, personal data, data under
terms that forbid redistribution), please open an issue without quoting the material, or follow
[SECURITY.md](SECURITY.md) for anything sensitive. It will be removed.
